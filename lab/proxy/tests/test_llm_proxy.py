from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from lab.proxy.llm_proxy import describe_request, make_server, rewrite_body, usage_summary

SCHEMA = {"type": "object"}


def test_usage_summary_reads_only_tokens_and_finish_reason() -> None:
    body = json.dumps(
        {
            "choices": [{"message": {"content": "secret answer"}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 8192},
        }
    ).encode()
    assert usage_summary(body) == "completion_tokens=8192 finish=length"


@pytest.mark.parametrize(
    "body",
    [
        {"model": "nvidia/m-1", "guided_json": {"title": "QuestionIntentModel"}},
        {"model": "nvidia/m-1", "nvext": {"guided_json": {"title": "QuestionIntentModel"}}},
        {"model": "nvidia/m-1", "response_format": {"json_schema": {"name": "QuestionIntentModel"}}},
    ],
)
def test_describe_request_names_the_model_and_step(body: dict) -> None:
    assert describe_request(json.dumps(body).encode()) == "model=nvidia/m-1 schema=QuestionIntentModel"


def test_describe_request_never_logs_prompts_or_odd_values() -> None:
    body = {"model": "a b\nsecret", "messages": [{"content": "private"}], "guided_json": {"title": "x y"}}
    assert describe_request(json.dumps(body).encode()) == "model=- schema=-"
    assert describe_request(b"not json") == "model=- schema=-"
    assert describe_request(None) == "model=- schema=-"


def test_usage_summary_ignores_the_same_words_inside_model_content() -> None:
    content = json.dumps({"completion_tokens": 1, "finish_reason": "fake"})
    body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
    assert usage_summary(body) == "completion_tokens=- finish=-"


def test_moves_nvext_guided_json_to_the_top_level() -> None:
    body, changed = rewrite_body({"model": "m", "nvext": {"guided_json": SCHEMA}})
    assert changed is True
    assert body == {"model": "m", "guided_json": SCHEMA}


def test_moves_guided_choice_too() -> None:
    body, changed = rewrite_body({"nvext": {"guided_choice": ["a", "b"]}})
    assert changed is True
    assert body == {"guided_choice": ["a", "b"]}


def test_keeps_other_nvext_fields() -> None:
    body, _ = rewrite_body({"nvext": {"guided_json": SCHEMA, "agent_hints": {"a": 1}}})
    assert body == {"guided_json": SCHEMA, "nvext": {"agent_hints": {"a": 1}}}


@pytest.mark.parametrize(
    "request_body",
    [
        {"model": "m", "messages": []},
        {"response_format": {"type": "json_schema"}},
        {"guided_json": SCHEMA},
        {"nvext": {"agent_hints": {}}},
        {"nvext": "not a dict"},
    ],
)
def test_everything_else_is_untouched(request_body: dict) -> None:
    body, changed = rewrite_body(json.loads(json.dumps(request_body)))
    assert changed is False
    assert body == request_body


class _Upstream(BaseHTTPRequestHandler):
    seen: list[dict] = []

    def _record(self, body: bytes) -> None:
        _Upstream.seen.append({"method": self.command, "path": self.path, "auth": self.headers.get("authorization"), "body": body})

    def do_GET(self) -> None:
        self._record(b"")
        self._respond(200, b'{"data": []}', "application/json")

    def do_POST(self) -> None:
        self._record(self.rfile.read(int(self.headers.get("content-length", 0))))
        if self.path.endswith("/fail"):
            self._respond(429, b'{"detail": "slow down"}', "application/json")
        elif self.path.endswith("/stream"):
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            for chunk in (b"data: one\n\n", b"data: two\n\n"):
                self.wfile.write(chunk)
                self.wfile.flush()
        else:
            self._respond(200, b'{"ok": true}', "application/json")

    def _respond(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def proxy() -> Iterator[str]:
    _Upstream.seen = []
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    server = make_server("127.0.0.1", 0, f"http://127.0.0.1:{upstream.server_port}/v1")
    for target in (upstream, server):
        threading.Thread(target=target.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/v1"
    server.shutdown()
    upstream.shutdown()


def test_forwards_rewritten_body_and_auth(proxy: str) -> None:
    response = httpx.post(
        f"{proxy}/chat/completions",
        headers={"Authorization": "Bearer secret"},
        json={"model": "m", "nvext": {"guided_json": SCHEMA}},
    )
    assert response.json() == {"ok": True}
    seen = _Upstream.seen[0]
    assert seen["path"] == "/v1/chat/completions"
    assert seen["auth"] == "Bearer secret"
    assert json.loads(seen["body"]) == {"model": "m", "guided_json": SCHEMA}


def test_get_requests_pass_through(proxy: str) -> None:
    assert httpx.get(f"{proxy}/models").json() == {"data": []}
    assert _Upstream.seen[0]["path"] == "/v1/models"


def test_upstream_errors_keep_their_status_and_body(proxy: str) -> None:
    response = httpx.post(f"{proxy}/fail", json={})
    assert response.status_code == 429
    assert response.json() == {"detail": "slow down"}


def test_streaming_responses_arrive_whole(proxy: str) -> None:
    response = httpx.post(f"{proxy}/stream", json={})
    assert response.text == "data: one\n\ndata: two\n\n"


def test_non_json_body_is_forwarded_as_is(proxy: str) -> None:
    httpx.post(f"{proxy}/chat/completions", content=b"not json")
    assert _Upstream.seen[0]["body"] == b"not json"


def test_unreachable_upstream_returns_502() -> None:
    server = make_server("127.0.0.1", 0, "http://127.0.0.1:9/v1")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        response = httpx.post(f"http://127.0.0.1:{server.server_port}/v1/x", json={})
        assert response.status_code == 502
    finally:
        server.shutdown()
