"""A forwarding proxy that makes Auto-Ontology's structured output work on the hosted endpoint.

langchain-nvidia sends `nvext: {guided_json: ...}`, which integrate.api.nvidia.com now rejects
with a 400, while `guided_json` at the top level of the body works (upstream issue #440). This
moves those keys and changes nothing else, so the lab can measure upstream's code unmodified.

Uses only the standard library, because it runs from a mounted file in the stack's own image.
Logs method, path, status, model, the structured output's schema name, whether a body was rewritten,
timing and token counts; it never logs prompts, answers or headers. A call is logged when upstream
finishes, even if the client already gave up, so calls that outlast a client timeout still show
their real duration.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

_MOVED_KEYS = ("guided_json", "guided_choice")
_DROPPED_REQUEST_HEADERS = {"host", "content-length", "connection", "accept-encoding"}
_DROPPED_RESPONSE_HEADERS = {"connection", "transfer-encoding", "content-length", "server", "date"}
_TIMEOUT_SECONDS = 600
_CHUNK = 8192
_COMPLETION_TOKENS = re.compile(rb'"completion_tokens":\s*(\d+)')
_FINISH_REASON = re.compile(rb'"finish_reason":\s*"(\w+)"')
_LABEL = re.compile(r"[\w./:-]{1,120}")


def usage_summary(body: bytes) -> str:
    """Extract only the token count and finish reason. The last match wins for streamed bodies."""
    tokens = _COMPLETION_TOKENS.findall(body)
    finish = _FINISH_REASON.findall(body)
    return f"completion_tokens={tokens[-1].decode() if tokens else '-'} finish={finish[-1].decode() if finish else '-'}"


def describe_request(raw: bytes | None) -> str:
    """Name the model and the agent step's schema, so time can be attributed. Reads nothing else."""
    try:
        body = json.loads(raw) if raw else {}
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    nvext = body.get("nvext") if isinstance(body.get("nvext"), dict) else {}
    guided = body.get("guided_json") or nvext.get("guided_json") or {}
    response_format = body.get("response_format") if isinstance(body.get("response_format"), dict) else {}
    json_schema = response_format.get("json_schema") if isinstance(response_format.get("json_schema"), dict) else {}
    schema = (guided.get("title") if isinstance(guided, dict) else None) or json_schema.get("name")
    return f"model={_safe_label(body.get('model'))} schema={_safe_label(schema)}"


def _safe_label(value: Any) -> str:
    return value if isinstance(value, str) and _LABEL.fullmatch(value) else "-"


def rewrite_body(body: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Return the body with guided keys moved out of `nvext`, and whether anything moved."""
    nvext = body.get("nvext")
    if not isinstance(nvext, dict) or not any(key in nvext for key in _MOVED_KEYS):
        return body, False
    for key in _MOVED_KEYS:
        if key in nvext:
            body[key] = nvext.pop(key)
    if not nvext:
        del body["nvext"]
    return body, True


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def make_server(host: str, port: int, upstream: str) -> ThreadingHTTPServer:
    parts = urlsplit(upstream)
    origin = f"{parts.scheme}://{parts.netloc}"
    prefix = parts.path.rstrip("/")
    opener = urllib.request.build_opener(_NoRedirect)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            self._forward(None)

        def do_POST(self) -> None:
            self._forward(self.rfile.read(int(self.headers.get("content-length", 0))))

        def _forward(self, raw: bytes | None) -> None:
            data, rewritten = _rewrite_raw(raw)
            # Clients are configured with the upstream's own path, so only the origin changes.
            path = self.path if self.path.startswith(prefix) else prefix + self.path
            headers = {k: v for k, v in self.headers.items() if k.lower() not in _DROPPED_REQUEST_HEADERS}
            request = urllib.request.Request(origin + path, data=data, headers=headers, method=self.command)
            started = time.monotonic()
            try:
                response = opener.open(request, timeout=_TIMEOUT_SECONDS)
            except urllib.error.HTTPError as error:
                response = error
            except OSError as error:
                self._send_bad_gateway(error)
                return
            with response:
                self._relay(response, rewritten, started, raw)

        def _relay(self, response: Any, rewritten: bool, started: float, raw: bytes | None) -> None:
            status = response.status if hasattr(response, "status") else response.code
            seen = bytearray()
            client_gone = False
            try:
                self.send_response(status)
                for key, value in response.headers.items():
                    if key.lower() not in _DROPPED_RESPONSE_HEADERS:
                        self.send_header(key, value)
                # Chunked so streamed completions keep flowing instead of buffering to the end.
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
            except ConnectionError:
                client_gone = True
            while chunk := response.read1(_CHUNK) if hasattr(response, "read1") else response.read(_CHUNK):
                seen += chunk
                if not client_gone:
                    client_gone = not self._send_chunk(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
            if not client_gone:
                client_gone = not self._send_chunk(b"0\r\n\r\n")
            if client_gone:
                self.close_connection = True
            print(
                f"{self.command} {self.path.split('?')[0]} {status} {describe_request(raw)} rewritten={rewritten} "
                f"seconds={time.monotonic() - started:.1f} request_bytes={len(raw or b'')} "
                f"{usage_summary(bytes(seen))} client_gone={client_gone}",
                flush=True,
            )

        def _send_chunk(self, data: bytes) -> bool:
            try:
                self.wfile.write(data)
                self.wfile.flush()
            except ConnectionError:
                return False
            return True

        def _send_bad_gateway(self, error: OSError) -> None:
            print(f"{self.command} {self.path.split('?')[0]} 502 {type(error).__name__}", flush=True)
            body = json.dumps({"detail": f"llm-proxy could not reach upstream: {type(error).__name__}"}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            pass  # The default access log would print the full path and query.

    return ThreadingHTTPServer((host, port), Handler)


def _rewrite_raw(raw: bytes | None) -> tuple[bytes | None, bool]:
    if not raw:
        return raw, False
    try:
        body = json.loads(raw)
    except ValueError:
        return raw, False
    if not isinstance(body, dict):
        return raw, False
    rewritten_body, rewritten = rewrite_body(body)
    return (json.dumps(rewritten_body).encode() if rewritten else raw), rewritten


if __name__ == "__main__":
    make_server("0.0.0.0", int(os.environ.get("LLM_PROXY_PORT", "8080")), os.environ["LLM_PROXY_UPSTREAM"]).serve_forever()
