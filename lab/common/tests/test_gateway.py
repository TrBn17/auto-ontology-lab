from __future__ import annotations

import json

import httpx
import pytest

from lab.common.gateway import Gateway, GatewayError


def _gateway(handler, source: str = "lab-test") -> Gateway:
    return Gateway("http://gw", "auto_ontology_tok", source=source, transport=httpx.MockTransport(handler))


def _sse(*events: object, done: bool = True) -> bytes:
    lines = [": ping\n\n"] + [f"data: {json.dumps(e)}\n\n" for e in events]
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def test_requests_carry_api_key_and_return_json() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers["x-api-key"]
        seen["path"] = request.url.path
        return httpx.Response(200, json={"data": [], "count": 0})

    assert _gateway(handler).get("/api/connections") == {"data": [], "count": 0}
    assert seen == {"key": "auto_ontology_tok", "path": "/api/connections"}


@pytest.mark.parametrize("status", [401, 403, 409, 422, 500, 502])
def test_error_status_raises_with_status_and_body_but_not_token(status: int) -> None:
    gw = _gateway(lambda r: httpx.Response(status, text="nope"))
    with pytest.raises(GatewayError) as exc:
        gw.post("/api/connections", {"connection": {}})
    assert exc.value.status == status
    assert "nope" in str(exc.value)
    assert "auto_ontology_tok" not in str(exc.value)


def test_redirect_to_login_is_an_error_not_a_success() -> None:
    gw = _gateway(lambda r: httpx.Response(307, headers={"location": "/login"}))
    with pytest.raises(GatewayError) as exc:
        gw.get("/api/terms")
    assert exc.value.status == 307


def test_html_200_is_an_error_rather_than_a_decode_crash() -> None:
    gw = _gateway(lambda r: httpx.Response(200, text="<html>sign in</html>"))
    with pytest.raises(GatewayError, match="non-JSON"):
        gw.get("/api/terms")


def test_empty_success_body_returns_none() -> None:
    assert _gateway(lambda r: httpx.Response(204)).delete("/api/connections/x") is None


def test_post_text_returns_yaml_and_still_raises_on_errors() -> None:
    assert _gateway(lambda r: httpx.Response(200, text="a: 1\n")).post_text("/api/model/export", {}) == "a: 1\n"
    with pytest.raises(GatewayError):
        _gateway(lambda r: httpx.Response(403, text="no")).post_text("/api/model/export", {})


def test_patch_sends_json_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PATCH"
        return httpx.Response(200, json=json.loads(request.content))

    assert _gateway(handler).patch("/api/terms/1", {"name_certified": True}) == {"name_certified": True}


def test_chat_sends_target_db_and_source_and_parses_result() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["source"] = request.headers["x-auto-ontology-source"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            content=_sse(
                {"type": "step", "node": "question_intent", "label": "x"},
                {"type": "sql", "sql": "SELECT 1"},
                {"type": "result", "answer": {"sql_code": "SELECT 2", "response": "two"}},
                {"type": "charts", "content": {}},
            ),
        )

    answer = _gateway(handler).chat("how many?", target_db="edu")
    assert seen["source"] == "lab-test"
    assert seen["body"] == {"question": "how many?", "target_db": "edu"}
    assert answer.sql == "SELECT 2"
    assert answer.response == "two"
    assert answer.error is None
    assert answer.abstained is False
    assert answer.information_only is False


def test_chat_abstention_is_distinct_from_information_answer() -> None:
    abstain = _gateway(
        lambda r: httpx.Response(200, content=_sse({"type": "result", "answer": {"sql_code": "", "response": "cannot"}}))
    ).chat("q", target_db="edu")
    assert abstain.abstained is True
    assert abstain.information_only is False

    info = _gateway(
        lambda r: httpx.Response(
            200,
            content=_sse(
                {"type": "step", "node": "information_agent"},
                {"type": "result", "answer": {"sql_code": "", "response": "a catalog answer"}},
            ),
        )
    ).chat("q", target_db="edu")
    assert info.abstained is False
    assert info.information_only is True


def test_chat_error_event_is_reported_with_last_streamed_sql() -> None:
    answer = _gateway(
        lambda r: httpx.Response(
            200, content=_sse({"type": "sql", "sql": "SELECT broken"}, {"type": "error", "message": "boom"})
        )
    ).chat("q", target_db="edu")
    assert answer.error == "boom"
    assert answer.last_streamed_sql == "SELECT broken"
    assert answer.sql == ""
    assert answer.abstained is False


def test_chat_stream_ending_without_done_is_infrastructure_failure() -> None:
    # A dropped stream must not be scored as the agent abstaining.
    gw = _gateway(lambda r: httpx.Response(200, content=_sse({"type": "step", "node": "x"}, done=False)))
    with pytest.raises(GatewayError, match="DONE"):
        gw.chat("q", target_db="edu")


def test_chat_done_without_result_or_error_is_infrastructure_failure() -> None:
    gw = _gateway(lambda r: httpx.Response(200, content=_sse({"type": "step", "node": "x"})))
    with pytest.raises(GatewayError, match="result"):
        gw.chat("q", target_db="edu")


def test_chat_http_409_raises_with_detail() -> None:
    gw = _gateway(lambda r: httpx.Response(409, json={"detail": "semantic layer missing"}))
    with pytest.raises(GatewayError, match="semantic layer missing") as exc:
        gw.chat("q", target_db="edu")
    assert exc.value.status == 409
