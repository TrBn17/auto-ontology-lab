"""Direct LLM calls for the baseline modes, which do not go through Auto-Ontology."""

from __future__ import annotations

import re
import time
from collections.abc import Callable

import httpx

from lab.common.errors import LabError

_FENCE = re.compile(r"```(?:sql)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_TIMEOUT = 300.0
_BODY_PREVIEW = 300
_RETRY_STATUSES = {429, 503}
_MAX_ATTEMPTS = 6
_MAX_DELAY_SECONDS = 60.0
_MIN_INTERVAL_SECONDS = 3.0


class LlmClient:
    """An OpenAI-compatible chat endpoint. Temperature 0 is the only pinned sampling setting."""

    def __init__(
        self,
        endpoint: str,
        model: str,
        api_key: str,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._model = model
        self._sleep = sleep
        self._next_call_at = 0.0
        self._client = httpx.Client(
            base_url=endpoint.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=_TIMEOUT,
            transport=transport,
        )

    def complete(self, prompt: str) -> str:
        response = self._post_with_backoff(
            {"model": self._model, "temperature": 0, "messages": [{"role": "user", "content": prompt}]}
        )
        if not response.is_success:
            raise LabError(f"LLM returned {response.status_code}: {response.text[:_BODY_PREVIEW]}")
        try:
            return str(response.json()["choices"][0]["message"]["content"] or "")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LabError(f"LLM response had an unexpected shape: {response.text[:_BODY_PREVIEW]}") from exc


    def _post_with_backoff(self, body: dict) -> httpx.Response:
        """Space calls out and wait on rate limits, because a burst also slows the gateway mode's calls."""
        for attempt in range(_MAX_ATTEMPTS):
            wait = self._next_call_at - time.monotonic()
            if wait > 0:
                self._sleep(wait)
            self._next_call_at = time.monotonic() + _MIN_INTERVAL_SECONDS
            response = self._client.post("/chat/completions", json=body)
            if response.status_code not in _RETRY_STATUSES or attempt == _MAX_ATTEMPTS - 1:
                return response
            self._sleep(_retry_delay(response, attempt))
        raise AssertionError("unreachable")


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    try:
        return min(float(response.headers["retry-after"]), _MAX_DELAY_SECONDS)
    except (KeyError, ValueError):
        return min(5.0 * 2**attempt, _MAX_DELAY_SECONDS)


def build_prompt(schema: str, question: str, evidence: str | None) -> str:
    parts = [
        "You write PostgreSQL queries. Answer with one SQL query in a ```sql code block.",
        f"Schema:\n{schema}",
    ]
    if evidence:
        parts.append(f"Hint: {evidence}")
    parts.append(f"Question: {question}")
    return "\n\n".join(parts)


def extract_sql(text: str) -> str:
    """Take the last fenced block, or the whole reply when there is none."""
    blocks = _FENCE.findall(text)
    sql = blocks[-1] if blocks else text
    return sql.strip().rstrip(";").strip()
