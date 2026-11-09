"""Client for the production gateway on :3000, the only entrypoint lab clients use."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from lab.common.errors import LabError

DEFAULT_TIMEOUT = 60.0
# UPSTREAM_ARCHITECTURE.md §3: chat needs a client timeout of at least 900s.
CHAT_TIMEOUT = 900.0
_SOURCE_HEADER = "x-auto-ontology-source"
_BODY_PREVIEW = 500


class GatewayError(LabError):
    """The gateway refused or broke a request. Never an answer from the agent."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class ChatAnswer:
    """One finished chat turn."""

    sql: str
    response: str
    error: str | None
    last_streamed_sql: str | None
    information_only: bool
    answer: dict[str, Any]
    events: list[dict[str, Any]] = field(repr=False)

    @property
    def abstained(self) -> bool:
        """No SQL and no error, and not the agent's non-SQL information path."""
        return not self.sql and self.error is None and not self.information_only


class Gateway:
    """Authenticated JSON and SSE calls. Tokens stay out of errors and repr."""

    def __init__(
        self, base_url: str, token: str, *, source: str = "lab", transport: httpx.BaseTransport | None = None
    ) -> None:
        self._source = source
        self._client = httpx.Client(
            base_url=base_url,
            headers={"x-api-key": token},
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=False,
            transport=transport,
        )

    def get(self, path: str, **params: Any) -> Any:
        return self._decode(self._client.get(path, params=params or None))

    def post(self, path: str, body: Any = None) -> Any:
        return self._decode(self._client.post(path, json=body))

    def post_text(self, path: str, body: Any = None) -> str:
        """POST to a route that answers with text, such as the YAML model export."""
        response = self._client.post(path, json=body)
        if not response.is_success:
            self._raise_for(response)
        return response.text

    def patch(self, path: str, body: Any = None) -> Any:
        return self._decode(self._client.patch(path, json=body))

    def put(self, path: str, body: Any = None) -> Any:
        return self._decode(self._client.put(path, json=body))

    def delete(self, path: str) -> Any:
        return self._decode(self._client.delete(path))

    def status_of(self, path: str) -> int:
        """Return the status code without raising, for permission probes."""
        return self._client.get(path).status_code

    def chat(
        self, question: str, *, target_db: str, evidence: str | None = None, timeout: float = CHAT_TIMEOUT
    ) -> ChatAnswer:
        """Ask one stateless question and wait for the stream to finish.

        Infrastructure failures raise, so a dropped stream can never be scored as abstention.
        """
        body: dict[str, Any] = {"question": question, "target_db": target_db}
        if evidence is not None:
            body["evidence"] = evidence
        deadline = time.monotonic() + timeout
        events: list[dict[str, Any]] = []
        done = False
        with self._client.stream(
            "POST",
            "/api/chat/completions",
            json=body,
            headers={_SOURCE_HEADER: self._source, "Accept": "text/event-stream"},
            timeout=httpx.Timeout(timeout, connect=10.0),
        ) as response:
            if not response.is_success:
                response.read()
                self._raise_for(response)
            for line in response.iter_lines():
                if time.monotonic() > deadline:
                    # Pings keep the socket busy, so httpx's read timeout alone would never fire.
                    raise GatewayError(f"chat exceeded {timeout:.0f}s without finishing")
                if not line.startswith("data: "):
                    continue
                payload = line[len("data: ") :]
                if payload == "[DONE]":
                    done = True
                    break
                events.append(json.loads(payload))
        if not done:
            raise GatewayError("chat stream ended without [DONE]")
        return _build_answer(events)

    def _decode(self, response: httpx.Response) -> Any:
        if not response.is_success:
            self._raise_for(response)
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise GatewayError(
                f"{response.request.method} {response.request.url.path} returned non-JSON: "
                f"{response.text[:_BODY_PREVIEW]}",
                response.status_code,
            ) from exc

    @staticmethod
    def _raise_for(response: httpx.Response) -> None:
        raise GatewayError(
            f"{response.request.method} {response.request.url.path} returned {response.status_code}: "
            f"{response.text[:_BODY_PREVIEW]}",
            response.status_code,
        )


def _build_answer(events: list[dict[str, Any]]) -> ChatAnswer:
    result = next((e for e in events if e.get("type") == "result"), None)
    error = next((e for e in events if e.get("type") == "error"), None)
    if result is None and error is None:
        raise GatewayError("chat stream finished with neither a result nor an error event")
    answer = (result or {}).get("answer") or {}
    streamed = [e.get("sql") for e in events if e.get("type") == "sql" and e.get("sql")]
    return ChatAnswer(
        sql=str(answer.get("sql_code") or ""),
        response=str(answer.get("response") or ""),
        error=str(error.get("message") or "unknown error") if error else None,
        last_streamed_sql=streamed[-1] if streamed else None,
        information_only=any(e.get("type") == "step" and e.get("node") == "information_agent" for e in events),
        answer=answer,
        events=events,
    )
