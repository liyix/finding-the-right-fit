#!/usr/bin/env python3
"""Tool-loop recheck for Claude Code/PI and OpenRouter's DeepSeek endpoint."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import claude_openrouter_pin_smoke as pin
import provider_smoke as base
import provider_tool_smoke as tool


CAMPAIGN_ID = "pilot-openrouter-claude-pi-deepseek-official-20260831-r1"
PREPARED_AT_UTC = "2026-08-31T12:15:00Z"
MODEL = "deepseek/deepseek-v4-pro-0813"
EXPECTED_MODEL = "deepseek/deepseek-v4-pro-20260813"
CONTEXT_TOKENS = 1_048_576
MAX_MODEL_OUTPUT_TOKENS = 384_000
CELL_TIMEOUT_SECONDS = 900
MAX_AGENT_TURNS = 4
SOFT_LIMIT_USD = 1.0
ENDPOINT = {
    "name": f"DeepSeek | {EXPECTED_MODEL}",
    "provider_name": "DeepSeek",
    "tag": "deepseek",
    "quantization": "unknown",
    "context_length": CONTEXT_TOKENS,
    "max_completion_tokens": MAX_MODEL_OUTPUT_TOKENS,
    "status": 0,
}
MODEL_SPEC = {
    "openrouter_model": MODEL,
    "anthropic_base_url": "https://openrouter.ai/api",
    "openrouter_route": {
        "only": ["deepseek"],
        "allow_fallbacks": False,
        "require_parameters": False,
    },
    "endpoint": ENDPOINT,
}


def build_cells() -> list[dict[str, Any]]:
    common = {
        "channel": "openrouter",
        "logical_model": "deepseek-v4-pro",
        "actual_model": MODEL,
        "provider": "openrouter",
        "key_env": "OPENROUTER_API_KEY",
        "translated": False,
        "translation": None,
        "base_url": "https://openrouter.ai/api/v1",
        "expected_endpoint": ENDPOINT,
        "reasoning_effort": "high",
        "run_only_if_all_prior_failed": [],
        "hard_output_limit": False,
    }
    return [
        {
            **common,
            "cell_id": "deepseek-official--pi",
            "harness": "pi",
            "harness_version": "0.84.4",
            "protocol": "openai-chat-completions",
            "proxy_required": False,
            "openrouter_route": {
                "only": ["deepseek"],
                "allow_fallbacks": False,
                "require_parameters": True,
            },
            "route_control": "native PI OpenRouter body route",
            "pi_model_context_window": CONTEXT_TOKENS,
            "pi_model_max_tokens": MAX_MODEL_OUTPUT_TOKENS,
            "pi_model_input": ["text"],
        },
        {
            **common,
            "cell_id": "deepseek-official--claude-code",
            "harness": "claude-code",
            "harness_version": "2.1.251",
            "protocol": "anthropic-messages",
            "proxy_required": False,
            "body_injector_required": True,
            "compatibility_variant": "transparent-provider-body-injector",
            "compatibility_drop_params": [],
            "anthropic_base_url_override": pin.INJECTOR_URL,
            "openrouter_route": MODEL_SPEC["openrouter_route"],
            "route_control": "provider.only=deepseek; fallback=false",
            "claude_code_cli_model": MODEL,
            "claude_code_alias_model": MODEL,
            "claude_code_subagent_model": MODEL,
            "claude_code_context_tokens": 1_000_000,
            "claude_code_auto_compact_tokens": 1_000_000,
            "claude_code_max_output_tokens": None,
            "claude_code_max_budget_usd": None,
        },
    ]


def docker_command(cell_dir: Path, *, validate: bool) -> list[str]:
    command = [
        "docker", "run", "--rm",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "--cpus", "2", "--memory", "4g",
        "-e", "OPENROUTER_API_KEY",
        "-e", "HOME=/trial/home",
        "-e", "PI_TELEMETRY=0",
        "-v", f"{base.ROOT / 'pilot'}:/pilot:ro",
        "-v", f"{cell_dir}:/trial",
        "-w", "/trial/workspace",
        base.IMAGE,
        "/opt/openhands/bin/python",
        "/pilot/claude_pi_deepseek_recheck.py",
        "_cell", "--spec", "/trial/spec.json",
    ]
    if validate:
        command.append("--validate")
    return command


def build_manifest() -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "campaign_id": CAMPAIGN_ID,
        "kind": "paid-terminal-tool-loop-provider-recheck",
        "prepared_at_utc": PREPARED_AT_UTC,
        "approval": {
            "required_before_launch": True,
            "approval_target": "campaign ID plus manifest SHA256",
            "launch_requires_cli_hash_match": True,
        },
        "scope": {
            "benchmark": None,
            "task": "one fixed two-round terminal tool loop per harness",
            "planned_trials": 2,
            "replicate": 1,
            "concurrency": 1,
            "prompt": tool.PROMPT,
            "claim": "strict official-provider routing and one terminal loop only",
        },
        "runner": {
            "entry": "python pilot/claude_pi_deepseek_recheck.py run --approved-manifest-sha256 <sha>",
            "image": base.IMAGE,
            "image_id": base.IMAGE_ID,
            "outer_retries": 0,
            "cell_timeout_seconds": CELL_TIMEOUT_SECONDS,
        },
        "model_path": {
            "requested_model": MODEL,
            "expected_actual_model": EXPECTED_MODEL,
            "provider": "OpenRouter pinned to DeepSeek",
            "endpoint": ENDPOINT,
            "endpoint_check": "authenticated endpoint API returned exactly one active tag=deepseek endpoint immediately before freeze",
        },
        "cells": build_cells(),
        "controls": {
            "reasoning_effort": "high: PI --thinking high; Claude Code --effort high",
            "context": "PI catalog metadata 1,048,576; Claude Code study controls 1,000,000 without model-name spoofing",
            "output_budget": (
                "PI custom catalog uses the endpoint-advertised native 384,000 maximum; "
                "Claude Code has no CLAUDE_CODE_MAX_OUTPUT_TOKENS override and its exact wire field is audited"
            ),
            "compaction": "PI native; Claude Code native policy with 1,000,000 auto-compact control; no trigger expected",
            "max_turns": "PI native print loop; Claude Code 4 for this fixed two-round tool loop",
            "temperature": "omitted/provider default",
            "seed": None,
            "tools": "installed defaults; prompt requires exactly one terminal call",
            "skills": [], "mcp": [],
            "subagents": "not invoked; Claude aliases/subagent route to the same DeepSeek model",
            "memory": "empty isolated HOME and workspace",
            "retries": "harness-native only; injector and outer retry 0; every upstream attempt audited",
        },
        "compatibility": {
            "pi": "native OpenRouter Chat; models.json supplies endpoint metadata and the strict provider body",
            "claude_code": (
                "Anthropic Messages remains unchanged except a transparent local injector adds provider.only=deepseek, "
                "allow_fallbacks=false, require_parameters=false; no protocol translation"
            ),
        },
        "accounting": {
            "primary": "OpenRouter generation records/current-key delta",
            "secondary": "PI or Claude Code native counters plus injector request records",
            "double_count_rule": "never sum duplicate representations of one request",
            "expected_cost_usd": "$0.02-$0.30; accepted soft exposure $1.00",
        },
        "post_run_audit": {
            "required": True,
            "scope": "both complete trajectories and every upstream request",
            "checks": [
                "actual provider/model and no fallback", "tool call/result/replay/final marker",
                "wire reasoning and output fields", "hidden retries, 429, tokens, cost, and contamination",
            ],
        },
        "source_sha256": {
            "pilot/claude_pi_deepseek_recheck.py": base.sha256_file(Path(__file__)),
            "pilot/provider_smoke.py": base.sha256_file(base.ROOT / "pilot/provider_smoke.py"),
            "pilot/claude_openrouter_pin_smoke.py": base.sha256_file(base.ROOT / "pilot/claude_openrouter_pin_smoke.py"),
        },
    }
    manifest["manifest_sha256"] = hashlib.sha256(base.canonical_json(manifest)).hexdigest()
    return manifest


def parse_telemetry(harness: str, cell_dir: Path) -> dict[str, Any]:
    if harness == "claude-code":
        return pin.parse_harness_telemetry(harness, cell_dir)
    return tool.parse_harness_telemetry(harness, cell_dir)


def marker_seen(harness: str, cell_dir: Path, telemetry: dict[str, Any]) -> bool:
    if harness == "claude-code":
        return pin.assistant_marker_and_artifact_seen(harness, cell_dir, telemetry)
    return tool.assistant_marker_and_artifact_seen(harness, cell_dir, telemetry)


def configure() -> None:
    base.CAMPAIGN_ID = CAMPAIGN_ID
    base.MANIFEST_PREPARED_AT_UTC = PREPARED_AT_UTC
    base.RUN_DIR = base.ROOT / "runs" / CAMPAIGN_ID
    base.REPORT_PATH = base.RUN_DIR / "report.md"
    base.MARKER = tool.MARKER
    base.PROMPT = tool.PROMPT
    base.HARNESSES = ("pi", "claude-code")
    base.MODELS = {"deepseek-v4-pro": MODEL_SPEC}
    base.SOFT_LIMITS_USD = {"OPENROUTER_API_KEY": SOFT_LIMIT_USD}
    base.UNKNOWN_CALL_RESERVE_USD = 0.25
    base.MAX_OUTPUT_TOKENS = None
    base.MAX_AGENT_TURNS = MAX_AGENT_TURNS
    base.CELL_TIMEOUT_SECONDS = CELL_TIMEOUT_SECONDS
    base.docker_command = docker_command
    base.build_cells = build_cells
    base.build_manifest = build_manifest
    base.parse_harness_telemetry = parse_telemetry
    base.assistant_marker_seen = marker_seen
    pin.TARGET_MODELS = {"deepseek-v4-pro": MODEL_SPEC}


def inside(spec_path: Path, *, validate: bool) -> int:
    spec = json.loads(spec_path.read_text())
    configure()
    if spec["harness"] == "claude-code":
        return pin.run_cell(spec_path, validate=validate)
    return base.inside_cell(spec_path, validate=validate)


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight")
    run = sub.add_parser("run")
    run.add_argument("--approved-manifest-sha256", required=True)
    cell = sub.add_parser("_cell")
    cell.add_argument("--spec", type=Path, required=True)
    cell.add_argument("--validate", action="store_true")
    args = parser.parse_args()
    configure()
    if args.command == "preflight":
        pin.offline_self_test()
        return base.host_preflight()
    if args.command == "run":
        return tool.run_approved(args.approved_manifest_sha256)
    return inside(args.spec, validate=args.validate)


if __name__ == "__main__":
    raise SystemExit(main())
