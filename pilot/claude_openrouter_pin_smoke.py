#!/usr/bin/env python3
"""Claude Code -> OpenRouter provider-pinning tool-loop pilot.

The local gateway preserves Anthropic Messages on both sides and changes exactly
one top-level request field: it adds the frozen OpenRouter ``provider`` object.
It neither retries nor translates requests, tools, thinking blocks, or SSE.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Any

import provider_smoke as base
import provider_tool_smoke as tool_base


CAMPAIGN_ID = "pilot-openrouter-claude-code-body-injector-tools-20260830-r1"
PREPARED_AT_UTC = "2026-08-30T14:18:00Z"
SOFT_LIMIT_USD = 3.00
MAX_OUTPUT_TOKENS = 1024
MAX_AGENT_TURNS = 4
CELL_TIMEOUT_SECONDS = 300
INJECTOR_PORT = 4002
INJECTOR_URL = f"http://127.0.0.1:{INJECTOR_PORT}"
UPSTREAM_URL = "https://openrouter.ai/api"
MARKER = tool_base.MARKER
PROMPT = tool_base.PROMPT

TARGET_MODELS: dict[str, dict[str, Any]] = {
    "claude-opus-5": base.MODELS["claude-opus-5"],
    **tool_base.PRESET_MODELS,
}

ORIGINAL_BUILD_MANIFEST = base.build_manifest
ORIGINAL_PARSE_TELEMETRY = base.parse_harness_telemetry
ORIGINAL_ASSISTANT_MARKER_SEEN = base.assistant_marker_seen

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
REQUEST_BODY_PATHS = {"/v1/messages"}


def build_cells() -> list[dict[str, Any]]:
    cells: list[dict[str, Any]] = []
    for logical_model, model in TARGET_MODELS.items():
        strict_id = f"openrouter-body-pin-strict--{logical_model}--claude-code"
        variants = [("strict", copy.deepcopy(model["openrouter_route"]), [])]
        if logical_model != "claude-opus-5":
            relaxed_route = copy.deepcopy(model["openrouter_route"])
            relaxed_route["require_parameters"] = False
            variants.append(("pinned-no-parameter-filter", relaxed_route, [strict_id]))
        for variant, route, prior_failures in variants:
            cell_id = f"openrouter-body-pin-{variant}--{logical_model}--claude-code"
            cells.append(
                {
                    "cell_id": cell_id,
                    "channel": "openrouter",
                    "harness": "claude-code",
                    "harness_version": "2.1.251",
                    "logical_model": logical_model,
                    "actual_model": model["openrouter_model"],
                    "provider": "openrouter",
                    "key_env": "OPENROUTER_API_KEY",
                    "protocol": "anthropic-messages",
                    "translated": False,
                    "translation": None,
                    "proxy_required": False,
                    "body_injector_required": True,
                    "compatibility_variant": (
                        "transparent-provider-body-injector-"
                        + ("strict" if variant == "strict" else "without-parameter-filter")
                    ),
                    "compatibility_drop_params": [],
                    "run_only_if_all_prior_failed": prior_failures,
                    "base_url": UPSTREAM_URL,
                    "anthropic_base_url_override": INJECTOR_URL,
                    "openrouter_route": route,
                    "route_control": (
                        "provider.only + quantization (when declared) + "
                        "allow_fallbacks=false"
                        + (" + require_parameters=true" if variant == "strict" else "")
                    ),
                    "expected_endpoint": model["endpoint"],
                    "reasoning_effort": "high",
                    "hard_output_limit": True,
                }
            )
    return cells


def docker_command(cell_dir: Path, *, validate: bool) -> list[str]:
    command = [
        "docker",
        "run",
        "--rm",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--cpus",
        "2",
        "--memory",
        "4g",
        "-e",
        "OPENROUTER_API_KEY",
        "-e",
        "HOME=/trial/home",
        "-e",
        "PI_TELEMETRY=0",
        "-e",
        "DSH_TELEMETRY_MODE=DISABLED",
        "-v",
        f"{base.ROOT / 'pilot'}:/pilot:ro",
        "-v",
        f"{cell_dir}:/trial",
        "-w",
        "/trial/workspace",
        base.IMAGE,
        "/opt/openhands/bin/python",
        "/pilot/claude_openrouter_pin_smoke.py",
        "_cell",
        "--spec",
        "/trial/spec.json",
    ]
    if validate:
        command.append("--validate")
    return command


def build_manifest() -> dict[str, Any]:
    manifest = ORIGINAL_BUILD_MANIFEST()
    manifest.pop("manifest_sha256", None)
    cells = build_cells()
    manifest.update(
        {
            "campaign_id": CAMPAIGN_ID,
            "kind": "paid-claude-code-openrouter-transparent-provider-pinning-pilot",
            "prepared_at_utc": PREPARED_AT_UTC,
            "cells": cells,
        }
    )
    manifest["scope"].update(
        {
            "benchmark": "none-openrouter-anthropic-messages-terminal-tool-loop-smoke",
            "dataset": None,
            "task_subset": (
                "one fixed synthetic terminal tool loop for each of the five formal target models"
            ),
            "replicate": 1,
            "planned_trials_maximum": len(cells),
            "planned_trials_minimum": len(TARGET_MODELS),
            "conditional_stop": (
                "for each non-Claude model, skip the no-parameter-filter variant when its "
                "strict require_parameters=true variant passes"
            ),
            "concurrency": 1,
            "prompt": PROMPT,
            "acceptance": (
                "successful Claude Code exit, exact terminal artifact and final marker, "
                "injector audit proving provider was the sole semantic body change, and "
                "OpenRouter generation metadata proving the requested endpoint was selected"
            ),
        }
    )
    manifest["runner"].update(
        {
            "entry_command": (
                "python pilot/claude_openrouter_pin_smoke.py run "
                "--approved-manifest-sha256 <sha256>"
            ),
            "execution": (
                "Claude Code 2.1.251 plus a local transparent Anthropic Messages body injector "
                "in the existing pinned pilot image; no benchmark runner"
            ),
            "integration_type": (
                "standalone compatibility pilot; the injector is a compatibility layer, "
                "not a Harbor adapter or ALE deployer"
            ),
            "cell_timeout_seconds": CELL_TIMEOUT_SECONDS,
        }
    )
    manifest["runner"]["harness_install_and_entry"] = {
        "claude-code": {
            "source": "npm @anthropic-ai/claude-code@2.1.251",
            "entry": "claude --print --output-format=stream-json --max-turns 4",
        }
    }
    manifest["controls"].update(
        {
            "reasoning_effort": "high, matching the frozen formal experiment control",
            "reasoning_summary": "Claude Code/OpenRouter model default",
            "thinking": (
                "Claude Code --effort high; outgoing thinking fields are preserved byte-for-field "
                "by the injector and audited but provider effectiveness remains empirical"
            ),
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "output_limit_location": "CLAUDE_CODE_MAX_OUTPUT_TOKENS=1024",
            "max_turns_or_iterations": "Claude Code --max-turns 4",
            "harness_budget_management": (
                "Claude Code --max-budget-usd 0.25 per cell; pilot safety cap only"
            ),
            "timeout_seconds": CELL_TIMEOUT_SECONDS,
            "retries": {
                "claude_code": (
                    "native default retained because no supported retry-disable control exists; "
                    "the injector performs zero retries and every upstream request is logged"
                ),
                "injector": 0,
                "outer_trial": 0,
            },
            "temperature": "Claude Code/provider default",
            "seed": None,
            "context_window": (
                "no CLAUDE_CODE_MAX_CONTEXT_TOKENS override; Claude Code 2.1.251 unknown-model "
                "metadata/default compaction behavior remains and runtime contextWindow is logged"
            ),
            "compaction": (
                "Claude Code default; no compaction expected in this short smoke and no claim "
                "about formal benchmark recovery is made"
            ),
            "tools": (
                "Claude Code installed defaults; fixed prompt requests exactly one Bash call and "
                "the injector preserves the complete tools array"
            ),
            "skills": "no project skills; empty isolated HOME/workspace",
            "mcp": [],
            "subagents": "Claude Code default availability; forbidden by the fixed prompt",
            "memory": "empty isolated HOME",
            "custom_prompt_files": [],
            "benchmark_specific_augmentation": False,
            "compatibility_only_changes": [
                "ANTHROPIC_BASE_URL points to localhost instead of OpenRouter directly",
                "the localhost injector adds exactly one top-level provider object frozen in the cell spec",
                "the original model ID, messages, system, tools, thinking, metadata, and all other JSON fields are preserved",
                "Anthropic headers are forwarded, response status/headers/body and SSE chunks are streamed without protocol translation",
                "the injector has no retry, fallback, model rewrite, prompt, tool, context, compaction, or reasoning logic",
                "strict variants retain require_parameters=true; a pre-registered diagnostic fallback only disables that filter while retaining provider.only, quantization, and allow_fallbacks=false",
            ],
        }
    )
    manifest["budget"].update(
        {
            "soft_limit_usd_per_key": {"OPENROUTER_API_KEY": SOFT_LIMIT_USD},
            "unknown_call_reserve_usd": 0.10,
            "stop_policy": (
                "sequential pre-call ledger; variants conditional on prior status; stop future "
                "cells when the OpenRouter soft ledger reaches $3.00"
            ),
            "hard_provider_cap": False,
            "expected_incremental_total_usd": (
                "approximately $0.25-$1.25; absolute campaign soft stop $3.00; failed "
                "pre-generation routing attempts normally cost $0 but are still audited"
            ),
        }
    )
    manifest["accounting"].update(
        {
            "preserve_sources": [
                "OpenRouter current-key usage before/after campaign",
                "OpenRouter response router metadata and per-generation API records",
                "Claude Code native stream-json usage/cost",
                "transparent injector one-record-per-upstream-request audit log",
            ],
            "aggregation_rule": "never add duplicate reports of the same request",
            "provider_cost": (
                "OpenRouter generation/current-key accounting is primary; Claude Code copies "
                "are retained source-qualified and not summed"
            ),
        }
    )
    manifest["comparison"] = {
        "matched_models": list(TARGET_MODELS),
        "matched_harnesses": ["claude-code"],
        "outcomes": [
            "strict Anthropic Messages provider/quantization routing",
            "terminal tool serialization, execution, result return, and second-round completion",
            "request-field parity before and after injection",
            "multi-request thinking/tool continuity observable in this short loop",
            "actual model/provider identity, retries, tokens, cost, and wall time",
        ],
        "scope_note": (
            "Claude Opus is a known-good injector control; GPT, GLM, Kimi, and DeepSeek are "
            "the four target cross-family paths"
        ),
        "quality_claim": (
            "none; this is a transport/tool-loop compatibility test, not a benchmark score "
            "or proof of long-context compaction correctness"
        ),
    }
    manifest["routing"] = {
        "mechanism": (
            "OpenRouter Anthropic Messages request-body provider object inserted by a localhost "
            "transparent injector because Claude Code exposes custom gateway headers but no "
            "arbitrary request-body extension setting"
        ),
        "strict_cells": (
            "all strict cells use provider.only, allow_fallbacks=false, require_parameters=true, "
            "and the endpoint quantization when OpenRouter declares one"
        ),
        "diagnostic_cells": (
            "only after a strict failure, retry the same exact model/provider/quantization with "
            "require_parameters=false; this remains pinned but is separately classified"
        ),
        "endpoint_snapshot_source": "OpenRouter authenticated endpoint metadata retained from prior pilot",
        "models": {
            model_id: {
                "model": model["openrouter_model"],
                "strict_route": model["openrouter_route"],
                "endpoint": model["endpoint"],
            }
            for model_id, model in TARGET_MODELS.items()
        },
    }
    manifest["injector_audit"] = {
        "request_paths_modified": sorted(REQUEST_BODY_PATHS),
        "request_fields_added": ["provider"],
        "request_fields_removed_or_rewritten": [],
        "header_values_logged": False,
        "request_or_response_contents_logged": False,
        "semantic_parity_check": (
            "the parsed forwarded body with provider removed must equal the parsed incoming body"
        ),
        "response_handling": "status, safe headers, and raw response/SSE bytes streamed unchanged",
        "offline_self_test": (
            "requires exact nested-body parity, Anthropic header preservation, exact SSE-byte "
            "parity, one provider insertion, and absence of a sentinel bearer secret in logs"
        ),
    }
    manifest["evidence_sources"] = [
        "https://openrouter.ai/docs/api/api-reference/anthropic-messages/create-messages",
        "https://openrouter.ai/docs/guides/routing/provider-selection",
        "https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration",
        "https://code.claude.com/docs/en/llm-gateway",
        "https://github.com/deepsteve/deepsteve/issues/499",
    ]
    manifest["source_sha256"]["pilot/provider_smoke.py"] = base.sha256_file(
        base.ROOT / "pilot" / "provider_smoke.py"
    )
    manifest["source_sha256"]["pilot/provider_tool_smoke.py"] = base.sha256_file(
        base.ROOT / "pilot" / "provider_tool_smoke.py"
    )
    manifest["source_sha256"]["pilot/claude_openrouter_pin_smoke.py"] = (
        base.sha256_file(Path(__file__))
    )
    manifest_hash = hashlib.sha256(base.canonical_json(manifest)).hexdigest()
    manifest["manifest_sha256"] = manifest_hash
    return manifest


def _filtered_request_headers(raw_headers: Any) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for raw_name, raw_value in raw_headers:
        name = raw_name.decode("latin-1")
        lower = name.lower()
        if lower in HOP_BY_HOP_HEADERS or lower in {"host", "content-length"}:
            continue
        result.append((name, raw_value.decode("latin-1")))
    return result


def _filtered_response_headers(raw_headers: Any) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for raw_name, raw_value in raw_headers:
        name = raw_name.decode("latin-1")
        if name.lower() in HOP_BY_HOP_HEADERS or name.lower() == "content-length":
            continue
        result.append((name, raw_value.decode("latin-1")))
    return result


def _request_structure(body: Any) -> dict[str, Any] | None:
    """Return an audit-safe summary without recording prompt or tool schemas."""
    if not isinstance(body, dict):
        return None

    content_types: dict[str, int] = {}
    thinking_blocks = 0
    signed_thinking_blocks = 0
    image_blocks = 0
    tool_result_blocks = 0
    messages = body.get("messages")
    if not isinstance(messages, list):
        messages = []
    roles: dict[str, int] = {}
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "missing")
        roles[role] = roles.get(role, 0) + 1
        content = message.get("content")
        if not isinstance(content, list):
            content = [content]
        for block in content:
            block_type = block.get("type") if isinstance(block, dict) else type(block).__name__
            key = f"{role}:{block_type or 'missing'}"
            content_types[key] = content_types.get(key, 0) + 1
            if not isinstance(block, dict):
                continue
            if block_type in {"thinking", "redacted_thinking"}:
                thinking_blocks += 1
                if bool(block.get("signature")):
                    signed_thinking_blocks += 1
            elif block_type == "image":
                image_blocks += 1
            elif block_type == "tool_result":
                tool_result_blocks += 1
                result_content = block.get("content")
                if not isinstance(result_content, list):
                    result_content = [result_content]
                image_blocks += sum(
                    isinstance(item, dict) and item.get("type") == "image"
                    for item in result_content
                )

    tools = body.get("tools")
    if not isinstance(tools, list):
        tools = []
    tool_names = sorted(
        str(tool.get("name"))
        for tool in tools
        if isinstance(tool, dict) and tool.get("name")
    )
    system = body.get("system")
    if not isinstance(system, list):
        system = [system] if system is not None else []
    return {
        "top_level_keys": sorted(body),
        "message_count": len(messages),
        "message_roles": roles,
        "message_content_types": content_types,
        "thinking_blocks": thinking_blocks,
        "signed_thinking_blocks": signed_thinking_blocks,
        "image_blocks": image_blocks,
        "tool_result_blocks": tool_result_blocks,
        "tool_count": len(tools),
        "tool_names": tool_names,
        "system_block_count": len(system),
        "metadata_keys": sorted(body.get("metadata", {}))
        if isinstance(body.get("metadata"), dict)
        else [],
        "stream": body.get("stream"),
        "max_tokens": body.get("max_tokens"),
        "thinking": body.get("thinking"),
        "output_config": body.get("output_config"),
        "context_management": body.get("context_management"),
    }


class ProviderInjector:
    def __init__(
        self,
        spec: dict[str, Any],
        upstream: str,
        log_path: Path,
        *,
        max_in_flight: int | None = None,
    ):
        if max_in_flight is not None and max_in_flight <= 0:
            raise ValueError("max_in_flight must be positive when set")
        self.spec = spec
        self.upstream = upstream.rstrip("/")
        self.log_path = log_path
        self.session: Any = None
        self.sequence = 0
        self.log_lock = asyncio.Lock()
        self.max_in_flight = max_in_flight
        self.request_gate = (
            asyncio.Semaphore(max_in_flight) if max_in_flight is not None else None
        )
        self.active_requests = 0
        self.active_high_water = 0
        self.active_lock = asyncio.Lock()

    async def startup(self, app: Any) -> None:
        import aiohttp

        self.session = aiohttp.ClientSession(
            # A high-reasoning benchmark response can stream for well over four
            # minutes. Limit only connection establishment; do not truncate a
            # healthy stream after a fixed wall-clock total.
            timeout=aiohttp.ClientTimeout(
                total=None,
                connect=30,
                sock_connect=30,
                sock_read=None,
            ),
            auto_decompress=False,
            trust_env=False,
        )

    async def cleanup(self, app: Any) -> None:
        if self.session is not None:
            await self.session.close()

    async def _write_log(self, record: dict[str, Any]) -> None:
        async with self.log_lock:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
            self.log_path.chmod(0o600)

    async def handle(self, request: Any) -> Any:
        from aiohttp import web

        started = time.monotonic()
        started_at_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.sequence += 1
        sequence = self.sequence
        incoming = await request.read()
        forwarded = incoming
        changed_fields: list[str] = []
        model: str | None = None
        semantic_parity = True
        error: str | None = None
        request_structure: dict[str, Any] | None = None

        if request.method == "POST" and request.path in REQUEST_BODY_PATHS:
            try:
                original = json.loads(incoming)
                if not isinstance(original, dict):
                    raise ValueError("Messages body is not an object")
                request_structure = _request_structure(original)
                model = original.get("model")
                if model != self.spec["actual_model"]:
                    raise ValueError(
                        f"model mismatch: expected {self.spec['actual_model']!r}, got {model!r}"
                    )
                if "provider" in original:
                    raise ValueError("incoming request already contains provider")
                modified = copy.deepcopy(original)
                modified["provider"] = self.spec["openrouter_route"]
                semantic_parity = {k: v for k, v in modified.items() if k != "provider"} == original
                if not semantic_parity:
                    raise ValueError("non-provider request field changed")
                forwarded = base.canonical_json(modified)
                changed_fields = ["provider"]
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                await self._write_log(
                    {
                        "sequence": sequence,
                        "method": request.method,
                        "path": request.path,
                        "error": error,
                        "upstream_attempted": False,
                    }
                )
                return web.json_response({"error": "provider injector rejected request"}, status=400)

        request_headers = _filtered_request_headers(request.raw_headers)
        client_retry_header = request.headers.get("x-stainless-retry-count")
        client_session_id = request.headers.get("x-claude-code-session-id")
        upstream_url = self.upstream + str(request.rel_url)
        status = 502
        response_bytes = 0
        queue_wait_seconds = 0.0
        active_at_start: int | None = None
        gate_acquired = False
        try:
            if self.request_gate is not None:
                queued_at = time.monotonic()
                await self.request_gate.acquire()
                gate_acquired = True
                queue_wait_seconds = time.monotonic() - queued_at
            async with self.active_lock:
                self.active_requests += 1
                active_at_start = self.active_requests
                self.active_high_water = max(
                    self.active_high_water, self.active_requests
                )
            async with self.session.request(
                request.method,
                upstream_url,
                data=forwarded if request.method not in {"GET", "HEAD"} else None,
                headers=request_headers,
                allow_redirects=False,
            ) as upstream_response:
                status = upstream_response.status
                response = web.StreamResponse(
                    status=upstream_response.status,
                    reason=upstream_response.reason,
                    headers=_filtered_response_headers(upstream_response.raw_headers),
                )
                await response.prepare(request)
                async for chunk in upstream_response.content.iter_chunked(65536):
                    response_bytes += len(chunk)
                    await response.write(chunk)
                await response.write_eof()
                return response
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            return web.json_response({"error": "upstream request failed"}, status=502)
        finally:
            if active_at_start is not None:
                async with self.active_lock:
                    self.active_requests -= 1
            if gate_acquired and self.request_gate is not None:
                self.request_gate.release()
            await self._write_log(
                {
                    "sequence": sequence,
                    "started_at_utc": started_at_utc,
                    "finished_at_utc": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                    ),
                    "method": request.method,
                    "path": request.path,
                    "model": model,
                    "request_structure": request_structure,
                    "client_retry_header": client_retry_header,
                    "client_session_sha256": hashlib.sha256(
                        client_session_id.encode("utf-8")
                    ).hexdigest()
                    if client_session_id
                    else None,
                    "provider_route": self.spec["openrouter_route"] if changed_fields else None,
                    "changed_fields": changed_fields,
                    "semantic_parity_except_provider": semantic_parity,
                    "incoming_body_sha256": hashlib.sha256(incoming).hexdigest(),
                    "forwarded_body_sha256": hashlib.sha256(forwarded).hexdigest(),
                    "forwarded_header_names": sorted({name.lower() for name, _ in request_headers}),
                    "upstream_status": status,
                    "response_bytes": response_bytes,
                    "request_gate_max_in_flight": self.max_in_flight,
                    "request_gate_queue_wait_seconds": round(
                        queue_wait_seconds, 6
                    ),
                    "request_gate_active_at_start": active_at_start,
                    "request_gate_active_high_water": self.active_high_water,
                    "elapsed_seconds": round(time.monotonic() - started, 6),
                    "error": error,
                    "injector_retry_count": 0,
                    "retry_count": 0,
                }
            )


def make_proxy_app(
    spec: dict[str, Any],
    upstream: str,
    log_path: Path,
    *,
    max_in_flight: int | None = None,
) -> Any:
    from aiohttp import web

    injector = ProviderInjector(
        spec, upstream, log_path, max_in_flight=max_in_flight
    )
    app = web.Application(client_max_size=64 * 1024**2)
    app.on_startup.append(injector.startup)
    app.on_cleanup.append(injector.cleanup)
    app.router.add_route("*", "/{tail:.*}", injector.handle)
    return app


def run_proxy(
    spec_path: Path,
    host: str,
    port: int,
    upstream: str,
    log_path: Path,
    *,
    max_in_flight: int | None = None,
) -> int:
    from aiohttp import web

    spec = json.loads(spec_path.read_text())
    validate_spec(spec)
    web.run_app(
        make_proxy_app(
            spec, upstream, log_path, max_in_flight=max_in_flight
        ),
        host=host,
        port=port,
        print=None,
        handle_signals=True,
    )
    return 0


def validate_spec(spec: dict[str, Any]) -> None:
    logical_model = spec.get("logical_model")
    if logical_model not in TARGET_MODELS:
        raise RuntimeError(f"unknown target model {logical_model!r}")
    if spec.get("actual_model") != TARGET_MODELS[logical_model]["openrouter_model"]:
        raise RuntimeError("actual model mismatch")
    if spec.get("harness") != "claude-code" or spec.get("protocol") != "anthropic-messages":
        raise RuntimeError("unexpected harness or protocol")
    if spec.get("translated") is not False or spec.get("body_injector_required") is not True:
        raise RuntimeError("injector classification mismatch")
    override = urllib.parse.urlsplit(str(spec.get("anthropic_base_url_override", "")))
    if (
        override.scheme != "http"
        or override.hostname not in {"127.0.0.1", "localhost", "host.docker.internal"}
        or override.port is None
        or override.path not in {"", "/"}
        or override.query
        or override.fragment
    ):
        raise RuntimeError("local gateway URL must be an explicit loopback/host-gateway port")
    route = spec.get("openrouter_route")
    if not isinstance(route, dict) or not route.get("only"):
        raise RuntimeError("provider.only is required")
    if route.get("allow_fallbacks") is not False:
        raise RuntimeError("allow_fallbacks must be false")


def wait_for_port(process: subprocess.Popen[str], port: int) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("provider injector exited during startup")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("provider injector did not become live")


def run_cell(spec_path: Path, *, validate: bool) -> int:
    spec = json.loads(spec_path.read_text())
    validate_spec(spec)
    if validate:
        return base.inside_cell(spec_path, validate=True)
    artifact = Path("/trial/workspace/bridge-check.txt")
    if artifact.exists():
        raise RuntimeError("tool-smoke artifact unexpectedly pre-exists")
    process_log = Path("/trial/provider-injector-process.log").open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__)),
            "_proxy",
            "--spec",
            str(spec_path),
            "--host",
            "127.0.0.1",
            "--port",
            str(INJECTOR_PORT),
            "--upstream",
            UPSTREAM_URL,
            "--log-path",
            "/trial/provider-injector.jsonl",
        ],
        stdout=process_log,
        stderr=subprocess.STDOUT,
        text=True,
    )
    process_log.close()
    try:
        wait_for_port(process, INJECTOR_PORT)
        return base.inside_cell(spec_path, validate=False)
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def parse_harness_telemetry(harness: str, cell_dir: Path) -> dict[str, Any]:
    telemetry = tool_base.parse_harness_telemetry(harness, cell_dir)
    injector_records = base.parse_json_lines(cell_dir / "provider-injector.jsonl")
    telemetry["transparent_provider_injector"] = {
        "source": "provider-injector.jsonl",
        "request_count": len(injector_records),
        "messages_request_count": sum(
            item.get("path") in REQUEST_BODY_PATHS for item in injector_records
        ),
        "all_messages_changed_only_provider": bool(injector_records)
        and all(
            item.get("changed_fields") == ["provider"]
            and item.get("semantic_parity_except_provider") is True
            for item in injector_records
            if item.get("path") in REQUEST_BODY_PATHS
        ),
        "upstream_statuses": [item.get("upstream_status") for item in injector_records],
        "retry_count": sum(int(item.get("retry_count", 0)) for item in injector_records),
        "records": injector_records,
    }
    return telemetry


def assistant_marker_and_artifact_seen(
    harness: str, cell_dir: Path, telemetry: dict[str, Any]
) -> bool:
    injector = telemetry.get("transparent_provider_injector", {})
    return (
        tool_base.assistant_marker_and_artifact_seen(harness, cell_dir, telemetry)
        and injector.get("messages_request_count", 0) >= 2
        and injector.get("all_messages_changed_only_provider") is True
    )


async def offline_self_test_async() -> None:
    from aiohttp import ClientSession, web

    received: dict[str, Any] = {"bodies": [], "active": 0, "active_high_water": 0}
    response_bytes = (
        b"event: message_start\ndata: {\"type\":\"message_start\"}\n\n"
        b"event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n"
    )

    async def fake_upstream(request: Any) -> Any:
        received["bodies"].append(await request.json())
        received["headers"] = {k.lower(): v for k, v in request.headers.items()}
        received["active"] += 1
        received["active_high_water"] = max(
            received["active_high_water"], received["active"]
        )
        try:
            await asyncio.sleep(0.05)
            return web.Response(
                body=response_bytes, headers={"Content-Type": "text/event-stream"}
            )
        finally:
            received["active"] -= 1

    fake_app = web.Application()
    fake_app.router.add_post("/v1/messages", fake_upstream)
    fake_runner = web.AppRunner(fake_app)
    await fake_runner.setup()
    fake_site = web.TCPSite(fake_runner, "127.0.0.1", 0)
    await fake_site.start()
    fake_port = fake_site._server.sockets[0].getsockname()[1]

    spec = build_cells()[0]
    with tempfile.TemporaryDirectory(prefix="provider-injector-self-test-") as tmp:
        log_path = Path(tmp) / "injector.jsonl"
        proxy_runner = web.AppRunner(
            make_proxy_app(
                spec,
                f"http://127.0.0.1:{fake_port}",
                log_path,
                max_in_flight=1,
            )
        )
        await proxy_runner.setup()
        proxy_site = web.TCPSite(proxy_runner, "127.0.0.1", 0)
        await proxy_site.start()
        proxy_port = proxy_site._server.sockets[0].getsockname()[1]
        original = {
            "model": spec["actual_model"],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "self-test-prompt-must-not-be-logged",
                        }
                    ],
                }
            ],
            "system": [
                {"type": "text", "text": "self-test-system-must-not-be-logged"}
            ],
            "thinking": {"type": "enabled", "budget_tokens": 128},
            "tools": [
                {
                    "name": "Bash",
                    "description": "run",
                    "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}},
                }
            ],
            "stream": True,
            "max_tokens": 256,
        }
        sentinel = "Bearer self-test-secret-must-not-be-logged"
        async with ClientSession(auto_decompress=False) as session:
            async def send_one() -> tuple[int, bytes]:
                async with session.post(
                    f"http://127.0.0.1:{proxy_port}/v1/messages",
                    json=original,
                    headers={
                        "Authorization": sentinel,
                        "Anthropic-Version": "2023-06-01",
                        "Anthropic-Beta": "interleaved-thinking-2025-05-14",
                        "X-Stainless-Retry-Count": "2",
                        "X-Claude-Code-Session-Id": "self-test-session",
                    },
                ) as response:
                    return response.status, await response.read()

            responses = await asyncio.gather(send_one(), send_one())
            assert all(status == 200 for status, _ in responses)
        forwarded = received["bodies"][0]
        assert forwarded.pop("provider") == spec["openrouter_route"]
        assert forwarded == original
        assert received["headers"]["authorization"] == sentinel
        assert received["headers"]["anthropic-version"] == "2023-06-01"
        assert received["headers"]["anthropic-beta"] == "interleaved-thinking-2025-05-14"
        assert all(body == response_bytes for _, body in responses)
        assert received["active_high_water"] == 1
        records = base.parse_json_lines(log_path)
        assert len(records) == 2
        assert records[0]["changed_fields"] == ["provider"]
        assert records[0]["semantic_parity_except_provider"] is True
        assert records[0]["client_retry_header"] == "2"
        assert records[0]["client_session_sha256"] == hashlib.sha256(
            b"self-test-session"
        ).hexdigest()
        assert records[0]["request_structure"]["message_content_types"] == {
            "user:text": 1
        }
        assert records[0]["request_structure"]["tool_names"] == ["Bash"]
        assert all(item["request_gate_max_in_flight"] == 1 for item in records)
        assert max(item["request_gate_active_high_water"] for item in records) == 1
        assert max(item["request_gate_queue_wait_seconds"] for item in records) >= 0.04
        log_text = log_path.read_text()
        assert "self-test-secret-must-not-be-logged" not in log_text
        assert "self-test-prompt-must-not-be-logged" not in log_text
        assert "self-test-system-must-not-be-logged" not in log_text
        await proxy_runner.cleanup()
    await fake_runner.cleanup()


def offline_self_test() -> int:
    asyncio.run(offline_self_test_async())
    print("transparent provider injector offline self-test passed")
    return 0


def self_test_docker_command() -> list[str]:
    return [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "-v",
        f"{base.ROOT / 'pilot'}:/pilot:ro",
        base.IMAGE,
        "/opt/openhands/bin/python",
        "/pilot/claude_openrouter_pin_smoke.py",
        "self-test",
    ]


def host_preflight() -> int:
    completed = subprocess.run(
        self_test_docker_command(),
        cwd=base.ROOT,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=90,
    )
    if completed.returncode != 0:
        print(completed.stdout[-8000:])
        return 2
    print(completed.stdout.strip(), flush=True)
    return base.host_preflight()


def configure_base() -> None:
    base.CAMPAIGN_ID = CAMPAIGN_ID
    base.MANIFEST_PREPARED_AT_UTC = PREPARED_AT_UTC
    base.RUN_DIR = base.ROOT / "runs" / CAMPAIGN_ID
    base.REPORT_PATH = base.RUN_DIR / "report.md"
    base.MARKER = MARKER
    base.PROMPT = PROMPT
    base.HARNESSES = ("claude-code",)
    base.MODELS = TARGET_MODELS
    base.SOFT_LIMITS_USD = {"OPENROUTER_API_KEY": SOFT_LIMIT_USD}
    base.UNKNOWN_CALL_RESERVE_USD = 0.10
    base.MAX_OUTPUT_TOKENS = MAX_OUTPUT_TOKENS
    base.MAX_AGENT_TURNS = MAX_AGENT_TURNS
    base.CELL_TIMEOUT_SECONDS = CELL_TIMEOUT_SECONDS
    base.docker_command = docker_command
    base.build_cells = build_cells
    base.build_manifest = build_manifest
    base.parse_harness_telemetry = parse_harness_telemetry
    base.assistant_marker_seen = assistant_marker_and_artifact_seen


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("preflight", help="zero-cost injector and cell validation")
    subparsers.add_parser("self-test", help=argparse.SUPPRESS)
    run = subparsers.add_parser("run", help="freeze and launch the approved pilot")
    run.add_argument("--approved-manifest-sha256", required=True)
    proxy = subparsers.add_parser("_proxy", help=argparse.SUPPRESS)
    proxy.add_argument("--spec", type=Path, required=True)
    proxy.add_argument("--host", required=True)
    proxy.add_argument("--port", type=int, required=True)
    proxy.add_argument("--upstream", required=True)
    proxy.add_argument("--log-path", type=Path, required=True)
    proxy.add_argument("--max-in-flight", type=int)
    cell = subparsers.add_parser("_cell", help=argparse.SUPPRESS)
    cell.add_argument("--spec", type=Path, required=True)
    cell.add_argument("--validate", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    configure_base()
    if args.command == "preflight":
        return host_preflight()
    if args.command == "self-test":
        return offline_self_test()
    if args.command == "run":
        return tool_base.run_approved(args.approved_manifest_sha256)
    if args.command == "_proxy":
        return run_proxy(
            args.spec,
            args.host,
            args.port,
            args.upstream,
            args.log_path,
            max_in_flight=args.max_in_flight,
        )
    if args.command == "_cell":
        return run_cell(args.spec, validate=args.validate)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
