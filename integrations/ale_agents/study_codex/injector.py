"""Transparent Responses proxy that injects the frozen OpenRouter route.

The proxy changes only ``model`` and ``provider`` in JSON request bodies.  It
does not translate protocols, retry, impose a timeout, or modify responses.
The audit log intentionally stores request shape rather than prompts/images.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import urlsplit


HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _audit_shape(payload: dict, body: bytes) -> dict:
    tools = payload.get("tools") if isinstance(payload.get("tools"), list) else []
    inputs = payload.get("input") if isinstance(payload.get("input"), list) else []
    return {
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "body_bytes": len(body),
        "model": payload.get("model"),
        "provider": payload.get("provider"),
        "reasoning": payload.get("reasoning"),
        "max_output_tokens_present": "max_output_tokens" in payload,
        "max_output_tokens": payload.get("max_output_tokens"),
        "tool_count": len(tools),
        "tool_types": [item.get("type") for item in tools if isinstance(item, dict)],
        "tool_names": [
            item.get("name") or (item.get("function") or {}).get("name")
            for item in tools if isinstance(item, dict)
        ],
        "input_types": [item.get("type") for item in inputs if isinstance(item, dict)],
    }


def inject_request(incoming: bytes, *, model: str, provider: dict) -> tuple[bytes, dict]:
    """Apply exactly the two disclosed request mutations."""
    original = json.loads(incoming)
    if not isinstance(original, dict):
        raise ValueError("JSON body must be an object")
    modified = dict(original)
    modified["model"] = model
    modified["provider"] = provider
    assert {
        key: value for key, value in modified.items() if key not in {"model", "provider"}
    } == {
        key: value for key, value in original.items() if key not in {"model", "provider"}
    }
    forwarded = json.dumps(modified, separators=(",", ":")).encode()
    return forwarded, modified


def offline_self_test() -> None:
    original = {
        "model": "gpt-6-astra",
        "input": [{"type": "message", "content": "private prompt"}],
        "reasoning": {"effort": "high"},
        "tools": [{"type": "function", "name": "shell"}],
        "stream": True,
    }
    provider = {
        "only": ["openai"], "allow_fallbacks": False, "require_parameters": False,
    }
    forwarded, modified = inject_request(
        json.dumps(original).encode(), model="openai/gpt-6-astra", provider=provider
    )
    assert modified["model"] == "openai/gpt-6-astra"
    assert modified["provider"] == provider
    assert modified["reasoning"] == original["reasoning"]
    assert modified["tools"] == original["tools"]
    assert "max_output_tokens" not in modified
    assert b"private prompt" in forwarded


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, value: dict) -> None:
        with self.lock, self.path.open("a", encoding="utf-8") as target:
            target.write(json.dumps(value, sort_keys=True) + "\n")


def handler_factory(*, upstream: str, route: dict, audit: AuditLog):
    parsed = urlsplit(upstream)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("injector upstream must be an http(s) URL")
    prefix = parsed.path.rstrip("/")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self.send_response(204)
                self.end_headers()
                return
            self._forward()

        def do_POST(self) -> None:  # noqa: N802
            self._forward()

        def _forward(self) -> None:
            started = _utc_now()
            raw = self.rfile.read(int(self.headers.get("content-length", "0")))
            forwarded = raw
            request_audit: dict = {}
            try:
                if raw:
                    forwarded, payload = inject_request(
                        raw, model=route["model"], provider=route["provider"]
                    )
                    request_audit = _audit_shape(payload, forwarded)

                headers = {
                    key: value for key, value in self.headers.items()
                    if key.lower() not in HOP_BY_HOP
                }
                headers["Content-Length"] = str(len(forwarded))
                connection_cls = (
                    http.client.HTTPSConnection
                    if parsed.scheme == "https" else http.client.HTTPConnection
                )
                connection = connection_cls(
                    parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
                )
                connection.request(self.command, prefix + self.path, body=forwarded, headers=headers)
                response = connection.getresponse()
                self.send_response(response.status, response.reason)
                for key, value in response.getheaders():
                    if key.lower() not in HOP_BY_HOP:
                        self.send_header(key, value)
                self.send_header("Connection", "close")
                self.end_headers()
                while chunk := response.read(65536):
                    self.wfile.write(chunk)
                    self.wfile.flush()
                audit.write({
                    "event": "request_completed",
                    "started_at": started,
                    "finished_at": _utc_now(),
                    "path": self.path,
                    "status": response.status,
                    "openrouter_generation_id": response.getheader("x-generation-id"),
                    "openrouter_request_id": response.getheader("x-request-id"),
                    **request_audit,
                })
                connection.close()
            except Exception as exc:  # noqa: BLE001
                audit.write({
                    "event": "injector_error", "started_at": started,
                    "finished_at": _utc_now(), "path": self.path,
                    "error_type": type(exc).__name__, "error": str(exc),
                    **request_audit,
                })
                if not self.wfile.closed:
                    try:
                        self.send_error(502, "OpenRouter injector upstream failure")
                    except Exception:  # noqa: BLE001
                        pass

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--upstream", required=True)
    args = parser.parse_args()
    route = json.loads(Path(args.config).read_text(encoding="utf-8"))
    server = ThreadingHTTPServer(
        ("127.0.0.1", args.port),
        handler_factory(upstream=args.upstream, route=route, audit=AuditLog(Path(args.log))),
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
