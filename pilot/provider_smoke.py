#!/usr/bin/env python3
"""Approved, low-cost provider connectivity pilot for the five pinned harnesses.

The host-side ``run`` command freezes a manifest before launching any model call.
The private ``_cell`` command runs one cell inside the pinned Docker image.
Secrets are inherited from ``.env`` and are never serialized by this script.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN_ID = "pilot-openrouter-four-model-five-harness-20260830-r1"
MANIFEST_PREPARED_AT_UTC = "2026-08-30T07:07:34Z"
IMAGE = "harness-provider-smoke:20260829-r1"
IMAGE_ID = "sha256:28d95202b19950b1036baf1bbf2543e5fa6b072533e54b6961ff0c1b78b36b10"
RUN_DIR = ROOT / "runs" / CAMPAIGN_ID
REPORT_PATH = RUN_DIR / "report.md"
MARKER = "PROVIDER_SMOKE_OK"
PROMPT = (
    "Connectivity test. Do not call tools or inspect files. "
    "Reply with exactly PROVIDER_SMOKE_OK and nothing else."
)
HARNESSES = ("pi", "claude-code", "codex", "openhands", "deepseek-harness")
SOFT_LIMITS_USD = {"OPENROUTER_API_KEY": 1.25}
UNKNOWN_CALL_RESERVE_USD = 0.05
MAX_OUTPUT_TOKENS = 128
MAX_AGENT_TURNS = 1
CELL_TIMEOUT_SECONDS = 240

MODELS: dict[str, dict[str, Any]] = {
    "claude-opus-5": {
        "openrouter_model": "anthropic/claude-opus-5",
        "anthropic_base_url": "https://openrouter.ai/api",
        "openrouter_route": {
            "only": ["anthropic"],
            "allow_fallbacks": False,
            "require_parameters": True,
        },
        "endpoint": {
            "tag": "anthropic",
            "provider_name": "Anthropic",
            "quantization": "unknown",
            "context_length": 1_000_000,
            "max_completion_tokens": 128_000,
            "prompt_usd_per_token": 0.000005,
            "completion_usd_per_token": 0.000025,
        },
    },
    "gpt-6-astra": {
        "openrouter_model": "openai/gpt-6-astra",
        "anthropic_base_url": "https://openrouter.ai/api",
        "openrouter_route": {
            "only": ["openai"],
            "allow_fallbacks": False,
            "require_parameters": True,
        },
        "endpoint": {
            "tag": "openai",
            "provider_name": "OpenAI",
            "quantization": "unknown",
            "context_length": 1_050_000,
            "max_completion_tokens": 128_000,
            "prompt_usd_per_token": 0.000002,
            "completion_usd_per_token": 0.000010,
        },
    },
    "glm-5.3": {
        "openrouter_model": "z-ai/glm-5.3",
        "anthropic_base_url": "https://openrouter.ai/api",
        "openrouter_route": {
            "only": ["z-ai/fp8"],
            "quantizations": ["fp8"],
            "allow_fallbacks": False,
            "require_parameters": True,
        },
        "endpoint": {
            "tag": "z-ai/fp8",
            "provider_name": "Z.AI",
            "quantization": "fp8",
            "context_length": 1_048_576,
            "max_completion_tokens": 131_072,
            "prompt_usd_per_token": 0.0000014,
            "completion_usd_per_token": 0.0000044,
        },
    },
    "kimi-k3": {
        "openrouter_model": "moonshotai/kimi-k3",
        "anthropic_base_url": "https://openrouter.ai/api",
        "openrouter_route": {
            "only": ["moonshotai/mxfp4"],
            "quantizations": ["mxfp4"],
            "allow_fallbacks": False,
            "require_parameters": True,
        },
        "endpoint": {
            "tag": "moonshotai/mxfp4",
            "provider_name": "Moonshot AI",
            "quantization": "mxfp4",
            "context_length": 1_048_576,
            "max_completion_tokens": 943_718,
            "prompt_usd_per_token": 0.000003,
            "completion_usd_per_token": 0.000015,
        },
    },
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    path.chmod(mode)


def build_cells() -> list[dict[str, Any]]:
    cells: list[dict[str, Any]] = []
    harness_versions = {
        "pi": "0.84.4",
        "claude-code": "2.1.251",
        "codex": "0.150.1",
        "openhands": "1.44.1",
        "deepseek-harness": "0.1.1-rc.2",
    }
    for model_id, model in MODELS.items():
        for harness in HARNESSES:
            if harness == "claude-code":
                protocol = "anthropic-messages"
                proxy_required = False
                route_control = "OpenRouter automatic provider routing; actual provider audited after the call"
                compatibility_drop_params: list[str] = []
            elif harness == "codex":
                protocol = "openai-responses"
                proxy_required = True
                route_control = "strict request-body route injection"
                compatibility_drop_params = ["parallel_tool_calls"]
            else:
                protocol = "openai-chat-completions"
                proxy_required = harness == "deepseek-harness"
                route_control = "strict request-body route injection"
                compatibility_drop_params = []
            cells.append(
                {
                    "cell_id": f"openrouter--{model_id}--{harness}",
                    "channel": "openrouter",
                    "harness": harness,
                    "harness_version": harness_versions[harness],
                    "logical_model": model_id,
                    "actual_model": model["openrouter_model"],
                    "provider": "openrouter",
                    "key_env": "OPENROUTER_API_KEY",
                    "protocol": protocol,
                    "translated": False,
                    "translation": None,
                    "proxy_required": proxy_required,
                    "compatibility_variant": (
                        "drop-parallel-tools" if harness == "codex" else "none"
                    ),
                    "compatibility_drop_params": compatibility_drop_params,
                    "run_only_if_all_prior_failed": [],
                    "base_url": (
                        "https://openrouter.ai/api"
                        if harness == "claude-code"
                        else "https://openrouter.ai/api/v1"
                    ),
                    "openrouter_route": model["openrouter_route"],
                    "route_control": route_control,
                    "dsh_semantics": (
                        "headless connectivity profile; not formal standard preset"
                        if harness == "deepseek-harness"
                        else None
                    ),
                    "hard_output_limit": harness != "codex",
                }
            )
    return cells


def build_manifest() -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "campaign_id": CAMPAIGN_ID,
        "kind": "paid-connectivity-pilot",
        "prepared_at_utc": MANIFEST_PREPARED_AT_UTC,
        "approval": {
            "required_before_launch": True,
            "approval_target": "campaign ID plus manifest SHA256",
            "launch_requires_cli_hash_match": True,
        },
        "scope": {
            "benchmark": "none-openrouter-connectivity-smoke",
            "dataset": None,
            "task_subset": "one fixed synthetic no-tool marker prompt per model/harness cell",
            "replicate": 1,
            "planned_trials_maximum": len(build_cells()),
            "planned_trials_minimum": len(build_cells()),
            "conditional_stop": None,
            "concurrency": 1,
            "prompt": PROMPT,
        },
        "runner": {
            "entry_command": "python pilot/provider_smoke.py run",
            "harbor_reference_version": "v0.22.0",
            "harbor_reference_commit": "4407eb5",
            "execution": "five pinned harnesses in one existing pilot image; no benchmark runner",
            "integration_type": "standalone pilot wrapper; neither Harbor adapter nor ALE deployer",
            "harness_install_and_entry": {
                "pi": {
                    "source": "npm @earendil-works/pi-coding-agent@0.84.4",
                    "entry": "pi --print --mode json --no-session",
                },
                "claude-code": {
                    "source": "npm @anthropic-ai/claude-code@2.1.251",
                    "entry": "claude --print --output-format=stream-json --max-turns 1",
                },
                "codex": {
                    "source": "npm @openai/codex@0.150.1",
                    "entry": "codex exec --strict-config --dangerously-bypass-approvals-and-sandbox --json",
                },
                "openhands": {
                    "source": "PyPI openhands-sdk==1.44.1 and openhands-tools==1.44.1",
                    "entry": "OpenHands SDK Conversation.run(max_iteration_per_run=1)",
                },
                "deepseek-harness": {
                    "source": "npm @deepseek-ai/dsh@0.1.1-rc.2",
                    "entry": "dsh --profile headless --patch <resolved-patch> <prompt>",
                },
            },
            "image": IMAGE,
            "image_id": IMAGE_ID,
            "sandbox_provider": "local Docker Engine; pilot-only isolated container",
            "container_user": "host uid:gid",
            "cpu": 2,
            "memory": "4 GiB",
            "network": "Docker default bridge; provider API egress enabled",
            "cell_timeout_seconds": CELL_TIMEOUT_SECONDS,
        },
        "controls": {
            "reasoning_effort": "low wherever the harness exposes it; connectivity-only cost control, not a formal benchmark default",
            "reasoning_summary": "none for Codex; other harness defaults",
            "thinking": "low where exposed; model/provider may keep reasoning always enabled",
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "output_limit_location": "128 at PI/Claude Code/OpenHands/DSH request configuration; Codex has no hard CLI output cap on this custom Responses path",
            "max_turns_or_iterations": "PI one print session; Claude Code and OpenHands one turn/iteration; Codex and DSH loops bounded by timeout; DSH also makes its default title request",
            "timeout_seconds": CELL_TIMEOUT_SECONDS,
            "retries": {
                "codex": 0,
                "openhands_llm": 0,
                "dsh_provider": 0,
                "litellm_proxy": 0,
                "pi": "no exposed retry override",
                "claude_code": "no exposed retry-disable control; hidden retries will be audited",
            },
            "temperature": "harness/provider default",
            "seed": None,
            "context_window": "endpoint snapshot in this manifest; harness unknown-model fallback metadata may be smaller and is retained",
            "compaction": "harness default; not expected in one short no-tool turn",
            "tools": "each harness's installed defaults remain available, but the fixed prompt explicitly forbids tool use",
            "skills": "no added skills; empty isolated HOME/workspace",
            "mcp": [],
            "subagents": "harness defaults; no added agents and the prompt asks for no tools",
            "memory": "empty isolated HOME",
            "custom_prompt_files": [],
            "benchmark_specific_augmentation": False,
            "compatibility_only_changes": [
                "Claude Code uses OpenRouter's native Anthropic Messages skin directly with bearer auth; no local protocol bridge",
                "Claude Code adds only X-OpenRouter-Metadata: enabled through its documented gateway custom-header setting for route auditability",
                "PI and OpenHands use native OpenRouter Chat and inject the strict provider object directly",
                "DSH stays Chat-to-Chat through LiteLLM 1.98.0 only to inject the strict provider object",
                "Codex stays Responses-to-Responses through LiteLLM 1.98.0 to inject the strict provider object and drops only parallel_tool_calls, the previously audited unsupported field",
                "PI/OpenHands map max_completion_tokens to the endpoints' advertised max_tokens field while preserving the value",
                "no model catalog, tool, prompt, skill, MCP, memory, or benchmark-specific augmentation is added",
            ],
        },
        "budget": {
            "soft_limit_usd_per_key": SOFT_LIMITS_USD,
            "unknown_call_reserve_usd": UNKNOWN_CALL_RESERVE_USD,
            "stop_policy": "sequential pre-call check against source-qualified cost or conservative reserve",
            "hard_provider_cap": False,
            "expected_incremental_total_usd": "approximately 0.35-0.85 total; OPENROUTER_API_KEY soft ceiling 1.25; exact cost depends on harness system-prompt size and hidden auxiliary/retry calls",
        },
        "accounting": {
            "preserve_sources": [
                "OpenRouter response metadata and generation API records",
                "harness native usage/cost",
                "OpenHands LiteLLM metrics",
                "LiteLLM proxy logs/metrics",
                "Codex built-in Responses proxy redacted request/response dumps",
            ],
            "aggregation_rule": "never add nested reports of the same call",
            "provider_cost": "OpenRouter/provider cost is primary; harness and proxy copies retained separately and never summed",
        },
        "comparison": {
            "matched_models": list(MODELS),
            "matched_harnesses": list(HARNESSES),
            "outcomes": [
                "basic harness-to-model connectivity",
                "exact marker compliance",
                "wall time",
                "source-qualified input/cached/output/reasoning tokens",
                "source-qualified reported cost",
                "requested and actual model/provider identity",
            ],
            "scope_note": "full 4 model x 5 harness OpenRouter connectivity matrix; DSH headless is connectivity-only and is not its formal standard preset",
            "quality_claim": "none; a deterministic no-tool marker prompt cannot establish tool-loop or benchmark quality equivalence",
        },
        "routing": {
            "strict_cells": "PI, Codex, OpenHands, and DSH use only=<official endpoint>, allow_fallbacks=false, require_parameters=true",
            "claude_code_exception": "Claude Code's direct gateway integration cannot attach the provider object; those four cells use OpenRouter automatic routing and must be audited for the actual provider",
            "endpoint_snapshot_source": "OpenRouter authenticated per-model endpoints API at prepared_at_utc",
            "models": {
                model_id: {
                    "model": model["openrouter_model"],
                    "strict_route": model["openrouter_route"],
                    "endpoint": model["endpoint"],
                }
                for model_id, model in MODELS.items()
            },
        },
        "versions": {
            "pi": "@earendil-works/pi-coding-agent@0.84.4",
            "claude_code": "@anthropic-ai/claude-code@2.1.251",
            "codex": "@openai/codex@0.150.1",
            "openhands_sdk": "openhands-sdk==1.44.1",
            "openhands_tools": "openhands-tools==1.44.1",
            "deepseek_harness": "@deepseek-ai/dsh@0.1.1-rc.2",
            "litellm_proxy": "litellm[proxy]==1.98.0",
        },
        "source_sha256": {
            "pilot/provider_smoke.py": sha256_file(Path(__file__)),
            "pilot/Containerfile": sha256_file(ROOT / "pilot" / "Containerfile"),
        },
        "cells": build_cells(),
    }
    manifest_hash = hashlib.sha256(canonical_json(manifest)).hexdigest()
    manifest["manifest_sha256"] = manifest_hash
    return manifest


def load_env_keys(path: Path) -> set[str]:
    result: set[str] = set()
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            result.add(stripped.split("=", 1)[0].removeprefix("export ").strip())
    return result


def load_env_value(path: Path, name: str) -> str | None:
    """Read one dotenv value without ever logging or serializing it."""
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.removeprefix("export ").strip() != name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        return value
    return None


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
        f"{ROOT / 'pilot'}:/pilot:ro",
        "-v",
        f"{cell_dir}:/trial",
        "-w",
        "/trial/workspace",
        IMAGE,
        "/opt/openhands/bin/python",
        "/pilot/provider_smoke.py",
        "_cell",
        "--spec",
        "/trial/spec.json",
    ]
    if validate:
        command.append("--validate")
    return command


def run_subprocess(
    args: list[str], *, cwd: Path, env: dict[str, str], timeout: int, log_path: Path
) -> tuple[int, float]:
    started = time.monotonic()
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            args,
            cwd=cwd,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            return_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            return_code = 124
    return return_code, time.monotonic() - started


def parse_json_lines(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not path.exists():
        return records
    for line in path.read_text(errors="replace").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


GENERATION_ID_PATTERN = re.compile(r"\bgen-[A-Za-z0-9_-]+\b")


def extract_openrouter_generation_ids(cell_dir: Path) -> list[str]:
    ids: set[str] = set()
    for path in cell_dir.rglob("*"):
        if not path.is_file() or path.suffix not in {".json", ".jsonl", ".log"}:
            continue
        if path.stat().st_size > 20 * 1024 * 1024:
            continue
        ids.update(GENERATION_ID_PATTERN.findall(path.read_text(errors="replace")))
    return sorted(ids)


def fetch_openrouter_generation_records(
    cell_dir: Path, api_key: str
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for generation_id in extract_openrouter_generation_ids(cell_dir):
        query = urllib.parse.urlencode({"id": generation_id})
        request = urllib.request.Request(
            f"https://openrouter.ai/api/v1/generation?{query}",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=15) as response:
                    payload = json.loads(response.read())
                if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
                    records.append(payload["data"])
                break
            except urllib.error.HTTPError as exc:
                if attempt == 2 or exc.code not in {404, 429}:
                    errors.append(
                        {
                            "generation_id": generation_id,
                            "error": "http",
                            "status": exc.code,
                        }
                    )
                    break
            except Exception as exc:
                if attempt == 2:
                    errors.append(
                        {
                            "generation_id": generation_id,
                            "error": type(exc).__name__,
                        }
                    )
                    break
            time.sleep(attempt + 1)
    return {
        "source": "openrouter-generation-api",
        "generation_ids_found": extract_openrouter_generation_ids(cell_dir),
        "records": records,
        "errors": errors,
    }


def parse_harness_telemetry(harness: str, cell_dir: Path) -> dict[str, Any]:
    if harness == "openhands":
        source = cell_dir / "openhands-result.json"
        return json.loads(source.read_text()) if source.exists() else {}

    stdout = cell_dir / "harness.log"
    records = parse_json_lines(stdout)
    if harness == "pi":
        messages = [
            item.get("message", {})
            for item in records
            if item.get("type") == "message_end"
            and (item.get("message") or {}).get("role") == "assistant"
        ]
        if messages:
            return {"source": "pi-native", "usage": messages[-1].get("usage")}
    if harness == "claude-code":
        results = [item for item in records if item.get("type") == "result"]
        if results:
            item = results[-1]
            return {
                "source": "claude-code-native",
                "usage": item.get("usage"),
                "total_cost_usd": item.get("total_cost_usd"),
                "model_usage": item.get("modelUsage") or item.get("model_usage"),
            }
    if harness == "codex":
        completed = [item for item in records if item.get("type") == "turn.completed"]
        if completed:
            return {
                "source": "codex-native",
                "usage": completed[-1].get("usage"),
            }
    if harness == "deepseek-harness":
        session_records: list[dict[str, Any]] = []
        session_paths = sorted((cell_dir / "home").rglob("*.jsonl"))
        for path in session_paths:
            session_records.extend(parse_json_lines(path))
        usages = [
            item
            for item in session_records
            if "usage" in str(item.get("type", "")).lower()
            or isinstance(item.get("usage"), dict)
            or bool(
                scalar_numbers(
                    item,
                    {
                        "input_tokens",
                        "prompt_tokens",
                        "output_tokens",
                        "completion_tokens",
                        "inputTokens",
                        "outputTokens",
                    },
                )
            )
        ]
        event_type_counts: dict[str, int] = {}
        for item in session_records:
            event_type = str(item.get("type", "unknown"))
            event_type_counts[event_type] = event_type_counts.get(event_type, 0) + 1
        return {
            "source": "dsh-native-session-log",
            "usage_events": usages,
            "session_jsonl_files": len(session_paths),
            "session_record_count": len(session_records),
            "event_type_counts": event_type_counts,
        }
    return {"source": f"{harness}-native", "unparsed": True}


def assistant_marker_seen(harness: str, cell_dir: Path, telemetry: dict[str, Any]) -> bool:
    """Check assistant output only, never the echoed connectivity prompt."""
    if harness == "openhands":
        return any(
            isinstance(message, str) and message.strip() == MARKER
            for message in telemetry.get("messages", [])
        )
    records = parse_json_lines(cell_dir / "harness.log")
    if harness == "pi":
        for item in records:
            message = item.get("message") or {}
            if item.get("type") != "message_end" or message.get("role") != "assistant":
                continue
            if message.get("stopReason") == "error":
                continue
            texts = [
                part.get("text", "")
                for part in message.get("content", [])
                if isinstance(part, dict) and part.get("type") == "text"
            ]
            if "\n".join(texts).strip() == MARKER:
                return True
        return False
    if harness == "claude-code":
        return any(
            item.get("type") == "result"
            and not item.get("is_error", False)
            and str(item.get("result", "")).strip() == MARKER
            for item in records
        )
    if harness == "codex":
        return any(
            item.get("type") == "item.completed"
            and (item.get("item") or {}).get("type") == "agent_message"
            and str((item.get("item") or {}).get("text", "")).strip()
            == MARKER
            for item in records
        )
    if harness == "deepseek-harness":
        log = cell_dir / "harness.log"
        return log.exists() and log.read_text(errors="replace").strip() == MARKER
    return False


def bridge_artifact_seen(cell_dir: Path) -> bool:
    artifact = cell_dir / "workspace" / "bridge-check.txt"
    return artifact.exists() and artifact.read_text(errors="replace") == MARKER


def scalar_numbers(value: Any, wanted: set[str]) -> list[float]:
    found: list[float] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in wanted and isinstance(child, (int, float)):
                found.append(float(child))
            else:
                found.extend(scalar_numbers(child, wanted))
    elif isinstance(value, list):
        for child in value:
            found.extend(scalar_numbers(child, wanted))
    return found


def find_named_dicts(value: Any, wanted: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key == wanted and isinstance(child, dict):
                found.append(child)
            found.extend(find_named_dicts(child, wanted))
    elif isinstance(value, list):
        for child in value:
            found.extend(find_named_dicts(child, wanted))
    return found


def budget_charge(telemetry: dict[str, Any], attempted: bool) -> tuple[float, str]:
    costs = scalar_numbers(
        telemetry, {"total_cost_usd", "cost_usd", "total_cost", "cost", "costUSD"}
    )
    for usage_cost in find_named_dicts(telemetry, "cost"):
        total = usage_cost.get("total")
        if isinstance(total, (int, float)):
            costs.append(float(total))
    positive_costs = [value for value in costs if 0 < value < 10]
    if positive_costs:
        return max(positive_costs), "reported-cost-max-not-summed"

    input_candidates = scalar_numbers(
        telemetry, {"input_tokens", "prompt_tokens", "uncachedInputTokens"}
    )
    output_candidates = scalar_numbers(
        telemetry, {"output_tokens", "completion_tokens", "outputTokens"}
    )
    if input_candidates or output_candidates:
        # Conservative upper bound across the four pinned OpenRouter routes.
        estimate = (
            max(input_candidates or [0.0]) * 5.0
            + max(output_candidates or [0.0]) * 25.0
        ) / 1_000_000
        return max(estimate, 0.001), "conservative-token-estimate"
    return (UNKNOWN_CALL_RESERVE_USD if attempted else 0.0), "unknown-call-reserve"


def usage_summary(telemetry: dict[str, Any]) -> dict[str, float | None]:
    """Normalize one source without adding nested copies of the same call."""

    def largest(names: set[str]) -> float | None:
        values = scalar_numbers(telemetry, names)
        return max(values) if values else None

    costs = [
        value
        for value in scalar_numbers(
            telemetry, {"total_cost_usd", "cost_usd", "total_cost", "costUSD"}
        )
        if 0 <= value < 10
    ]
    for usage_cost in find_named_dicts(telemetry, "cost"):
        total = usage_cost.get("total")
        if isinstance(total, (int, float)) and 0 <= float(total) < 10:
            costs.append(float(total))
    return {
        "input_tokens": largest({"input", "input_tokens", "prompt_tokens", "inputTokens"}),
        "cached_input_tokens": largest(
            {
                "cacheRead",
                "cached_tokens",
                "cache_read_input_tokens",
                "cacheReadInputTokens",
            }
        ),
        "output_tokens": largest(
            {"output", "output_tokens", "completion_tokens", "outputTokens"}
        ),
        "reasoning_tokens": largest(
            {"reasoning", "reasoning_tokens", "thinking_tokens"}
        ),
        "reported_cost_usd": max(costs) if costs else None,
    }


def compact_number(value: float | None, *, digits: int = 0) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}"


def write_report(manifest: dict[str, Any], results: list[dict[str, Any]]) -> None:
    counts: dict[str, int] = {}
    for item in results:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    ledgers: dict[str, float] = {}
    for item in results:
        ledgers[item["key_env"]] = ledgers.get(item["key_env"], 0.0) + float(
            item.get("budget_charge_usd", 0.0)
        )

    lines = [
        f"# {CAMPAIGN_ID}",
        "",
        f"- Manifest SHA256: `{manifest['manifest_sha256']}`",
        f"- Image: `{IMAGE_ID}`",
        f"- Generated: `{utc_now()}`",
        f"- Scope: {len(manifest['cells'])} OpenRouter connectivity cells; no benchmark scores",
        "",
        "## Outcome",
        "",
        "| status | count |",
        "|---|---:|",
    ]
    for status in sorted(counts):
        lines.append(f"| {status} | {counts[status]} |")
    lines.extend(["", "## Soft-budget ledger", "", "| key | charged USD |", "|---|---:|"])
    for key, amount in sorted(ledgers.items()):
        lines.append(f"| `{key}` | {amount:.6f} |")
    lines.extend(
        [
            "",
            "The ledger uses the highest surfaced cost for a call, otherwise a conservative token estimate, otherwise a `$0.05` unknown-call reserve. It is a stop-control estimate, not a provider invoice.",
            "",
            "## Cell results",
            "",
            "| channel | model | harness | protocol | status | marker | seconds | charged USD |",
            "|---|---|---|---|---|---:|---:|---:|",
        ]
    )
    for item in results:
        lines.append(
            f"| {item['channel']} | {item['logical_model']} | {item['harness']} | "
            f"{item['protocol']} | {item['status']} | {str(item.get('marker_seen', False)).lower()} | "
            f"{item.get('wall_seconds', 0):.1f} | {item.get('budget_charge_usd', 0):.6f} |"
        )
    lines.extend(
        [
            "",
            "## Accounting and audit notes",
            "",
            "Raw harness logs, generated compatibility configs, LiteLLM proxy logs, Codex's redacted raw Responses request/response dumps, OpenRouter generation records, source-qualified telemetry, and per-cell results are under the ignored immutable run directory. Nested token/cost counters were preserved but not added together.",
            "",
            "A trajectory audit is required before using these results as compatibility evidence. Inspect every cell, actual model/provider routing, protocol errors, unexpected tool calls, hidden retries and auxiliary calls, token/cost disagreements, and malformed/missing artifacts. Record accept/rerun/exclude decisions in the canonical provider report; any rerun needs a new campaign ID and approval.",
        ]
    )
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines) + "\n")


def host_run(approved_manifest_sha256: str) -> int:
    if RUN_DIR.exists():
        print(f"ERROR immutable run directory already exists: {RUN_DIR}", file=sys.stderr)
        return 2
    if shutil.which("docker") is None:
        print("ERROR docker not found", file=sys.stderr)
        return 2
    env_file = ROOT / ".env"
    if not env_file.exists():
        print("ERROR .env not found", file=sys.stderr)
        return 2
    required = {cell["key_env"] for cell in build_cells()}
    missing = required - load_env_keys(env_file)
    if missing:
        print("ERROR missing required key names: " + ", ".join(sorted(missing)), file=sys.stderr)
        return 2

    manifest = build_manifest()
    if approved_manifest_sha256 != manifest["manifest_sha256"]:
        print(
            "ERROR approved manifest hash does not match the resolved configuration: "
            f"expected {manifest['manifest_sha256']}",
            file=sys.stderr,
        )
        return 2
    RUN_DIR.mkdir(parents=True)
    write_json(RUN_DIR / "manifest.json", manifest, mode=0o444)
    write_json(
        RUN_DIR / "approval.json",
        {
            "campaign_id": CAMPAIGN_ID,
            "approved_manifest_sha256": approved_manifest_sha256,
            "launch_recorded_at_utc": utc_now(),
        },
        mode=0o444,
    )
    print(f"Frozen manifest {manifest['manifest_sha256']}", flush=True)

    # Validate every resolved cell without making a model request.
    cell_count = len(manifest["cells"])
    for index, cell in enumerate(manifest["cells"], 1):
        cell_dir = RUN_DIR / "cells" / cell["cell_id"]
        (cell_dir / "workspace").mkdir(parents=True)
        (cell_dir / "home").mkdir(parents=True)
        write_json(cell_dir / "spec.json", cell)
        completed = subprocess.run(
            docker_command(cell_dir, validate=True),
            cwd=ROOT,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=90,
        )
        (cell_dir / "validation.log").write_text(completed.stdout)
        if completed.returncode != 0:
            print(
                f"ERROR zero-cost validation failed [{index}/{cell_count}] {cell['cell_id']}; "
                f"see {cell_dir / 'validation.log'}",
                file=sys.stderr,
            )
            return 2
    print(f"Zero-cost validation passed for all {cell_count} resolved cells", flush=True)

    results: list[dict[str, Any]] = []
    ledgers = {key: 0.0 for key in required}
    for index, cell in enumerate(manifest["cells"], 1):
        key_env = cell["key_env"]
        prior_ids = set(cell.get("run_only_if_all_prior_failed") or [])
        if any(
            item["cell_id"] in prior_ids and item["status"] == "passed"
            for item in results
        ):
            result = {
                **cell,
                "status": "skipped-prior-variant-passed",
                "attempted": False,
                "marker_seen": False,
                "wall_seconds": 0.0,
                "budget_charge_usd": 0.0,
                "budget_charge_source": "not-attempted",
                "completed_at_utc": utc_now(),
            }
            results.append(result)
            write_json(RUN_DIR / "results.json", results)
            print(
                f"[{index}/{cell_count}] SKIP {cell['cell_id']} prior variant passed",
                flush=True,
            )
            continue
        required_pass_ids = set(cell.get("run_only_if_all_prior_passed") or [])
        if required_pass_ids and not required_pass_ids.issubset(
            {
                item["cell_id"]
                for item in results
                if item.get("status") == "passed"
            }
        ):
            result = {
                **cell,
                "status": "skipped-prerequisite-not-passed",
                "attempted": False,
                "marker_seen": False,
                "wall_seconds": 0.0,
                "budget_charge_usd": 0.0,
                "budget_charge_source": "not-attempted",
                "completed_at_utc": utc_now(),
            }
            results.append(result)
            write_json(RUN_DIR / "results.json", results)
            print(
                f"[{index}/{cell_count}] SKIP {cell['cell_id']} prerequisite not passed",
                flush=True,
            )
            continue
        soft_limit = SOFT_LIMITS_USD[key_env]
        if ledgers[key_env] + UNKNOWN_CALL_RESERVE_USD > soft_limit + 1e-9:
            result = {
                **cell,
                "status": "skipped-soft-budget",
                "attempted": False,
                "marker_seen": False,
                "wall_seconds": 0.0,
                "budget_charge_usd": 0.0,
                "budget_charge_source": "not-attempted",
                "completed_at_utc": utc_now(),
            }
            results.append(result)
            write_json(RUN_DIR / "results.json", results)
            print(f"[{index}/{cell_count}] SKIP {cell['cell_id']} soft budget", flush=True)
            continue

        cell_dir = RUN_DIR / "cells" / cell["cell_id"]
        print(f"[{index}/{cell_count}] RUN  {cell['cell_id']}", flush=True)
        launch_env = os.environ.copy()
        key_value = load_env_value(env_file, key_env)
        if key_value:
            launch_env[key_env] = key_value
        return_code, wall_seconds = run_subprocess(
            docker_command(cell_dir, validate=False),
            cwd=ROOT,
            env=launch_env,
            timeout=CELL_TIMEOUT_SECONDS + 20,
            log_path=cell_dir / "container.log",
        )
        telemetry = parse_harness_telemetry(cell["harness"], cell_dir)
        if cell["channel"] == "openrouter":
            openrouter_key = load_env_value(env_file, "OPENROUTER_API_KEY")
            if openrouter_key:
                telemetry["openrouter_generation_api"] = (
                    fetch_openrouter_generation_records(cell_dir, openrouter_key)
                )
        marker_seen = assistant_marker_seen(cell["harness"], cell_dir, telemetry)
        write_json(cell_dir / "telemetry.json", telemetry)
        charge, charge_source = budget_charge(telemetry, attempted=True)
        ledgers[key_env] += charge
        status = (
            "passed"
            if return_code == 0 and marker_seen
            else "failed"
        )
        if return_code == 124:
            status = "timeout"
        result = {
            **cell,
            "status": status,
            "attempted": True,
            "return_code": return_code,
            "marker_seen": marker_seen,
            "wall_seconds": round(wall_seconds, 3),
            "budget_charge_usd": round(charge, 9),
            "budget_charge_source": charge_source,
            "usage_summary": usage_summary(telemetry),
            "key_ledger_usd_after": round(ledgers[key_env], 9),
            "completed_at_utc": utc_now(),
        }
        results.append(result)
        write_json(cell_dir / "result.json", result)
        write_json(RUN_DIR / "results.json", results)
        print(
            f"[{index}/{cell_count}] {status.upper():7} marker={marker_seen} "
            f"{wall_seconds:.1f}s {key_env} ledger=${ledgers[key_env]:.4f}",
            flush=True,
        )
        if ledgers[key_env] >= soft_limit:
            print(f"Soft stop reached for {key_env}", flush=True)

    summary = {
        "campaign_id": CAMPAIGN_ID,
        "manifest_sha256": manifest["manifest_sha256"],
        "completed_at_utc": utc_now(),
        "counts": {
            status: sum(item["status"] == status for item in results)
            for status in sorted({item["status"] for item in results})
        },
        "budget_ledgers_usd": ledgers,
        "post_run_trajectory_audit_required": True,
    }
    write_json(RUN_DIR / "summary.json", summary)
    write_report(manifest, results)
    print(f"Report {REPORT_PATH}", flush=True)
    return 0 if all(item["status"] == "passed" for item in results) else 1


def host_preflight() -> int:
    """Resolve and validate all cells in disposable directories; never call a model."""
    manifest = build_manifest()
    with tempfile.TemporaryDirectory(prefix="provider-smoke-preflight-", dir="/tmp") as tmp:
        base = Path(tmp)
        cell_count = len(manifest["cells"])
        for index, cell in enumerate(manifest["cells"], 1):
            cell_dir = base / cell["cell_id"]
            (cell_dir / "workspace").mkdir(parents=True)
            (cell_dir / "home").mkdir(parents=True)
            write_json(cell_dir / "spec.json", cell)
            completed = subprocess.run(
                docker_command(cell_dir, validate=True),
                cwd=ROOT,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=90,
            )
            if completed.returncode != 0:
                print(f"FAIL [{index}/{cell_count}] {cell['cell_id']}")
                print(completed.stdout[-4000:])
                inner = cell_dir / "validation-inner.log"
                if inner.exists():
                    print(inner.read_text(errors="replace")[-8000:])
                proxy = cell_dir / "litellm.log"
                if proxy.exists():
                    print(proxy.read_text(errors="replace")[-8000:])
                return 2
            print(f"OK   [{index}/{cell_count}] {cell['cell_id']}", flush=True)
    print(f"Preflight complete; prospective manifest {manifest['manifest_sha256']}")
    print("Safety: configuration/model-catalog expansion only; zero model calls")
    return 0


def proxy_config(spec: dict[str, Any]) -> dict[str, Any]:
    if spec["channel"] == "openrouter":
        target_model = f"openrouter/{spec['actual_model']}"
        api_key = "os.environ/OPENROUTER_API_KEY"
        api_base = "https://openrouter.ai/api/v1"
        extra_body = {"provider": spec["openrouter_route"]}
    else:
        model = MODELS[spec["logical_model"]]
        target_model = model["openhands_model"]
        api_key = f"os.environ/{spec['key_env']}"
        api_base = model["chat_base_url"]
        extra_body = {}
    params: dict[str, Any] = {
        "model": target_model,
        "api_key": api_key,
        "api_base": api_base,
        "num_retries": 0,
        "timeout": 180,
        "max_tokens": MAX_OUTPUT_TOKENS,
    }
    if spec["channel"] == "openrouter":
        # LiteLLM 1.98.0's bundled support table lagged the current OpenRouter
        # endpoint metadata. The pinned official endpoints explicitly advertise
        # reasoning_effort, so allow that field through unchanged.
        params["allowed_openai_params"] = ["reasoning_effort"]
        params["extra_headers"] = {"X-OpenRouter-Metadata": "enabled"}
    elif spec["harness"] == "codex":
        # The Codex request controls effort; allow the Chat bridge to forward it
        # unchanged while leaving vendor thinking-mode defaults untouched.
        params["allowed_openai_params"] = ["reasoning_effort"]
    if spec["harness"] == "codex":
        if spec["channel"] == "openrouter":
            # OpenRouter exposes Responses natively. LiteLLM only injects the
            # audited route and any explicitly approved compatibility drops.
            params["use_chat_completions_api"] = False
            drop_params = spec.get("compatibility_drop_params") or []
            if drop_params:
                params["additional_drop_params"] = drop_params
        else:
            # The GLM/Kimi pay-as-you-go endpoints expose Chat Completions while
            # Codex 0.150.1 is Responses-only, so use the pinned protocol bridge.
            params["use_chat_completions_api"] = True
    if extra_body:
        params["extra_body"] = extra_body
    return {
        "model_list": [{"model_name": "pilot-model", "litellm_params": params}],
        "litellm_settings": {"num_retries": 0, "request_timeout": 180},
    }


def start_proxy(spec: dict[str, Any], trial: Path) -> subprocess.Popen[str]:
    config_path = trial / "litellm.yaml"
    # JSON is valid YAML and keeps the pilot runner independent of a host YAML library.
    config_path.write_text(json.dumps(proxy_config(spec), indent=2) + "\n")
    log = (trial / "litellm.log").open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            "/opt/litellm/bin/litellm",
            "--config",
            str(config_path),
            "--host",
            "127.0.0.1",
            "--port",
            "4000",
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("LiteLLM proxy exited during startup")
        try:
            with urllib.request.urlopen(
                "http://127.0.0.1:4000/health/liveliness", timeout=1
            ) as response:
                if response.status == 200:
                    return process
        except Exception:
            time.sleep(0.25)
    process.terminate()
    raise RuntimeError("LiteLLM proxy did not become live")


def start_codex_recorder(trial: Path) -> subprocess.Popen[str]:
    """Start Codex's transparent, redacting Responses exchange recorder."""
    dump_dir = trial / "codex-responses-dumps"
    log = (trial / "codex-recorder.log").open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            "codex",
            "responses-api-proxy",
            "--port",
            "4001",
            "--upstream-url",
            "http://127.0.0.1:4000/v1/responses",
            "--dump-dir",
            str(dump_dir),
        ],
        stdin=subprocess.PIPE,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
    )
    log.close()
    assert process.stdin is not None
    process.stdin.write("pilot-local-recorder\n")
    process.stdin.close()

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Codex Responses recorder exited during startup")
        try:
            with socket.create_connection(("127.0.0.1", 4001), timeout=1):
                return process
        except OSError:
            time.sleep(0.25)
    process.terminate()
    raise RuntimeError("Codex Responses recorder did not become live")


def write_pi_config(spec: dict[str, Any], config_dir: Path) -> tuple[str, str]:
    config_dir.mkdir(parents=True, exist_ok=True)
    model_id = spec["actual_model"]
    provider = "openrouter" if spec["channel"] == "openrouter" else {
        "glm-5.3": "zai",
        "deepseek-v4-pro": "deepseek",
        "kimi-k3": "moonshotai",
    }[spec["logical_model"]]
    model_max_tokens = spec.get("pi_model_max_tokens", MAX_OUTPUT_TOKENS)
    model_override: dict[str, Any] = {}
    if model_max_tokens is not None:
        model_override.update(
            {
                "maxTokens": model_max_tokens,
                "samplingParams": {"max_tokens": model_max_tokens},
            }
        )
    if spec.get("pi_model_context_window") is not None:
        model_override["contextWindow"] = spec["pi_model_context_window"]
    if spec.get("pi_model_input") is not None:
        model_override["input"] = spec["pi_model_input"]
    if spec["channel"] == "openrouter":
        # PI otherwise emits max_completion_tokens for generic OpenRouter
        # models, while the pinned official endpoints advertise max_tokens.
        # This is a name-only compatibility mapping; the 128-token cap is kept.
        model_override["compat"] = {
            "maxTokensField": "max_tokens",
            "openRouterRouting": spec["openrouter_route"],
        }
        provider_cfg: dict[str, Any] = {
            "apiKey": "$OPENROUTER_API_KEY",
            "headers": {"X-OpenRouter-Metadata": "enabled"},
            "modelOverrides": {model_id: model_override},
        }
    else:
        model = MODELS[spec["logical_model"]]
        provider_cfg = {
            "baseUrl": model["chat_base_url"],
            "apiKey": f"${spec['key_env']}",
            "modelOverrides": {model_id: model_override},
        }
    write_json(config_dir / "models.json", {"providers": {provider: provider_cfg}}, mode=0o600)
    return provider, model_id


def write_codex_config(spec: dict[str, Any], codex_home: Path) -> None:
    codex_home.mkdir(parents=True, exist_ok=True)
    if spec.get("proxy_required"):
        base_url = "http://127.0.0.1:4001/v1"
        env_key = "PILOT_PROXY_API_KEY"
        model_id = "pilot-model"
    else:
        base_url = MODELS[spec["logical_model"]]["responses_base_url"]
        env_key = spec["key_env"]
        model_id = spec["actual_model"]
    config = f'''model = "{model_id}"
model_provider = "pilot"
model_reasoning_effort = "low"
model_reasoning_summary = "none"

[model_providers.pilot]
name = "pilot"
base_url = "{base_url}"
env_key = "{env_key}"
wire_api = "responses"
request_max_retries = 0
stream_max_retries = 0
stream_idle_timeout_ms = 180000
'''
    (codex_home / "config.toml").write_text(config)


def write_dsh_patch(spec: dict[str, Any], path: Path) -> None:
    if spec["channel"] == "direct" and spec["logical_model"] == "deepseek-v4-pro":
        patch: list[dict[str, Any]] = [
            {
                "id": "llm-deepseek",
                "config": {
                    "apiKeyEnv": "DEEPSEEK_API_KEY",
                    "baseURL": "https://api.deepseek.com",
                    "reasoningEffort": "low",
                    "maxTokens": MAX_OUTPUT_TOKENS,
                    "retryPolicy": {"mode": "normal", "maxRetries": 0},
                },
            },
            {
                "id": "agent-default-model",
                "config": {"provider": "deepseek-official", "model": "deepseek-v4-pro"},
            },
        ]
    else:
        provider_retries = (
            1
            if spec["channel"] == "direct" and spec["logical_model"] == "kimi-k3"
            else 0
        )
        if spec["channel"] == "openrouter":
            base_url = "http://127.0.0.1:4000/v1"
            api_key_env = "PILOT_PROXY_API_KEY"
            model_id = "pilot-model"
            compat: dict[str, Any] = {"supportsDeveloperRole": False, "maxTokensField": "max_tokens"}
        else:
            model = MODELS[spec["logical_model"]]
            base_url = model["chat_base_url"]
            api_key_env = spec["key_env"]
            model_id = spec["actual_model"]
            compat = {
                "supportsDeveloperRole": False,
                "maxTokensField": "max_tokens",
                "thinkingFormat": "zai" if spec["logical_model"] == "glm-5.3" else "openai",
            }
        route = "pilot-route"
        patch = [
            {
                "id": "llm-pi-ai",
                "config": {
                    "providers": {
                        route: {
                            "displayName": "Pilot route",
                            "apiKeyEnv": api_key_env,
                            "api": "openai-completions",
                            "baseURL": base_url,
                            "reasoning": "low",
                            "retryPolicy": {
                                "mode": "normal",
                                "maxRetries": provider_retries,
                            },
                            "models": [
                                {
                                    "id": model_id,
                                    "contextWindow": 1_000_000,
                                    "maxTokens": MAX_OUTPUT_TOKENS,
                                    "reasoningEfforts": {"off": None, "low": "low"},
                                    "compat": compat,
                                }
                            ],
                        }
                    }
                },
            },
            {"id": "agent-default-model", "config": {"provider": route, "model": model_id}},
        ]
    patch.append(
        {
            "id": "session-persistence-jsonl",
            "config": {
                "root": "/trial/home/.dsh/sessions-jsonl",
                "compression": "none",
                "packChunks": False,
            },
        }
    )
    # DSH's YAML parser accepts JSON, which is a strict YAML subset.
    path.write_text(json.dumps(patch, indent=2) + "\n")


def apply_openhands_openrouter_compat(
    options: dict[str, Any], spec: dict[str, Any]
) -> dict[str, Any]:
    """Map only the output-limit field required by the pinned OR endpoints."""
    if spec["channel"] == "openrouter":
        value = options.pop("max_completion_tokens", None)
        if value is not None:
            options["max_tokens"] = value
    return options


def validate_openhands_install_and_compat(spec: dict[str, Any]) -> None:
    """Exercise the installed SDK and local field mapping without an API call."""
    from openhands.sdk.llm import llm as openhands_llm_module

    if importlib.metadata.version("openhands-sdk") != "1.44.1":
        raise RuntimeError("unexpected OpenHands SDK version")
    if not callable(openhands_llm_module.select_chat_options):
        raise RuntimeError("OpenHands select_chat_options is unavailable")
    probe = apply_openhands_openrouter_compat(
        {
            "max_completion_tokens": MAX_OUTPUT_TOKENS,
            "reasoning_effort": "low",
            "tools": [],
        },
        spec,
    )
    if spec["channel"] == "openrouter":
        if probe.get("max_tokens") != MAX_OUTPUT_TOKENS:
            raise RuntimeError("OpenHands output-limit compatibility mapping failed")
        if "max_completion_tokens" in probe:
            raise RuntimeError("OpenHands unsupported output-limit field remains")
        if probe.get("reasoning_effort") != "low" or probe.get("tools") != []:
            raise RuntimeError("OpenHands compatibility mapping changed other fields")


def run_openhands(spec: dict[str, Any], trial: Path, env: dict[str, str]) -> int:
    from openhands.sdk import LLM, Agent, AgentContext, Conversation, Tool
    from openhands.sdk.llm import llm as openhands_llm_module
    from openhands.tools.file_editor import FileEditorTool
    from openhands.tools.task_tracker import TaskTrackerTool
    from openhands.tools.terminal import TerminalTool

    if spec["channel"] == "openrouter":
        model_name = f"openrouter/{spec['actual_model']}"
        base_url = "https://openrouter.ai/api/v1"
        key = env["OPENROUTER_API_KEY"]
        extra_body = {"provider": spec["openrouter_route"]}
        extra_headers = {"X-OpenRouter-Metadata": "enabled"}
    else:
        model = MODELS[spec["logical_model"]]
        model_name = model["openhands_model"]
        base_url = model["chat_base_url"]
        key = env[spec["key_env"]]
        extra_body = {}
        extra_headers = None
        if spec["logical_model"] in {"glm-5.3", "deepseek-v4-pro"}:
            extra_body = {"thinking": {"type": "enabled"}}

    kwargs: dict[str, Any] = {
        "model": model_name,
        "api_key": key,
        "base_url": base_url,
        "reasoning_effort": "low",
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "num_retries": 0,
        "timeout": 180,
    }
    if extra_body:
        kwargs["litellm_extra_body"] = extra_body
    if extra_headers:
        kwargs["extra_headers"] = extra_headers
    llm = LLM(**kwargs)
    tools = [
        Tool(name=TerminalTool.name),
        Tool(name=FileEditorTool.name),
        Tool(name=TaskTrackerTool.name),
    ]
    agent = Agent(llm=llm, tools=tools, agent_context=AgentContext(skills=[]))
    conversation = Conversation(
        agent=agent,
        workspace=str(trial / "workspace"),
        max_iteration_per_run=MAX_AGENT_TURNS,
    )
    original_select_chat_options = openhands_llm_module.select_chat_options

    def select_chat_options_compat(*args: Any, **call_kwargs: Any) -> dict[str, Any]:
        options = original_select_chat_options(*args, **call_kwargs)
        # OpenHands 1.44.1 emits max_completion_tokens, but the pinned official
        # OpenRouter endpoints advertise max_tokens. Preserve the value and
        # change only the field name.
        return apply_openhands_openrouter_compat(options, spec)

    openhands_llm_module.select_chat_options = select_chat_options_compat
    try:
        conversation.send_message(PROMPT)
        conversation.run()
    finally:
        openhands_llm_module.select_chat_options = original_select_chat_options
    token_usage = llm.metrics.accumulated_token_usage
    messages: list[str] = []
    for event in conversation.state.events:
        if getattr(event, "source", None) == "agent" and getattr(event, "llm_message", None):
            content = getattr(event.llm_message, "content", None)
            if isinstance(content, list):
                messages.extend(
                    str(getattr(part, "text", ""))
                    for part in content
                    if getattr(part, "text", None)
                )
    serialized_events: list[Any] = []
    for event in conversation.state.events:
        if hasattr(event, "model_dump"):
            serialized_events.append(event.model_dump(mode="json"))
        else:
            serialized_events.append({"repr": repr(event)})
    result = {
        "source": "openhands-sdk-litellm-metrics",
        "prompt_tokens": token_usage.prompt_tokens if token_usage else 0,
        "completion_tokens": token_usage.completion_tokens if token_usage else 0,
        "cached_tokens": token_usage.cache_read_tokens if token_usage else 0,
        "cost_usd": llm.metrics.accumulated_cost,
        "messages": messages,
        "skills_loaded": 0,
        "mcp_servers": 0,
        "event_count": len(serialized_events),
    }
    write_json(trial / "openhands-result.json", result)
    write_json(trial / "openhands-events.json", serialized_events)
    (trial / "harness.log").write_text("\n".join(messages) + "\n")
    return 0


def inside_cell(spec_path: Path, *, validate: bool) -> int:
    import tomllib

    spec = json.loads(spec_path.read_text())
    trial = Path("/trial")
    workspace = trial / "workspace"
    home = trial / "home"
    workspace.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "PILOT_PROXY_API_KEY": "pilot-local-proxy",
            "PI_TELEMETRY": "0",
            "DSH_TELEMETRY_MODE": "DISABLED",
        }
    )
    proxy: subprocess.Popen[str] | None = None
    codex_recorder: subprocess.Popen[str] | None = None
    needs_proxy = spec.get("proxy_required", spec["translated"])

    try:
        if validate:
            os.environ.setdefault(spec["key_env"], "validation-dummy")
        if needs_proxy:
            proxy = start_proxy(spec, trial)
            if spec["harness"] == "codex":
                codex_recorder = start_codex_recorder(trial)

        harness = spec["harness"]
        if harness == "pi":
            config_dir = home / ".pi" / "agent"
            provider, model_id = write_pi_config(spec, config_dir)
            env["PI_CODING_AGENT_DIR"] = str(config_dir)
            env["PI_OFFLINE"] = "1"
            if spec["logical_model"] == "kimi-k3" and spec["channel"] == "direct":
                env["MOONSHOT_API_KEY"] = env["KIMI_API_KEY"]
            command = [
                "pi",
                "--list-models",
                model_id,
            ] if validate else [
                "pi",
                "--print",
                "--mode",
                "json",
                "--no-session",
                "--provider",
                provider,
                "--model",
                model_id,
                "--thinking",
                "low",
                "--",
                PROMPT,
            ]
        elif harness == "claude-code":
            if validate:
                return 0
            if spec["translated"]:
                env["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:4000"
                env["ANTHROPIC_API_KEY"] = "pilot-local-proxy"
                model_id = "pilot-model"
            elif spec["channel"] == "openrouter":
                env["ANTHROPIC_BASE_URL"] = spec.get(
                    "anthropic_base_url_override", "https://openrouter.ai/api"
                )
                env["ANTHROPIC_AUTH_TOKEN"] = env["OPENROUTER_API_KEY"]
                env["ANTHROPIC_API_KEY"] = ""
                env["ANTHROPIC_CUSTOM_HEADERS"] = "X-OpenRouter-Metadata: enabled"
                model_id = spec["actual_model"]
            else:
                env["ANTHROPIC_BASE_URL"] = MODELS[spec["logical_model"]]["anthropic_base_url"]
                env["ANTHROPIC_API_KEY"] = env[spec["key_env"]]
                model_id = spec["actual_model"]
            command_model = spec.get("claude_code_cli_model", model_id)
            model_overrides = spec.get("claude_code_model_overrides")
            claude_config_dir = home / ".claude"
            if model_overrides:
                claude_config_dir.mkdir(parents=True, exist_ok=True)
                write_json(
                    claude_config_dir / "settings.json",
                    {"modelOverrides": model_overrides},
                )
            else:
                alias_model = spec.get("claude_code_alias_model", model_id)
                env.update(
                    {
                        "ANTHROPIC_MODEL": alias_model,
                        "ANTHROPIC_DEFAULT_SONNET_MODEL": alias_model,
                        "ANTHROPIC_DEFAULT_OPUS_MODEL": alias_model,
                        "ANTHROPIC_DEFAULT_HAIKU_MODEL": alias_model,
                        "CLAUDE_CODE_SUBAGENT_MODEL": spec.get(
                            "claude_code_subagent_model", alias_model
                        ),
                    }
                )
            env.update(
                {
                    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                    "IS_SANDBOX": "1",
                    "CLAUDE_CONFIG_DIR": str(claude_config_dir),
                }
            )
            configured_output_tokens = spec.get(
                "claude_code_max_output_tokens", MAX_OUTPUT_TOKENS
            )
            if configured_output_tokens is not None:
                env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(configured_output_tokens)
            context_tokens = spec.get("claude_code_context_tokens")
            if context_tokens is not None:
                env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(context_tokens)
            auto_compact_tokens = spec.get("claude_code_auto_compact_tokens")
            if auto_compact_tokens is not None:
                env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(auto_compact_tokens)
            command = [
                "claude",
                "--verbose",
                "--output-format=stream-json",
                "--print",
                "--permission-mode=bypassPermissions",
                "--max-turns",
                str(MAX_AGENT_TURNS),
                "--effort",
                spec.get("reasoning_effort", "low"),
                "--model",
                command_model,
                "--no-session-persistence",
                PROMPT,
            ]
            max_budget_usd = spec.get("claude_code_max_budget_usd", 0.25)
            if max_budget_usd is not None:
                command[command.index("--effort"):command.index("--effort")] = [
                    "--max-budget-usd",
                    str(max_budget_usd),
                ]
        elif harness == "codex":
            codex_home = home / ".codex"
            write_codex_config(spec, codex_home)
            with (codex_home / "config.toml").open("rb") as handle:
                tomllib.load(handle)
            if validate:
                # Force Codex itself to parse --strict-config while removing the
                # configured credential so validation stops before network I/O.
                validation_env = env.copy()
                validation_env["CODEX_HOME"] = str(codex_home)
                validation_env.pop(
                    "PILOT_PROXY_API_KEY"
                    if spec.get("proxy_required")
                    else spec["key_env"],
                    None,
                )
                completed = subprocess.run(
                    [
                        "codex",
                        "exec",
                        "--strict-config",
                        "--skip-git-repo-check",
                        "--json",
                        "--model",
                        "pilot-model"
                        if spec.get("proxy_required")
                        else spec["actual_model"],
                        "--",
                        PROMPT,
                    ],
                    cwd=workspace,
                    env=validation_env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=15,
                    check=False,
                )
                (trial / "validation-inner.log").write_text(completed.stdout)
                return 2 if "unknown configuration field" in completed.stdout else 0
            env["CODEX_HOME"] = str(codex_home)
            model_id = (
                "pilot-model" if spec.get("proxy_required") else spec["actual_model"]
            )
            command = [
                "codex",
                "exec",
                "--strict-config",
                "--dangerously-bypass-approvals-and-sandbox",
                "--skip-git-repo-check",
                "--json",
                "--model",
                model_id,
                "--",
                PROMPT,
            ]
        elif harness == "openhands":
            if validate:
                validate_openhands_install_and_compat(spec)
                return 0
            return run_openhands(spec, trial, env)
        elif harness == "deepseek-harness":
            patch_path = trial / "dsh-patch.yaml"
            write_dsh_patch(spec, patch_path)
            env["DSH_HOME"] = str(home / ".dsh")
            command = [
                "dsh",
                "--profile",
                "headless",
                "--patch",
                str(patch_path),
                "--dump-config",
            ] if validate else [
                "dsh",
                "--profile",
                "headless",
                "--patch",
                str(patch_path),
                PROMPT,
            ]
        else:
            raise ValueError(f"unknown harness: {harness}")

        output_path = trial / ("validation-inner.log" if validate else "harness.log")
        with output_path.open("w", encoding="utf-8") as output:
            completed = subprocess.run(
                command,
                cwd=workspace,
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=60 if validate else CELL_TIMEOUT_SECONDS,
                check=False,
            )
        return completed.returncode
    except subprocess.TimeoutExpired:
        return 124
    except Exception as exc:
        (trial / "harness.log").write_text(
            f"pilot runner exception: {type(exc).__name__}: {exc}\n"
        )
        return 125
    finally:
        if codex_recorder is not None and codex_recorder.poll() is None:
            codex_recorder.terminate()
            try:
                codex_recorder.wait(timeout=5)
            except subprocess.TimeoutExpired:
                codex_recorder.kill()
                codex_recorder.wait()
        if proxy is not None and proxy.poll() is None:
            proxy.terminate()
            try:
                proxy.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proxy.kill()
                proxy.wait()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("preflight", help="zero-cost disposable config validation")
    run = subparsers.add_parser("run", help="freeze and launch the approved pilot")
    run.add_argument("--approved-manifest-sha256", required=True)
    cell = subparsers.add_parser("_cell", help=argparse.SUPPRESS)
    cell.add_argument("--spec", type=Path, required=True)
    cell.add_argument("--validate", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "preflight":
        return host_preflight()
    if args.command == "run":
        return host_run(args.approved_manifest_sha256)
    if args.command == "_cell":
        return inside_cell(args.spec, validate=args.validate)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
