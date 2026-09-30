#!/usr/bin/env python3
"""Claude Code 1M selector repair qualification for the Claude study arm.

The existing audited Anthropic Messages injector is reused unchanged and adds
only OpenRouter's top-level ``provider`` object.  This campaign repairs the r2
runner omission of Claude Code's official ``[1m]`` model selector.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import claude_openrouter_pin_smoke as pin
import provider_smoke as base
import provider_tool_smoke as tool_base


CAMPAIGN_ID = "pilot-claude-code-five-model-controls-20260831-r3"
PREPARED_AT_UTC = "2026-08-31T07:54:00Z"
CONTEXT_TOKENS = 1_000_000
AUTO_COMPACT_TOKENS = 1_000_000
MAX_OUTPUT_TOKENS = 64_000
MAX_AGENT_TURNS = 4
CELL_TIMEOUT_SECONDS = 900
CAMPAIGN_SOFT_LIMIT_USD = 1.0


TARGET_MODELS: dict[str, dict[str, Any]] = {
    name: copy.deepcopy(model)
    for name, model in {
        **base.MODELS,
        "deepseek-v4-pro": tool_base.PRESET_MODELS["deepseek-v4-pro"],
    }.items()
}

# Claude Code's full Anthropic Messages body is accepted by the Anthropic
# endpoint with parameter filtering enabled.  The four cross-family endpoints
# need require_parameters=false; provider.only and fallback=false remain strict.
for logical_model, model in TARGET_MODELS.items():
    if logical_model != "claude-opus-5":
        model["openrouter_route"]["require_parameters"] = False


ENDPOINT_SNAPSHOTS: dict[str, dict[str, Any]] = {
    "claude-opus-5": {
        "model": "anthropic/claude-opus-5",
        "name": "Anthropic | anthropic/claude-opus-5-20260723",
        "provider_name": "Anthropic",
        "tag": "anthropic",
        "quantization": "unknown",
        "context_length": 1_000_000,
        "max_completion_tokens": 128_000,
        "pricing": {
            "prompt": "0.000005",
            "completion": "0.000025",
            "input_cache_read": "0.0000005",
            "input_cache_write": "0.00000625",
        },
        "status": 0,
    },
    "gpt-6-astra": {
        "model": "openai/gpt-6-astra",
        "name": "OpenAI | openai/gpt-6-astra-20260903",
        "provider_name": "OpenAI",
        "tag": "openai",
        "quantization": "unknown",
        "context_length": 1_050_000,
        "max_completion_tokens": 128_000,
        "pricing": {
            "prompt": "0.000002",
            "completion": "0.00001",
            "input_cache_read": "0.0000002",
            "long_context_override_after_prompt_tokens": 272_000,
        },
        "status": 0,
    },
    "glm-5.3": {
        "model": "z-ai/glm-5.3",
        "name": "Z.AI | z-ai/glm-5.3-20260816",
        "provider_name": "Z.AI",
        "tag": "z-ai/fp8",
        "quantization": "fp8",
        "context_length": 1_048_576,
        "max_completion_tokens": 131_072,
        "pricing": {
            "prompt": "0.0000014",
            "completion": "0.0000044",
            "input_cache_read": "0.00000026",
        },
        "status": 0,
    },
    "kimi-k3": {
        "model": "moonshotai/kimi-k3",
        "name": "Moonshot AI | moonshotai/kimi-k3-20260715",
        "provider_name": "Moonshot AI",
        "tag": "moonshotai/mxfp4",
        "quantization": "mxfp4",
        "context_length": 1_048_576,
        "max_completion_tokens": 943_718,
        "pricing": {
            "prompt": "0.000003",
            "completion": "0.000015",
            "input_cache_read": "0.0000003",
        },
        "status": 0,
    },
    "deepseek-v4-pro": {
        "model": "deepseek/deepseek-v4-pro-0813",
        "name": "DeepSeek | deepseek/deepseek-v4-pro-20260813",
        "provider_name": "DeepSeek",
        "tag": "deepseek",
        "quantization": "unknown",
        "context_length": 1_048_576,
        "max_completion_tokens": 384_000,
        "pricing": {
            "prompt_standard": "0.00000132",
            "completion_standard": "0.00000396",
            "input_cache_read_standard": "0.000000044",
            "time_dependent_discount": True,
        },
        "status": 0,
    },
}


def build_cells() -> list[dict[str, Any]]:
    order = ("claude-opus-5",)
    cells: list[dict[str, Any]] = []
    for logical_model in order:
        model = TARGET_MODELS[logical_model]
        cell_id = f"cc-exact-controls--{logical_model}"
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
                "compatibility_variant": "transparent-provider-body-injector",
                "compatibility_drop_params": [],
                "run_only_if_all_prior_failed": [],
                "run_only_if_all_prior_passed": [],
                "base_url": pin.UPSTREAM_URL,
                "anthropic_base_url_override": pin.INJECTOR_URL,
                "openrouter_route": copy.deepcopy(model["openrouter_route"]),
                "route_control": (
                    "provider.only + quantization when declared + "
                    "allow_fallbacks=false"
                ),
                "expected_endpoint": copy.deepcopy(ENDPOINT_SNAPSHOTS[logical_model]),
                "reasoning_effort": "high",
                "hard_output_limit": True,
                "claude_code_context_tokens": CONTEXT_TOKENS,
                "claude_code_auto_compact_tokens": AUTO_COMPACT_TOKENS,
                "claude_code_max_budget_usd": None,
                "claude_code_cli_model": model["openrouter_model"] + "[1m]",
                "claude_code_alias_model": model["openrouter_model"] + "[1m]",
                "claude_code_subagent_model": model["openrouter_model"] + "[1m]",
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
        "-v",
        f"{base.ROOT / 'pilot'}:/pilot:ro",
        "-v",
        f"{cell_dir}:/trial",
        "-w",
        "/trial/workspace",
        base.IMAGE,
        "/opt/openhands/bin/python",
        "/pilot/claude_code_model_qualification.py",
        "_cell",
        "--spec",
        "/trial/spec.json",
    ]
    if validate:
        command.append("--validate")
    return command


def build_manifest() -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "campaign_id": CAMPAIGN_ID,
        "kind": "paid-claude-code-five-model-exact-control-tool-qualification",
        "prepared_at_utc": PREPARED_AT_UTC,
        "approval": {
            "required_before_launch": True,
            "approval_target": "campaign ID plus manifest SHA256",
            "launch_requires_cli_hash_match": True,
        },
        "scope": {
            "benchmark": None,
            "task": "one fixed two-round terminal tool loop for the Claude 1M repair",
            "replicate": 1,
            "planned_trials": 1,
            "phase_gate": "single repaired Claude cell; no dependent paid cells",
            "phase_concurrency": 1,
            "prompt": tool_base.PROMPT,
            "quality_claim": (
                "transport, exact-control acceptance, provider pinning, and one observed "
                "terminal tool loop only; not benchmark or compaction evidence"
            ),
        },
        "cells": build_cells(),
        "qualification_parent": {
            "campaign_id": "pilot-claude-code-five-model-controls-20260831-r2",
            "manifest_sha256": "257df88a33d829af1adab3b4b28c4b7a96e437aba7aaf33f81b7bc7ac8e1442c",
            "cell": "cc-exact-controls--claude-opus-5",
            "result": "route and tool loop passed, but client context telemetry was 200K",
            "root_cause": (
                "r2 set context/compaction environment controls but omitted Claude "
                "Code's official [1m] model selector"
            ),
        },
        "runner": {
            "entry_command": (
                "python pilot/claude_code_model_qualification.py run "
                "--approved-manifest-sha256 <sha256>"
            ),
            "harness": "@anthropic-ai/claude-code@2.1.251",
            "image": base.IMAGE,
            "image_id": base.IMAGE_ID,
            "sandbox": "isolated Docker container; 2 CPU, 4 GiB per cell",
            "cell_timeout_seconds": CELL_TIMEOUT_SECONDS,
            "outer_retry": 0,
        },
        "model_paths": {
            "claude-opus-5": {
                "requested_model": TARGET_MODELS["claude-opus-5"]["openrouter_model"],
                "claude_code_selector": "anthropic/claude-opus-5[1m]",
                "provider_receives_model": "anthropic/claude-opus-5",
                "route": TARGET_MODELS["claude-opus-5"]["openrouter_route"],
                "snapshot": ENDPOINT_SNAPSHOTS["claude-opus-5"],
            }
        },
        "controls": {
            "reasoning_effort": "high via Claude Code --effort high",
            "thinking": "adaptive provider behavior; no MAX_THINKING_TOKENS override",
            "context_window": (
                "official full-model [1m] selector for main/default/subagent model plus "
                "CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000; Claude Code strips [1m] before "
                "sending the provider model ID"
            ),
            "compaction": (
                "CLAUDE_CODE_AUTO_COMPACT_WINDOW=1000000; no trigger expected in this "
                "short qualification and no recovery claim is made"
            ),
            "output_budget": (
                "CLAUDE_CODE_MAX_OUTPUT_TOKENS=64000, the approved within-Claude-Code "
                "normalization to the known Opus path; unknown-model modelUsage metadata "
                "may still say 32000, so the injector wire record is authoritative"
            ),
            "max_turns": 4,
            "task_timeout_seconds": CELL_TIMEOUT_SECONDS,
            "temperature": "omitted; provider default",
            "top_p": "omitted; provider default",
            "seed": None,
            "tools": "Claude Code installed defaults; prompt requires exactly one Bash call",
            "skills": [],
            "mcp_servers": [],
            "subagents": (
                "default tool remains available but prompt forbids use; "
                "CLAUDE_CODE_SUBAGENT_MODEL equals the same [1m] main model"
            ),
            "memory": "empty isolated HOME; session persistence disabled",
            "benchmark_specific_augmentation": False,
            "retries": {
                "claude_code": "native default, every upstream attempt logged",
                "injector": 0,
                "outer": 0,
            },
        },
        "compatibility_layer": {
            "protocol": "Anthropic Messages in and out; raw SSE passthrough",
            "semantic_request_change": "add top-level provider object only",
            "does_not_change": [
                "model",
                "system",
                "messages",
                "tools",
                "thinking",
                "response bytes",
            ],
            "require_parameters": (
                "true for Anthropic; false for cross-family endpoints because Claude "
                "Code's complete Messages body otherwise fails capability filtering"
            ),
        },
        "budget": {
            "expected_total_usd": "approximately $0.10-$0.50",
            "campaign_soft_limit_usd": CAMPAIGN_SOFT_LIMIT_USD,
            "accepted_exposure_usd": CAMPAIGN_SOFT_LIMIT_USD,
            "hard_provider_cap": False,
            "stop_behavior": "one cell is allowed to finish; no process-group kill",
        },
        "accounting": {
            "primary": "OpenRouter per-generation cost and route metadata",
            "secondary": [
                "Claude Code native stream-json usage",
                "injector one-record-per-attempt audit",
                "OpenRouter current-key before/after snapshot",
            ],
            "double_count_rule": "never sum duplicate reports of the same call",
        },
        "post_run_audit": {
            "required": True,
            "checks": [
                "exact actual model/provider and strict no-fallback route",
                "max_tokens=64000, effort=high, tools and signed thinking structure",
                "terminal call/result/replay/final marker and artifact",
                "all native retries, 429, protocol errors, tokens and cost",
                "no skills, MCP, subagent, memory or prompt contamination",
            ],
        },
        "source_sha256": {
            "pilot/claude_code_model_qualification.py": base.sha256_file(Path(__file__)),
            "pilot/claude_openrouter_pin_smoke.py": base.sha256_file(
                base.ROOT / "pilot" / "claude_openrouter_pin_smoke.py"
            ),
            "pilot/provider_smoke.py": base.sha256_file(
                base.ROOT / "pilot" / "provider_smoke.py"
            ),
        },
    }
    manifest["manifest_sha256"] = hashlib.sha256(
        base.canonical_json(manifest)
    ).hexdigest()
    return manifest


def configure_modules() -> None:
    base.CAMPAIGN_ID = CAMPAIGN_ID
    base.MANIFEST_PREPARED_AT_UTC = PREPARED_AT_UTC
    base.RUN_DIR = base.ROOT / "runs" / CAMPAIGN_ID
    base.REPORT_PATH = base.RUN_DIR / "report.md"
    base.MARKER = tool_base.MARKER
    base.PROMPT = tool_base.PROMPT
    base.HARNESSES = ("claude-code",)
    base.MODELS = TARGET_MODELS
    base.SOFT_LIMITS_USD = {"OPENROUTER_API_KEY": CAMPAIGN_SOFT_LIMIT_USD}
    base.UNKNOWN_CALL_RESERVE_USD = 0.25
    base.MAX_OUTPUT_TOKENS = MAX_OUTPUT_TOKENS
    base.MAX_AGENT_TURNS = MAX_AGENT_TURNS
    base.CELL_TIMEOUT_SECONDS = CELL_TIMEOUT_SECONDS
    base.docker_command = docker_command
    base.build_cells = build_cells
    base.build_manifest = build_manifest
    base.parse_harness_telemetry = pin.parse_harness_telemetry
    base.assistant_marker_seen = pin.assistant_marker_and_artifact_seen
    pin.TARGET_MODELS = TARGET_MODELS
    pin.build_cells = build_cells


def validate_cells(manifest: dict[str, Any], root: Path) -> None:
    for cell in manifest["cells"]:
        cell_dir = root / cell["cell_id"]
        (cell_dir / "workspace").mkdir(parents=True)
        (cell_dir / "home").mkdir(parents=True)
        base.write_json(cell_dir / "spec.json", cell)
        completed = subprocess.run(
            docker_command(cell_dir, validate=True),
            cwd=base.ROOT,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=90,
        )
        (cell_dir / "validation.log").write_text(completed.stdout)
        if completed.returncode != 0:
            raise RuntimeError(f"zero-cost validation failed for {cell['cell_id']}")


def run_one(cell: dict[str, Any], env_file: Path) -> dict[str, Any]:
    cell_dir = base.RUN_DIR / "cells" / cell["cell_id"]
    launch_env = os.environ.copy()
    key_value = base.load_env_value(env_file, cell["key_env"])
    if key_value:
        launch_env[cell["key_env"]] = key_value
    return_code, wall_seconds = base.run_subprocess(
        docker_command(cell_dir, validate=False),
        cwd=base.ROOT,
        env=launch_env,
        timeout=CELL_TIMEOUT_SECONDS + 20,
        log_path=cell_dir / "container.log",
    )
    telemetry = pin.parse_harness_telemetry("claude-code", cell_dir)
    generation: dict[str, Any] = {"records": [], "errors": []}
    if key_value:
        settlement_deadline = time.monotonic() + 90
        while True:
            generation = base.fetch_openrouter_generation_records(cell_dir, key_value)
            errors = generation.get("errors") or []
            if not errors or time.monotonic() >= settlement_deadline:
                break
            if any(item.get("status") != 404 for item in errors):
                break
            time.sleep(5)
        telemetry["openrouter_generation_api"] = generation
    marker_seen = pin.assistant_marker_and_artifact_seen(
        "claude-code", cell_dir, telemetry
    )
    provider_records = generation.get("records") or []
    expected_snapshot = cell["expected_endpoint"]
    expected_actual_model = expected_snapshot["name"].split(" | ", 1)[1]
    provider_route_verified = bool(provider_records) and not generation.get("errors") and all(
        item.get("provider_name") == expected_snapshot["provider_name"]
        and item.get("model") == expected_actual_model
        for item in provider_records
    )
    injector_records = (
        telemetry.get("transparent_provider_injector", {}).get("records") or []
    )
    message_records = [item for item in injector_records if item.get("path") == "/v1/messages"]
    controls_verified = bool(message_records) and all(
        (item.get("request_structure") or {}).get("max_tokens") == MAX_OUTPUT_TOKENS
        and ((item.get("request_structure") or {}).get("output_config") or {}).get("effort")
        == "high"
        and ((item.get("request_structure") or {}).get("thinking") or {}).get("type")
        == "adaptive"
        for item in message_records
    )
    context_values = base.scalar_numbers(
        telemetry.get("model_usage") or {}, {"contextWindow", "context_window"}
    )
    context_verified = CONTEXT_TOKENS in {int(value) for value in context_values}
    base.write_json(cell_dir / "telemetry.json", telemetry)
    charge, charge_source = base.budget_charge(telemetry, attempted=True)
    provider_cost = sum(
        float(item.get("total_cost") or item.get("usage") or 0)
        for item in provider_records
    )
    status = (
        "passed"
        if return_code == 0
        and marker_seen
        and provider_route_verified
        and controls_verified
        and context_verified
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
        "provider_route_verified": provider_route_verified,
        "controls_verified": controls_verified,
        "context_verified": context_verified,
        "wall_seconds": round(wall_seconds, 3),
        "provider_cost_usd": provider_cost,
        "budget_charge_usd": round(charge, 9),
        "budget_charge_source": charge_source,
        "usage_summary": base.usage_summary(telemetry),
        "completed_at_utc": base.utc_now(),
    }
    base.write_json(cell_dir / "result.json", result)
    return result


def run_approved(approved_manifest_sha256: str) -> int:
    configure_modules()
    if base.RUN_DIR.exists():
        print(f"ERROR immutable run directory already exists: {base.RUN_DIR}", file=sys.stderr)
        return 2
    manifest = build_manifest()
    if approved_manifest_sha256 != manifest["manifest_sha256"]:
        print(
            "ERROR approval hash mismatch; expected " + manifest["manifest_sha256"],
            file=sys.stderr,
        )
        return 2
    env_file = base.ROOT / ".env"
    key = base.load_env_value(env_file, "OPENROUTER_API_KEY")
    if not key:
        print("ERROR OPENROUTER_API_KEY is missing", file=sys.stderr)
        return 2

    base.RUN_DIR.mkdir(parents=True)
    base.write_json(base.RUN_DIR / "manifest.json", manifest, mode=0o444)
    base.write_json(
        base.RUN_DIR / "approval.json",
        {
            "campaign_id": CAMPAIGN_ID,
            "approved_manifest_sha256": approved_manifest_sha256,
            "recorded_at_utc": base.utc_now(),
        },
        mode=0o444,
    )
    validate_cells(manifest, base.RUN_DIR / "cells")
    before = tool_base.current_key_usage()
    base.write_json(base.RUN_DIR / "openrouter-usage-before.json", before, mode=0o444)

    cells = manifest["cells"]
    results: list[dict[str, Any]] = []
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=1) as executor:
        futures = {executor.submit(run_one, cell, env_file): cell for cell in cells}
        for future in as_completed(futures):
            result = future.result()
            with lock:
                results.append(result)
                base.write_json(base.RUN_DIR / "results.json", results)
            print(
                f"[{len(results)}/{len(cells)}] {result['status'].upper()} "
                f"{result['cell_id']} {result['wall_seconds']:.1f}s",
                flush=True,
            )

    after = tool_base.current_key_usage()
    usage = {"source": "openrouter-current-key", "before": before, "after": after}
    if isinstance(before.get("usage"), (int, float)) and isinstance(
        after.get("usage"), (int, float)
    ):
        usage["key_delta_usd"] = after["usage"] - before["usage"]
    base.write_json(base.RUN_DIR / "openrouter-usage-after.json", usage, mode=0o444)

    ordered = {cell["cell_id"]: i for i, cell in enumerate(cells)}
    results.sort(key=lambda item: ordered[item["cell_id"]])
    base.write_json(base.RUN_DIR / "results.json", results, mode=0o444)
    summary = {
        "campaign_id": CAMPAIGN_ID,
        "manifest_sha256": manifest["manifest_sha256"],
        "completed_at_utc": base.utc_now(),
        "counts": {
            status: sum(item["status"] == status for item in results)
            for status in sorted({item["status"] for item in results})
        },
        "provider_cost_usd_not_double_counted": sum(
            float(item.get("provider_cost_usd", 0)) for item in results
        ),
        "post_run_trajectory_audit_required": True,
    }
    base.write_json(base.RUN_DIR / "summary.json", summary, mode=0o444)
    base.write_report(manifest, results)
    return 0 if all(item["status"] == "passed" for item in results) else 1


def preflight() -> int:
    configure_modules()
    pin.offline_self_test()
    manifest = build_manifest()
    with tempfile.TemporaryDirectory(prefix="cc-claude-1m-preflight-", dir="/tmp") as raw:
        validate_cells(manifest, Path(raw))
    print(
        "Zero-cost exact-control preflight passed for "
        f"{len(manifest['cells'])} resolved model cells"
    )
    print(f"Prospective manifest SHA256: {manifest['manifest_sha256']}")
    print("No model API call was made")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("manifest")
    sub.add_parser("preflight")
    run = sub.add_parser("run")
    run.add_argument("--approved-manifest-sha256", required=True)
    proxy = sub.add_parser("_proxy", help=argparse.SUPPRESS)
    proxy.add_argument("--spec", type=Path, required=True)
    proxy.add_argument("--host", required=True)
    proxy.add_argument("--port", type=int, required=True)
    proxy.add_argument("--upstream", required=True)
    proxy.add_argument("--log-path", type=Path, required=True)
    cell = sub.add_parser("_cell", help=argparse.SUPPRESS)
    cell.add_argument("--spec", type=Path, required=True)
    cell.add_argument("--validate", action="store_true")
    args = parser.parse_args()
    configure_modules()
    if args.command == "manifest":
        print(json.dumps(build_manifest(), indent=2, ensure_ascii=False))
        return 0
    if args.command == "preflight":
        return preflight()
    if args.command == "run":
        return run_approved(args.approved_manifest_sha256)
    if args.command == "_proxy":
        return pin.run_proxy(args.spec, args.host, args.port, args.upstream, args.log_path)
    if args.command == "_cell":
        return pin.run_cell(args.spec, validate=args.validate)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
