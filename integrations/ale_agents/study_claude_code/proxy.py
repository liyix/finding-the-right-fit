"""Transparent Anthropic Messages body injector used by Claude Code.

The proxy adds exactly one top-level ``provider`` field, performs no retries,
and streams the upstream response without protocol translation.  Its audit log
contains request structure and hashes, never prompts, headers, or secrets.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
from pathlib import Path
from typing import Any


_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def _headers(raw: tuple[tuple[bytes, bytes], ...]) -> list[tuple[str, str]]:
    # Keep ordering and duplicate headers; only hop-by-hop/framing headers are
    # removed because aiohttp must regenerate them for the current connection.
    return [
        (name.decode("latin-1"), value.decode("latin-1"))
        for name, value in raw
        if name.decode("latin-1").lower() not in _HOP_HEADERS
        and name.decode("latin-1").lower() not in {"host", "content-length"}
    ]


def _structure(body: dict[str, Any]) -> dict[str, Any]:
    tools = body.get("tools") if isinstance(body.get("tools"), list) else []
    messages = body.get("messages") if isinstance(body.get("messages"), list) else []
    types: dict[str, int] = {}
    for message in messages:
        role = str(message.get("role")) if isinstance(message, dict) else "unknown"
        content = message.get("content") if isinstance(message, dict) else None
        blocks = content if isinstance(content, list) else []
        for block in blocks:
            kind = block.get("type") if isinstance(block, dict) else type(block).__name__
            key = f"{role}:{kind}"
            types[key] = types.get(key, 0) + 1
    return {
        "model": body.get("model"),
        "message_content_types": types,
        "tool_count": len(tools),
        "tool_names": [item.get("name") for item in tools if isinstance(item, dict)],
        "stream": body.get("stream"),
        "max_tokens": body.get("max_tokens"),
        "thinking": body.get("thinking"),
        "output_config": body.get("output_config"),
        "context_management": body.get("context_management"),
    }


def inject_provider(
    incoming: bytes, *, model: str, route: dict[str, Any]
) -> tuple[bytes, dict[str, Any]]:
    """Validate and inject the sole allowed semantic request mutation."""
    original = json.loads(incoming)
    if not isinstance(original, dict):
        raise ValueError("Messages body is not an object")
    if original.get("model") != model:
        raise ValueError(f"expected model {model!r}, got {original.get('model')!r}")
    if "provider" in original:
        raise ValueError("incoming request already contains provider")
    modified = copy.deepcopy(original)
    modified["provider"] = route
    forwarded = _canonical(modified)
    assert {key: value for key, value in modified.items() if key != "provider"} == original
    return forwarded, _structure(original)


class ProviderInjector:
    def __init__(
        self, *, model: str, route: dict[str, Any], upstream: str, log_path: Path
    ) -> None:
        self.model = model
        self.route = route
        self.upstream = upstream.rstrip("/")
        self.log_path = log_path
        self.session: Any = None
        self._lock = asyncio.Lock()
        self._sequence = 0

    async def startup(self, _app: Any) -> None:
        import aiohttp

        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(
                total=None, connect=30, sock_connect=30, sock_read=None
            ),
            auto_decompress=False,
            trust_env=False,
        )

    async def cleanup(self, _app: Any) -> None:
        if self.session is not None:
            await self.session.close()

    async def _log(self, record: dict[str, Any]) -> None:
        async with self._lock:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as target:
                target.write(json.dumps(record, sort_keys=True) + "\n")
            self.log_path.chmod(0o600)

    async def handle(self, request: Any) -> Any:
        from aiohttp import web

        started = time.monotonic()
        self._sequence += 1
        incoming = await request.read()
        forwarded = incoming
        changed: list[str] = []
        structure: dict[str, Any] | None = None
        error: str | None = None
        model: str | None = None
        if request.method == "POST" and request.path == "/v1/messages":
            try:
                original = json.loads(incoming)
                model = original.get("model") if isinstance(original, dict) else None
                forwarded, structure = inject_provider(
                    incoming, model=self.model, route=self.route
                )
                changed = ["provider"]
            except Exception as exc:  # request gate; no upstream attempt
                await self._log({
                    "sequence": self._sequence,
                    "method": request.method,
                    "path": request.path,
                    "upstream_attempted": False,
                    "error": f"{type(exc).__name__}: {exc}",
                })
                return web.json_response(
                    {"error": "provider injector rejected request"}, status=400
                )

        status = 502
        response_bytes = 0
        try:
            async with self.session.request(
                request.method,
                self.upstream + str(request.rel_url),
                data=forwarded if request.method not in {"GET", "HEAD"} else None,
                headers=_headers(request.raw_headers),
                allow_redirects=False,
            ) as upstream:
                status = upstream.status
                response = web.StreamResponse(
                    status=status,
                    reason=upstream.reason,
                    headers=_headers(upstream.raw_headers),
                )
                await response.prepare(request)
                async for chunk in upstream.content.iter_chunked(65536):
                    response_bytes += len(chunk)
                    await response.write(chunk)
                await response.write_eof()
                return response
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            return web.json_response({"error": "upstream request failed"}, status=502)
        finally:
            await self._log({
                "sequence": self._sequence,
                "method": request.method,
                "path": request.path,
                "model": model,
                "request_structure": structure,
                "provider_route": self.route if changed else None,
                "changed_fields": changed,
                "semantic_parity_except_provider": changed == ["provider"],
                "incoming_body_sha256": hashlib.sha256(incoming).hexdigest(),
                "forwarded_body_sha256": hashlib.sha256(forwarded).hexdigest(),
                "upstream_status": status,
                "response_bytes": response_bytes,
                "elapsed_seconds": round(time.monotonic() - started, 6),
                "injector_retry_count": 0,
                "error": error,
            })


def make_app(
    *, model: str, route: dict[str, Any], upstream: str, log_path: Path
) -> Any:
    from aiohttp import web

    injector = ProviderInjector(
        model=model, route=route, upstream=upstream, log_path=log_path
    )
    app = web.Application(client_max_size=64 * 1024**2)
    app.on_startup.append(injector.startup)
    app.on_cleanup.append(injector.cleanup)
    app.router.add_route("*", "/{tail:.*}", injector.handle)
    return app


def offline_self_test() -> None:
    """Prove the sole body mutation without opening sockets or calling an API."""
    route = {"only": ["anthropic"], "allow_fallbacks": False, "require_parameters": True}
    original = {
        "model": "anthropic/claude-opus-5",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "private prompt"}]}],
        "tools": [{"name": "Bash", "input_schema": {"type": "object"}}],
        "stream": True,
        "max_tokens": 64_000,
        "output_config": {"effort": "high"},
    }
    incoming = _canonical(original)
    encoded, structure = inject_provider(
        incoming, model="anthropic/claude-opus-5", route=route
    )
    forwarded = json.loads(encoded)
    assert forwarded.pop("provider") == route
    assert forwarded == original
    assert structure["tool_names"] == ["Bash"]
    assert structure["max_tokens"] == 64_000
    assert "private prompt" not in json.dumps(structure)
