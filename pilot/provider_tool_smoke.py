#!/usr/bin/env python3
"""Approved OpenRouter terminal-tool-loop pilot built on provider_smoke."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import provider_smoke as base


CAMPAIGN_ID = "pilot-openrouter-four-model-five-harness-tools-20260830-r1"
PREPARED_AT_UTC = "2026-08-30T07:43:24Z"
PRESET_CAMPAIGN_ID = "pilot-openrouter-claude-code-target-pinning-20260830-r1"
PRESET_PREPARED_AT_UTC = "2026-08-30T11:59:28Z"
MARKER = "TOOL_SMOKE_OK"
PROMPT = (
    "Terminal tool-loop test. You must make exactly one call to your default "
    "terminal or shell tool. In that tool call, run exactly this command; the "
    "backticks are delimiters: `printf %s TOOL_SMOKE_OK > bridge-check.txt && "
    "cat bridge-check.txt`. "
    "Do not simulate the command or use direct filesystem APIs. After the tool "
    "result shows exactly TOOL_SMOKE_OK, reply with exactly TOOL_SMOKE_OK and "
    "nothing else. Do not use network access, web search, skills, MCP, "
    "sub-agents, task trackers, or any other files or tools."
)
SOFT_LIMIT_USD = 1.75
PRESET_SOFT_LIMIT_USD = 0.75
MAX_OUTPUT_TOKENS = 256
PRESET_MAX_OUTPUT_TOKENS = 1024
MAX_AGENT_TURNS = 4
CELL_TIMEOUT_SECONDS = 300
PRESET_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")


# Claude Opus 5 already passed the strict Anthropic preset tool-loop campaign.
# This follow-up exercises only the four still-unverified target models so that
# the five-model conclusion can combine the old accepted Claude cell with four
# new cells without paying to repeat it.
PRESET_MODELS: dict[str, dict[str, Any]] = {
    logical_model: base.MODELS[logical_model]
    for logical_model in ("gpt-6-astra", "glm-5.3", "kimi-k3")
}
PRESET_MODELS["deepseek-v4-pro"] = {
    "openrouter_model": "deepseek/deepseek-v4-pro-0813",
    "anthropic_base_url": "https://openrouter.ai/api",
    "openrouter_route": {
        "only": ["deepseek"],
        "allow_fallbacks": False,
        "require_parameters": True,
    },
    "endpoint": {
        "tag": "deepseek",
        "provider_name": "DeepSeek",
        "quantization": "unknown",
        "context_length": 1_048_576,
        "max_completion_tokens": 384_000,
        "prompt_usd_per_token": 0.00000066,
        "completion_usd_per_token": 0.00000198,
        "pricing_note": "OpenRouter time-of-day overrides may temporarily double both rates",
    },
}


ORIGINAL_BUILD_MANIFEST = base.build_manifest
ORIGINAL_BUILD_CELLS = base.build_cells
ORIGINAL_HARNESSES = base.HARNESSES
ORIGINAL_MODELS = base.MODELS
ORIGINAL_PARSE_TELEMETRY = base.parse_harness_telemetry
ORIGINAL_ASSISTANT_MARKER_SEEN = base.assistant_marker_seen


def preset_config(logical_model: str) -> dict[str, Any]:
    model = PRESET_MODELS[logical_model]
    return {
        "model": model["openrouter_model"],
        "provider": model["openrouter_route"],
    }


def preset_config_sha256(logical_model: str) -> str:
    return hashlib.sha256(base.canonical_json(preset_config(logical_model))).hexdigest()


def preset_slug(logical_model: str) -> str:
    suffix = preset_config_sha256(logical_model)[:12]
    safe_model = re.sub(r"[^a-z0-9]+", "-", logical_model).strip("-")
    return f"harness-test-cc-target-20260830-r1-{safe_model}-{suffix}"


def build_preset_cells() -> list[dict[str, Any]]:
    cells: list[dict[str, Any]] = []
    for logical_model, model in PRESET_MODELS.items():
        slug = preset_slug(logical_model)
        preset_model = f"@preset/{slug}"
        cells.append(
            {
                "cell_id": f"openrouter-preset--{logical_model}--claude-code",
                "channel": "openrouter",
                "harness": "claude-code",
                "harness_version": "2.1.251",
                "logical_model": logical_model,
                "actual_model": preset_model,
                "resolved_model": model["openrouter_model"],
                "provider": "openrouter",
                "key_env": "OPENROUTER_API_KEY",
                "protocol": "anthropic-messages",
                "translated": False,
                "translation": None,
                "proxy_required": False,
                "compatibility_variant": "server-side-provider-preset",
                "compatibility_drop_params": [],
                "run_only_if_all_prior_failed": [],
                "base_url": "https://openrouter.ai/api",
                "openrouter_route": model["openrouter_route"],
                "route_control": "strict server-side OpenRouter preset",
                "preset_slug": slug,
                "preset_config": preset_config(logical_model),
                "preset_config_sha256": preset_config_sha256(logical_model),
                "preset_must_be_new": True,
                "claude_code_cli_model": preset_model,
                "claude_code_model_overrides": None,
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
        "/pilot/provider_tool_smoke.py",
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
    manifest.update(
        {
            "campaign_id": CAMPAIGN_ID,
            "kind": "paid-terminal-tool-loop-pilot",
            "prepared_at_utc": PREPARED_AT_UTC,
        }
    )
    manifest["scope"].update(
        {
            "benchmark": "none-openrouter-terminal-tool-loop-smoke",
            "task_subset": "one fixed synthetic terminal tool loop per model/harness cell",
            "prompt": PROMPT,
            "acceptance": (
                "successful harness exit, exact final marker, exact workspace artifact, "
                "and post-run trajectory evidence of at least one successful terminal tool call"
            ),
        }
    )
    manifest["runner"]["entry_command"] = (
        "python pilot/provider_tool_smoke.py run --approved-manifest-sha256 <sha256>"
    )
    manifest["runner"]["cell_timeout_seconds"] = CELL_TIMEOUT_SECONDS
    manifest["runner"]["harness_install_and_entry"]["claude-code"]["entry"] = (
        "claude --print --output-format=stream-json --max-turns 4"
    )
    manifest["runner"]["harness_install_and_entry"]["openhands"]["entry"] = (
        "OpenHands SDK Conversation.run(max_iteration_per_run=4)"
    )
    manifest["controls"].update(
        {
            "reasoning_effort": (
                "low wherever exposed; pilot-only cost control, not the formal benchmark default"
            ),
            "reasoning_summary": "none for Codex; other harness defaults",
            "thinking": "low where exposed; provider may keep reasoning enabled",
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "output_limit_location": (
                "256 at PI/Claude Code/OpenHands/DSH request configuration; "
                "Codex has no hard CLI output cap on this custom Responses path"
            ),
            "max_turns_or_iterations": (
                "Claude Code and OpenHands capped at 4; PI, Codex, and DSH bounded by "
                "300-second cell timeout; DSH also makes its default title request"
            ),
            "timeout_seconds": CELL_TIMEOUT_SECONDS,
            "context_window": (
                "endpoint snapshot retained from the approved connectivity pilot; "
                "harness unknown-model fallback metadata is unchanged"
            ),
            "compaction": "harness default; not expected in one two-round tool loop",
            "tools": (
                "installed harness defaults remain; the fixed pilot prompt requests exactly "
                "one existing terminal/shell tool call and forbids all other tools"
            ),
            "skills": "no project skills; built-in default catalogs remain available but are forbidden by the prompt",
            "mcp": [],
            "subagents": "harness defaults remain available but are forbidden by the prompt",
            "memory": "empty isolated HOME",
            "custom_prompt_files": [],
            "benchmark_specific_augmentation": False,
        }
    )
    manifest["budget"].update(
        {
            "soft_limit_usd_per_key": {"OPENROUTER_API_KEY": SOFT_LIMIT_USD},
            "expected_incremental_total_usd": (
                "approximately 0.75-1.35 total; OPENROUTER_API_KEY soft ceiling 1.75; "
                "actual cost depends on cache behavior, reasoning, Claude Code auto-routing, "
                "and DSH title calls"
            ),
        }
    )
    manifest["accounting"]["preserve_sources"].append(
        "OpenRouter current-key usage immediately before and after the campaign"
    )
    manifest["comparison"].update(
        {
            "outcomes": [
                "terminal tool-call serialization",
                "sandbox command execution and exact file artifact",
                "tool-result return to the model",
                "second-round reasoning and exact final marker",
                "actual model/provider identity",
                "hidden retries, auxiliary calls, source-qualified tokens, cost, and wall time",
            ],
            "scope_note": (
                "full 4 model x 5 harness OpenRouter terminal-tool-loop matrix; DSH remains "
                "headless connectivity semantics and Claude Code remains automatic routing"
            ),
            "quality_claim": (
                "none; this validates one terminal function loop only, not freeform editing, "
                "namespace/MCP tools, benchmark quality, or official-API equivalence"
            ),
        }
    )
    manifest["source_sha256"]["pilot/provider_tool_smoke.py"] = base.sha256_file(
        Path(__file__)
    )
    manifest_hash = hashlib.sha256(base.canonical_json(manifest)).hexdigest()
    manifest["manifest_sha256"] = manifest_hash
    return manifest


def build_preset_manifest() -> dict[str, Any]:
    manifest = ORIGINAL_BUILD_MANIFEST()
    manifest.pop("manifest_sha256", None)
    manifest.update(
        {
            "campaign_id": PRESET_CAMPAIGN_ID,
            "kind": "paid-claude-code-openrouter-target-model-pinning-pilot",
            "prepared_at_utc": PRESET_PREPARED_AT_UTC,
        }
    )
    manifest["scope"].update(
        {
            "benchmark": "none-openrouter-preset-terminal-tool-loop-smoke",
            "task_subset": (
                "one fixed synthetic terminal tool loop for each still-unverified formal "
                "target model: GPT-5.6 Sol, GLM-5.3, Kimi K3, and DeepSeek V4 Pro"
            ),
            "prompt": PROMPT,
            "acceptance": (
                "new preset created with exact frozen config; expected provider and "
                "quantization selected; successful Claude Code exit; exact terminal command, "
                "tool result, workspace artifact, and second-round final marker"
            ),
        }
    )
    manifest["runner"].update(
        {
            "entry_command": (
                "python pilot/provider_tool_smoke.py preset-run "
                "--approved-manifest-sha256 <sha256>"
            ),
            "execution": (
                "Claude Code 2.1.251 in the existing pinned pilot image; "
                "no benchmark runner"
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
            "reasoning_summary": "Claude Code/provider default",
            "thinking": (
                "Claude Code --effort high; OpenRouter endpoint metadata declares "
                "reasoning_effort support for all four selected first-party endpoints"
            ),
            "max_output_tokens": PRESET_MAX_OUTPUT_TOKENS,
            "output_limit_location": "CLAUDE_CODE_MAX_OUTPUT_TOKENS=1024",
            "max_turns_or_iterations": "Claude Code --max-turns 4",
            "harness_budget_management": (
                "Claude Code --max-budget-usd 0.25 per cell; pilot-only safety cap, "
                "not the formal benchmark default, and provider billing remains authoritative"
            ),
            "permission_mode": (
                "--permission-mode=bypassPermissions inside the isolated pilot container"
            ),
            "timeout_seconds": CELL_TIMEOUT_SECONDS,
            "retries": {
                "claude_code": (
                    "upstream default retained; no exposed retry-disable control; local "
                    "failure capture reported max_retries=10 with exponential backoff; "
                    "actual attempts will be audited from trajectory and provider records"
                )
            },
            "context_window": (
                "no CLAUDE_CODE_MAX_CONTEXT_TOKENS override; retain Claude Code 2.1.251 "
                "unknown-model metadata/default compaction behavior and record the runtime "
                "contextWindow instead of claiming that provider-advertised 1M changes it"
            ),
            "compaction": (
                "Claude Code default auto-compaction behavior for an unknown model; "
                "no custom compaction prompt and no compaction expected in this short smoke"
            ),
            "tools": (
                "Claude Code installed defaults remain; the fixed pilot prompt requests "
                "exactly one Bash/terminal call and forbids every other tool"
            ),
            "skills": "no project skills; empty isolated HOME and workspace",
            "mcp": [],
            "subagents": (
                "Claude Code defaults remain available but are forbidden by the prompt; "
                "all default model roles resolve to the same underlying cell preset"
            ),
            "memory": "empty isolated HOME",
            "custom_prompt_files": [],
            "benchmark_specific_augmentation": False,
            "compatibility_only_changes": [
                "OpenRouter server-side preset injects only the frozen model and provider route into Anthropic Messages requests",
                "requests use OpenRouter's documented bare @preset/<slug> model reference because combined <model>@preset/<slug> previously returned model_not_found for non-Claude models",
                "all four selected models and bare preset references use Claude Code's unknown-model path; no model is disguised as a recognized Claude family",
                "the preset persists only the original model ID and strict provider policy, leaving tools, system prompt, reasoning, temperature, and messages request-controlled",
                "Claude Code context metadata is not overridden; runtime telemetry is retained as the actual harness behavior",
                "X-OpenRouter-Metadata: enabled is retained for route auditability",
                "no protocol bridge, model catalog, tool, prompt, skill, MCP, memory, or benchmark-specific augmentation is added",
            ],
        }
    )
    manifest["budget"].update(
        {
            "soft_limit_usd_per_key": {
                "OPENROUTER_API_KEY": PRESET_SOFT_LIMIT_USD
            },
            "expected_incremental_total_usd": (
                "approximately 0.15-0.45 total for four two-request tool loops; "
                "OPENROUTER_API_KEY soft ceiling 0.75; preset CRUD and endpoint metadata "
                "calls are expected to have zero inference cost"
            ),
        }
    )
    manifest["accounting"]["preserve_sources"] = [
        "OpenRouter response metadata and per-generation API records",
        "OpenRouter current-key usage immediately before and after the campaign",
        "Claude Code native usage/cost and stream-json trajectory",
        "OpenRouter preset creation and post-call retrieval responses",
    ]
    manifest["comparison"].update(
        {
            "matched_models": list(PRESET_MODELS),
            "matched_harnesses": ["claude-code"],
            "outcomes": [
                "Anthropic Messages bare @preset resolution",
                "strict provider and quantization selection",
                "terminal tool-call serialization and execution",
                "tool-result return and second-round exact final marker",
                "actual model/provider identity, retries, tokens, cost, and wall time",
            ],
            "scope_note": (
                "four new Claude Code cells; combine with the accepted prior Claude Opus 5 "
                "preset cell to cover all five formal target models"
            ),
            "quality_claim": (
                "none; one terminal function loop does not establish benchmark quality, "
                "freeform/MCP compatibility, or official-API equivalence"
            ),
        }
    )
    manifest["routing"].update(
        {
            "strict_cells": (
                "all four cells use an immutable per-model preset containing only model "
                "plus provider.only, allow_fallbacks=false, require_parameters=true, "
                "and quantization where applicable"
            ),
            "claude_code_exception": None,
            "preset_reference": "bare documented reference @preset/<unique-slug>",
        }
    )
    manifest["claude_code_request_parity_preflight"] = {
        "environment": (
            "Claude Code 2.1.251 in the pinned image with networking disabled and a "
            "local Anthropic Messages capture server"
        ),
        "representative_unknown_model_path": (
            "a direct GPT-5.6 Sol slug and a bare preset reference produced identical "
            "request bodies after normalizing only model identity and dynamic metadata "
            "user ID; normalized SHA256 "
            "f303aadae81b466a3acc1b08430784f21b1ad1a8b4efc0748858dfe8bc06602c"
        ),
        "classification": (
            "both references emitted Claude Code's unrecognized_model marker and exposed "
            "the same 25 request-level default tools; all four paid cells intentionally retain this "
            "same unknown-model harness path"
        ),
        "common_equal_fields": [
            "top-level request keys",
            "thinking",
            "context_management",
            "max_tokens",
            "messages",
            "tool names and schemas",
            "anthropic-beta headers",
        ],
    }
    manifest["preset_management"] = {
        "create_endpoint": "POST https://openrouter.ai/api/v1/presets/{slug}/messages",
        "existence_check": "GET the unique slug; abort on any pre-existing preset",
        "payload_fields": ["model", "provider", "messages"],
        "transient_messages": "one inert user placeholder required by the API and ignored for preset storage",
        "forbidden_persisted_fields": [
            "system",
            "tools",
            "temperature",
            "top_p",
            "reasoning",
        ],
        "post_create_checks": (
            "version=1, exact model/provider config, null system prompt, designated "
            "version ID recorded, and unchanged config re-read after the model call"
        ),
        "lifecycle": (
            "unique slug is never updated or deleted; OpenRouter always resolves the "
            "designated version, so the run aborts if the stored ID/config drifts"
        ),
        "slugs": {
            logical_model: {
                "slug": preset_slug(logical_model),
                "config_sha256": preset_config_sha256(logical_model),
            }
            for logical_model in PRESET_MODELS
        },
    }
    manifest["source_sha256"]["pilot/provider_tool_smoke.py"] = base.sha256_file(
        Path(__file__)
    )
    manifest_hash = hashlib.sha256(base.canonical_json(manifest)).hexdigest()
    manifest["manifest_sha256"] = manifest_hash
    return manifest


def parse_harness_telemetry(harness: str, cell_dir: Path) -> dict[str, Any]:
    telemetry = ORIGINAL_PARSE_TELEMETRY(harness, cell_dir)
    artifact = cell_dir / "workspace" / "bridge-check.txt"
    telemetry["tool_smoke_artifact"] = {
        "path": "workspace/bridge-check.txt",
        "exists": artifact.exists(),
        "exact_content": artifact.exists()
        and artifact.read_text(errors="replace") == MARKER,
    }
    return telemetry


def assistant_marker_and_artifact_seen(
    harness: str, cell_dir: Path, telemetry: dict[str, Any]
) -> bool:
    return ORIGINAL_ASSISTANT_MARKER_SEEN(harness, cell_dir, telemetry) and bool(
        telemetry.get("tool_smoke_artifact", {}).get("exact_content")
    )


def validate_preset_spec(spec: dict[str, Any]) -> None:
    logical_model = spec.get("logical_model")
    if logical_model not in PRESET_MODELS:
        raise RuntimeError(f"unknown preset logical model: {logical_model!r}")
    slug = spec.get("preset_slug")
    if not isinstance(slug, str) or not PRESET_SLUG_PATTERN.fullmatch(slug):
        raise RuntimeError("invalid OpenRouter preset slug")
    preset_model = f"@preset/{slug}"
    expected = {
        "harness": "claude-code",
        "protocol": "anthropic-messages",
        "actual_model": preset_model,
        "resolved_model": PRESET_MODELS[logical_model]["openrouter_model"],
        "preset_config": preset_config(logical_model),
        "preset_config_sha256": preset_config_sha256(logical_model),
        "claude_code_cli_model": preset_model,
        "claude_code_model_overrides": None,
        "reasoning_effort": "high",
    }
    for field, value in expected.items():
        if spec.get(field) != value:
            raise RuntimeError(f"preset spec mismatch for {field}")
    if slug != preset_slug(logical_model) or spec.get("preset_must_be_new") is not True:
        raise RuntimeError("preset immutability guard mismatch")


def openrouter_json(
    key: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {key}"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = base.canonical_json(payload)
    request = urllib.request.Request(
        f"https://openrouter.ai/api/v1/{path.lstrip('/')}",
        data=data,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.loads(response.read())
    if not isinstance(result, dict):
        raise RuntimeError("OpenRouter returned a non-object JSON response")
    return result


def get_preset_if_present(key: str, slug: str) -> dict[str, Any] | None:
    quoted = urllib.parse.quote(slug, safe="")
    try:
        return openrouter_json(key, "GET", f"presets/{quoted}")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        detail = error.read().decode(errors="replace")[-1000:]
        raise RuntimeError(
            f"OpenRouter preset lookup failed with HTTP {error.code}: {detail}"
        ) from error


def verify_endpoint_snapshot(spec: dict[str, Any], key: str) -> dict[str, Any]:
    model_id = spec["resolved_model"]
    author, slug = model_id.split("/", 1)
    payload = openrouter_json(
        key,
        "GET",
        "models/"
        + urllib.parse.quote(author, safe="")
        + "/"
        + urllib.parse.quote(slug, safe="")
        + "/endpoints",
    )
    endpoints = payload.get("data", {}).get("endpoints", [])
    expected_tag = PRESET_MODELS[spec["logical_model"]]["endpoint"]["tag"]
    matching = [item for item in endpoints if item.get("tag") == expected_tag]
    if len(matching) != 1:
        raise RuntimeError(
            f"expected exactly one active OpenRouter endpoint tagged {expected_tag!r}"
        )
    endpoint = matching[0]
    expected_quantization = PRESET_MODELS[spec["logical_model"]]["endpoint"][
        "quantization"
    ]
    if endpoint.get("quantization") != expected_quantization:
        raise RuntimeError("OpenRouter endpoint quantization changed since manifest freeze")
    if endpoint.get("status") not in (0, None):
        raise RuntimeError("frozen OpenRouter endpoint is not active")
    return {
        "checked_model": model_id,
        "expected_tag": expected_tag,
        "selected_endpoint": {
            field: endpoint.get(field)
            for field in (
                "name",
                "provider_name",
                "tag",
                "quantization",
                "context_length",
                "max_completion_tokens",
                "supported_parameters",
                "status",
            )
        },
    }


def validate_persisted_preset(
    spec: dict[str, Any], response: dict[str, Any], *, expected_version_id: str | None = None
) -> dict[str, Any]:
    data = response.get("data")
    if not isinstance(data, dict) or data.get("slug") != spec["preset_slug"]:
        raise RuntimeError("OpenRouter preset response has the wrong slug")
    designated = data.get("designated_version")
    if not isinstance(designated, dict):
        raise RuntimeError("OpenRouter preset response omitted designated_version")
    version_id = data.get("designated_version_id")
    if not isinstance(version_id, str) or designated.get("id") != version_id:
        raise RuntimeError("OpenRouter preset designated version ID is inconsistent")
    if expected_version_id is not None and version_id != expected_version_id:
        raise RuntimeError("OpenRouter preset designated version drifted during the run")
    if designated.get("version") != 1:
        raise RuntimeError("OpenRouter preset is not a newly created version 1")
    if designated.get("config") != spec["preset_config"]:
        raise RuntimeError("OpenRouter persisted preset config differs from the manifest")
    if designated.get("system_prompt") not in (None, ""):
        raise RuntimeError("OpenRouter preset unexpectedly persisted a system prompt")
    actual_hash = hashlib.sha256(
        base.canonical_json(designated["config"])
    ).hexdigest()
    if actual_hash != spec["preset_config_sha256"]:
        raise RuntimeError("OpenRouter persisted preset config hash mismatch")
    return {
        "slug": spec["preset_slug"],
        "preset_id": data.get("id"),
        "designated_version_id": version_id,
        "version": designated.get("version"),
        "config": designated.get("config"),
        "config_sha256": actual_hash,
        "system_prompt": designated.get("system_prompt"),
    }


def create_frozen_preset(spec: dict[str, Any], key: str) -> dict[str, Any]:
    slug = spec["preset_slug"]
    if get_preset_if_present(key, slug) is not None:
        raise RuntimeError(
            f"refusing to update pre-existing OpenRouter preset {slug!r}"
        )
    payload = {
        **spec["preset_config"],
        "messages": [
            {
                "role": "user",
                "content": "preset-configuration-placeholder-ignored-by-openrouter",
            }
        ],
    }
    quoted = urllib.parse.quote(slug, safe="")
    try:
        return openrouter_json(key, "POST", f"presets/{quoted}/messages", payload)
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[-1000:]
        raise RuntimeError(
            f"OpenRouter preset creation failed with HTTP {error.code}: {detail}"
        ) from error


def run_preset_cell(
    spec: dict[str, Any], spec_path: Path, *, validate: bool
) -> int:
    validate_preset_spec(spec)
    if validate:
        return base.inside_cell(spec_path, validate=True)
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is missing inside the preset cell")
    trial = Path("/trial")
    endpoint_check = verify_endpoint_snapshot(spec, key)
    base.write_json(trial / "preset-endpoint-check.json", endpoint_check, mode=0o444)
    created_response = create_frozen_preset(spec, key)
    base.write_json(
        trial / "preset-create-response.json", created_response, mode=0o444
    )
    lock = validate_persisted_preset(spec, created_response)
    base.write_json(trial / "preset-lock.json", lock, mode=0o444)
    return_code = base.inside_cell(spec_path, validate=False)
    current_response = get_preset_if_present(key, spec["preset_slug"])
    if current_response is None:
        raise RuntimeError("OpenRouter preset disappeared during the model call")
    base.write_json(
        trial / "preset-post-response.json", current_response, mode=0o444
    )
    post_lock = validate_persisted_preset(
        spec,
        current_response,
        expected_version_id=lock["designated_version_id"],
    )
    base.write_json(trial / "preset-post-lock.json", post_lock, mode=0o444)
    return return_code


def current_key_usage() -> dict[str, Any]:
    env_file = base.ROOT / ".env"
    key = base.load_env_value(env_file, "OPENROUTER_API_KEY")
    if not key:
        return {"available": False}
    request = urllib.request.Request(
        "https://openrouter.ai/api/v1/auth/key",
        headers={"Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.loads(response.read()).get("data", {})
    return {
        "available": True,
        "usage": data.get("usage"),
        "usage_daily": data.get("usage_daily"),
        "usage_weekly": data.get("usage_weekly"),
        "usage_monthly": data.get("usage_monthly"),
        "limit": data.get("limit"),
        "limit_remaining": data.get("limit_remaining"),
        "is_free_tier": data.get("is_free_tier"),
    }


def run_approved(approved_manifest_sha256: str) -> int:
    before = current_key_usage()
    return_code = base.host_run(approved_manifest_sha256)
    after = current_key_usage()
    if base.RUN_DIR.exists():
        payload = {"source": "openrouter-current-key", "before": before, "after": after}
        if isinstance(before.get("usage"), (int, float)) and isinstance(
            after.get("usage"), (int, float)
        ):
            payload["campaign_usage_delta_usd"] = after["usage"] - before["usage"]
        base.write_json(base.RUN_DIR / "openrouter-key-usage.json", payload, mode=0o444)
    return return_code


def configure_base(*, preset_mode: bool) -> None:
    campaign_id = PRESET_CAMPAIGN_ID if preset_mode else CAMPAIGN_ID
    prepared_at = PRESET_PREPARED_AT_UTC if preset_mode else PREPARED_AT_UTC
    soft_limit = PRESET_SOFT_LIMIT_USD if preset_mode else SOFT_LIMIT_USD
    base.CAMPAIGN_ID = campaign_id
    base.MANIFEST_PREPARED_AT_UTC = prepared_at
    base.RUN_DIR = base.ROOT / "runs" / campaign_id
    base.REPORT_PATH = base.RUN_DIR / "report.md"
    base.MARKER = MARKER
    base.PROMPT = PROMPT
    base.HARNESSES = ("claude-code",) if preset_mode else ORIGINAL_HARNESSES
    base.MODELS = PRESET_MODELS if preset_mode else ORIGINAL_MODELS
    base.SOFT_LIMITS_USD = {"OPENROUTER_API_KEY": soft_limit}
    base.MAX_OUTPUT_TOKENS = (
        PRESET_MAX_OUTPUT_TOKENS if preset_mode else MAX_OUTPUT_TOKENS
    )
    base.MAX_AGENT_TURNS = MAX_AGENT_TURNS
    base.CELL_TIMEOUT_SECONDS = CELL_TIMEOUT_SECONDS
    base.docker_command = docker_command
    base.build_cells = build_preset_cells if preset_mode else ORIGINAL_BUILD_CELLS
    base.build_manifest = build_preset_manifest if preset_mode else build_manifest
    base.parse_harness_telemetry = parse_harness_telemetry
    base.assistant_marker_seen = assistant_marker_and_artifact_seen


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("preflight", help="zero-cost disposable config validation")
    run = subparsers.add_parser("run", help="freeze and launch the approved pilot")
    run.add_argument("--approved-manifest-sha256", required=True)
    subparsers.add_parser(
        "preset-preflight",
        help="zero-cost Claude Code OpenRouter preset config validation",
    )
    preset_run = subparsers.add_parser(
        "preset-run", help="freeze and launch the approved Claude Code preset pilot"
    )
    preset_run.add_argument("--approved-manifest-sha256", required=True)
    cell = subparsers.add_parser("_cell", help=argparse.SUPPRESS)
    cell.add_argument("--spec", type=Path, required=True)
    cell.add_argument("--validate", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    preset_mode = args.command in {"preset-preflight", "preset-run"}
    if args.command == "_cell":
        spec = json.loads(args.spec.read_text())
        preset_mode = "preset_slug" in spec
    configure_base(preset_mode=preset_mode)
    if args.command in {"preflight", "preset-preflight"}:
        return base.host_preflight()
    if args.command in {"run", "preset-run"}:
        return run_approved(args.approved_manifest_sha256)
    if args.command == "_cell":
        artifact = Path("/trial/workspace/bridge-check.txt")
        if not args.validate and artifact.exists():
            raise RuntimeError("tool-smoke artifact unexpectedly pre-exists")
        if preset_mode:
            return run_preset_cell(spec, args.spec, validate=args.validate)
        return base.inside_cell(args.spec, validate=args.validate)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
