#!/usr/bin/env python3
"""Zero-cost Codex 0.150.1 model-control parity check.

This check talks only to a localhost mock Responses server. It clones Codex's
bundled GPT-5.6 Sol model metadata for each study model, applies the shared
study controls, captures the first request, and verifies the model-visible
instructions and tool schemas are identical across arms. The default ``alias``
mode instead uses one shared compatibility model identity for all five arms;
that is the simpler Harbor/OpenRouter design used by the paid qualification.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import http.server
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
from typing import Any


TARGETS = {
    "claude-opus-5": "anthropic/claude-opus-5",
    "gpt-6-astra": "openai/gpt-6-astra",
    "glm-5.3": "z-ai/glm-5.3",
    "kimi-k3": "moonshotai/kimi-k3",
    "deepseek-v4-pro-0813": "deepseek/deepseek-v4-pro-0813",
}
CONTEXT_WINDOW = 1_000_000
AUTO_COMPACT_LIMIT = 950_000
MAX_OUTPUT_TOKENS = 128_000
FINAL_TEXT = "CODEX_PARITY_OK"
COMPAT_ALIAS = "openrouter-eval"


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class CaptureState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: list[dict[str, Any]] = []

    def append(self, value: dict[str, Any]) -> None:
        with self.lock:
            self.requests.append(value)


def response_object(status: str, output: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": "resp_codex_parity",
        "object": "response",
        "created_at": int(time.time()),
        "status": status,
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "model": "local-parity-model",
        "output": output,
        "parallel_tool_calls": True,
        "previous_response_id": None,
        "reasoning": {"effort": "high", "summary": None},
        "store": False,
        "temperature": None,
        "text": {"format": {"type": "text"}, "verbosity": "low"},
        "tool_choice": "auto",
        "tools": [],
        "top_p": None,
        "truncation": "disabled",
        "usage": {
            "input_tokens": 1,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 1,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 2,
        },
    }


def sse_payload() -> bytes:
    item = {
        "id": "msg_codex_parity",
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [
            {
                "type": "output_text",
                "text": FINAL_TEXT,
                "annotations": [],
                "logprobs": [],
            }
        ],
    }
    events = [
        {"type": "response.created", "response": response_object("in_progress", [])},
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "id": item["id"],
                "type": "message",
                "status": "in_progress",
                "role": "assistant",
                "content": [],
            },
        },
        {
            "type": "response.content_part.added",
            "item_id": item["id"],
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": [], "logprobs": []},
        },
        {
            "type": "response.output_text.delta",
            "item_id": item["id"],
            "output_index": 0,
            "content_index": 0,
            "delta": FINAL_TEXT,
            "logprobs": [],
        },
        {
            "type": "response.output_text.done",
            "item_id": item["id"],
            "output_index": 0,
            "content_index": 0,
            "text": FINAL_TEXT,
            "logprobs": [],
        },
        {
            "type": "response.content_part.done",
            "item_id": item["id"],
            "output_index": 0,
            "content_index": 0,
            "part": item["content"][0],
        },
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": response_object("completed", [item])},
    ]
    return "".join(
        f"data: {json.dumps(event, separators=(',', ':'))}\n\n" for event in events
    ).encode()


def make_handler(state: CaptureState):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            return

        def do_POST(self) -> None:
            if not self.path.endswith("/responses"):
                self.send_error(404)
                return
            size = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                self.send_error(400)
                return
            state.append(body)
            payload = sse_payload()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


def bundled_gpt_entry() -> dict[str, Any]:
    completed = subprocess.run(
        ["codex", "debug", "models", "--bundled"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )
    catalog = json.loads(completed.stdout)
    matches = [model for model in catalog["models"] if model.get("slug") == "gpt-6-astra"]
    if len(matches) != 1:
        raise RuntimeError("Codex bundled catalog did not contain exactly one gpt-6-astra")
    return matches[0]


def write_catalog(path: Path, baseline: dict[str, Any]) -> dict[str, Any]:
    models = []
    for alias in TARGETS:
        model = copy.deepcopy(baseline)
        model["slug"] = alias
        model["display_name"] = alias
        model["context_window"] = CONTEXT_WINDOW
        model["max_context_window"] = CONTEXT_WINDOW
        model["effective_context_window_percent"] = 95
        # GPT-5.6's bundled Responses Lite + code_mode_only path relies on an
        # OpenAI-hosted backend that a generic OpenRouter /responses endpoint
        # does not provide. Keep the GPT-5.6 instructions and tool metadata,
        # but select Codex's public Responses tool serialization for every arm.
        model["use_responses_lite"] = False
        model["tool_mode"] = None
        models.append(model)
    catalog = {"models": models}
    path.write_bytes(canonical(catalog) + b"\n")
    return catalog


def write_config(
    path: Path,
    alias: str,
    catalog_path: Path | None,
    port: int,
    *,
    include_verbosity: bool,
    native_openai: bool = False,
) -> None:
    if native_openai:
        path.write_text(
            f'''model = "{alias}"
openai_base_url = "http://127.0.0.1:{port}/v1"
model_reasoning_effort = "high"
model_reasoning_summary = "none"
'''
        )
        return
    catalog_line = (
        f'model_catalog_json = "{catalog_path}"\n' if catalog_path is not None else ""
    )
    verbosity_line = 'model_verbosity = "low"\n' if include_verbosity else ""
    path.write_text(
        f'''model = "{alias}"
model_provider = "parity-mock"
{catalog_line}model_reasoning_effort = "high"
model_reasoning_summary = "none"
{verbosity_line}model_context_window = {CONTEXT_WINDOW}
model_auto_compact_token_limit = {AUTO_COMPACT_LIMIT}
model_auto_compact_token_limit_scope = "total"

[agents]
default_subagent_model = "{alias}"
default_subagent_reasoning_effort = "high"

[model_providers.parity-mock]
name = "parity-mock"
base_url = "http://127.0.0.1:{port}/v1"
env_key = "CODEX_PARITY_DUMMY_KEY"
wire_api = "responses"
request_max_retries = 0
stream_max_retries = 0
stream_idle_timeout_ms = 30000
'''
    )


def selected_request_fields(request: dict[str, Any]) -> dict[str, Any]:
    tools = request.get("tools") or []
    input_items = request.get("input") or []
    additional_tools = [
        item for item in input_items
        if isinstance(item, dict) and item.get("type") == "additional_tools"
    ]
    return {
        "reasoning": request.get("reasoning"),
        "text": request.get("text"),
        "include": request.get("include"),
        "parallel_tool_calls": request.get("parallel_tool_calls"),
        "store": request.get("store"),
        "stream": request.get("stream"),
        "instructions_sha256": digest(request.get("instructions")),
        "tools_sha256": digest(tools),
        "tool_types": [tool.get("type") for tool in tools],
        "tool_names": [tool.get("name") or tool.get("type") for tool in tools],
        "namespace_children": {
            str(tool.get("name")): [
                child.get("name")
                for child in tool.get("tools") or []
                if isinstance(child, dict)
            ]
            for tool in tools
            if tool.get("type") == "namespace"
        },
        "tool_count": len(tools),
        "input_item_types": [
            item.get("type") for item in input_items if isinstance(item, dict)
        ],
        "additional_tools_count": len(additional_tools),
        "additional_tools_sha256": digest(additional_tools),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--mode",
        choices=("alias", "catalog", "native"),
        default="alias",
        help="one shared fallback alias, or five GPT-metadata catalog clones",
    )
    args = parser.parse_args()

    completed = subprocess.run(
        ["codex", "--version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=True
    )
    version = completed.stdout.strip().splitlines()[-1]
    if version != "codex-cli 0.150.1":
        raise RuntimeError(f"expected codex-cli 0.150.1, got {version!r}")

    state = CaptureState()
    port = free_port()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), make_handler(state))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()

    baseline = bundled_gpt_entry()
    rows: list[dict[str, Any]] = []
    try:
        with tempfile.TemporaryDirectory(prefix="codex-config-parity-") as tmp_name:
            root = Path(tmp_name)
            catalog_path: Path | None = None
            catalog: dict[str, Any] | None = None
            if args.mode == "catalog":
                catalog_path = root / "models.json"
                catalog = write_catalog(catalog_path, baseline)
            targets = (
                {"gpt-6-astra": TARGETS["gpt-6-astra"]}
                if args.mode == "native"
                else TARGETS
            )
            for logical_alias, openrouter_model in targets.items():
                codex_model = (
                    logical_alias
                    if args.mode in {"catalog", "native"}
                    else COMPAT_ALIAS
                )
                before = len(state.requests)
                codex_home = root / f"home-{logical_alias}"
                workspace = root / f"workspace-{logical_alias}"
                codex_home.mkdir()
                workspace.mkdir()
                write_config(
                    codex_home / "config.toml",
                    codex_model,
                    catalog_path,
                    port,
                    include_verbosity=args.mode == "catalog",
                    native_openai=args.mode == "native",
                )
                environment = os.environ.copy()
                environment.update(
                    {
                        "CODEX_HOME": str(codex_home),
                        "CODEX_PARITY_DUMMY_KEY": "local-only-dummy",
                        "OPENAI_API_KEY": "local-only-dummy",
                    }
                )
                run = subprocess.run(
                    [
                        "codex",
                        "exec",
                        "--strict-config",
                        "--skip-git-repo-check",
                        "--ephemeral",
                        "--json",
                        "--dangerously-bypass-approvals-and-sandbox",
                        "--enable",
                        "unified_exec",
                        "--model",
                        codex_model,
                        "-C",
                        str(workspace),
                        "Reply with exactly CODEX_PARITY_OK and do not call tools.",
                    ],
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=60,
                    check=False,
                )
                captured = state.requests[before:]
                if run.returncode != 0 or FINAL_TEXT not in run.stdout or len(captured) != 1:
                    raise RuntimeError(
                        f"{logical_alias} failed: returncode={run.returncode}, requests={len(captured)}, "
                        f"output={run.stdout[-1500:]}"
                    )
                request = captured[0]
                if request.get("model") != codex_model:
                    raise AssertionError(
                        f"{logical_alias}: Codex sent model={request.get('model')!r}"
                    )
                fields = selected_request_fields(request)
                if (request.get("reasoning") or {}).get("effort") != "high":
                    raise AssertionError(
                        f"{logical_alias}: reasoning effort was not high"
                    )
                expected_verbosity = (
                    "low" if args.mode in {"catalog", "native"} else None
                )
                if (request.get("text") or {}).get("verbosity") != expected_verbosity:
                    raise AssertionError(
                        f"{logical_alias}: unexpected verbosity: "
                        f"{(request.get('text') or {}).get('verbosity')!r}"
                    )
                if args.mode != "native" and not request.get("tools"):
                    raise AssertionError(
                        f"{logical_alias}: Harbor-equivalent launch exposed no tools"
                    )
                if args.mode == "native" and not any(
                    isinstance(item, dict) and item.get("type") == "additional_tools"
                    for item in request.get("input") or []
                ):
                    raise AssertionError(
                        "native GPT path did not expose Responses Lite additional_tools"
                    )
                rows.append(
                    {
                        "logical_alias": logical_alias,
                        "codex_model": codex_model,
                        "openrouter_model": openrouter_model,
                        "request_fields": fields,
                    }
                )

            field_hashes = {digest(row["request_fields"]) for row in rows}
            if len(field_hashes) != 1:
                raise AssertionError("Codex request controls differ across model arms")
            result = {
                "status": "passed",
                "network": "localhost-only",
                "paid_api_calls": 0,
                "codex_version": version,
                "mode": args.mode,
                "compatibility_alias": COMPAT_ALIAS if args.mode == "alias" else None,
                "baseline": {
                    "source": "codex debug models --bundled",
                    "slug": baseline["slug"],
                    "sha256": digest(baseline),
                    "bundled_context_window": baseline.get("context_window"),
                    "bundled_max_context_window": baseline.get("max_context_window"),
                    "bundled_effective_context_window_percent": baseline.get(
                        "effective_context_window_percent"
                    ),
                    "default_reasoning_level": baseline.get("default_reasoning_level"),
                    "default_reasoning_summary": baseline.get("default_reasoning_summary"),
                    "default_verbosity": baseline.get("default_verbosity"),
                },
                "shared_overrides": ({
                    "context_window": "bundled default (272000; max 872000)",
                    "auto_compact_token_limit": "bundled/default derived behavior",
                    "reasoning_effort": "high",
                    "reasoning_summary": "none (also bundled default)",
                    "verbosity": "bundled default low",
                    "subagent_model": "bundled/default",
                    "provider_proxy_max_output_tokens": "unset",
                    "use_responses_lite": True,
                    "tool_mode": "code_mode_only",
                } if args.mode == "native" else {
                    "context_window": CONTEXT_WINDOW,
                    "auto_compact_token_limit": AUTO_COMPACT_LIMIT,
                    "auto_compact_token_limit_scope": "total",
                    "reasoning_effort": "high",
                    "reasoning_summary": "none",
                    "verbosity": (
                        "low" if args.mode == "catalog" else "unset uniformly; "
                        "Codex fallback metadata does not advertise verbosity support"
                    ),
                    "subagent_model": "same as parent cell",
                    "subagent_reasoning_effort": "high",
                    "provider_proxy_max_output_tokens": MAX_OUTPUT_TOKENS,
                    "use_responses_lite": False,
                    "tool_mode": None,
                }),
                "catalog_sha256": digest(catalog) if catalog is not None else None,
                "request_control_sha256": next(iter(field_hashes)),
                "rows": rows,
                "limitations": [
                    "model_context_window and auto-compaction thresholds are client-side controls and do not appear in the Responses body",
                    "max output tokens and strict OpenRouter provider routing are proxy-side controls and require the paid passthrough qualification",
                ],
            }
    finally:
        server.shutdown()

    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
