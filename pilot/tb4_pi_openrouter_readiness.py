#!/usr/bin/env python3
"""One-task TB4 readiness pilot for PI and four strict OpenRouter routes.

GLM is intentionally absent: its exact PI path is already benchmark-ready.
Each remaining provider gets one independent Harbor job. Jobs run in parallel
across providers, while each provider/model lane has concurrency one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import provider_smoke as smoke
import tb4_claude_code_glm as common
import tb4_pi_glm as legacy


ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN_ID = "pilot-tb4-pi-openrouter-readiness-20260831-r1"
PREPARED_AT_UTC = "2026-08-31T15:30:00Z"
RUN_DIR = ROOT / "runs" / CAMPAIGN_ID
HARBOR_JOBS_DIR = RUN_DIR / "harbor"
TASKS = ("bun-sourcemap-leak",)
HARBOR_VERSION = "0.22.0"
PI_VERSION = "0.84.4"
PI_REMOTE_DIR = "/tmp/harbor-pi-agent"
CAMPAIGN_CONCURRENCY = 4
PER_PROVIDER_CONCURRENCY = 1
WHOLE_RUN_TIMEOUT_SECONDS = 9 * 60 * 60


MODEL_SPECS: dict[str, dict[str, Any]] = {
    "claude-opus-5": {
        "model": "anthropic/claude-opus-5",
        "expected_model": "anthropic/claude-opus-5-20260723",
        "provider_name": "Anthropic",
        "route": {
            "only": ["anthropic"],
            "allow_fallbacks": False,
            "require_parameters": True,
        },
        "quantization": "unknown",
        "pi_context_window": 1_000_000,
        "pi_max_tokens": 128_000,
        "endpoint_max_tokens": 128_000,
        "list_context": "1M",
        "list_max_out": "128K",
        "input": ["text", "image"],
    },
    "gpt-6-astra": {
        "model": "openai/gpt-6-astra",
        "expected_model": "openai/gpt-6-astra-20260903",
        "provider_name": "OpenAI",
        "route": {
            "only": ["openai"],
            "allow_fallbacks": False,
            "require_parameters": True,
        },
        "quantization": "unknown",
        "pi_context_window": 1_050_000,
        "pi_max_tokens": 128_000,
        "endpoint_max_tokens": 128_000,
        "list_context": "1.1M",
        "list_max_out": "128K",
        "input": ["text", "image"],
    },
    "kimi-k3": {
        "model": "moonshotai/kimi-k3",
        "expected_model": "moonshotai/kimi-k3-20260715",
        "provider_name": "Moonshot AI",
        "route": {
            "only": ["moonshotai/mxfp4"],
            "quantizations": ["mxfp4"],
            "allow_fallbacks": False,
            "require_parameters": True,
        },
        "quantization": "mxfp4",
        "pi_context_window": 1_048_576,
        # PI 0.84.4's bundled non-batch model entry is 131,072 even though
        # the pinned endpoint advertises 943,718. Preserve the harness-native
        # entry here; do not silently upgrade it to endpoint capacity.
        "pi_max_tokens": 131_072,
        "endpoint_max_tokens": 943_718,
        "list_context": "1.0M",
        "list_max_out": "131.1K",
        "input": ["text", "image"],
    },
    "deepseek-v4-pro": {
        "model": "deepseek/deepseek-v4-pro-0813",
        "expected_model": "deepseek/deepseek-v4-pro-20260813",
        "provider_name": "DeepSeek",
        "route": {
            "only": ["deepseek"],
            "allow_fallbacks": False,
            "require_parameters": True,
        },
        "quantization": "unknown",
        "pi_context_window": 1_048_576,
        "pi_max_tokens": 384_000,
        "endpoint_max_tokens": 384_000,
        "list_context": "1.0M",
        "list_max_out": "384K",
        "input": ["text"],
    },
}


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    path.chmod(mode)


def job_name(logical_model: str, *, install_only: bool = False) -> str:
    suffix = "--install-only" if install_only else ""
    return f"terminal-bench-4--pi--{logical_model}--rep-1{suffix}"


def pi_models_json(logical_model: str) -> dict[str, Any]:
    spec = MODEL_SPECS[logical_model]
    return {
        "providers": {
            "openrouter": {
                "modelOverrides": {
                    spec["model"]: {
                        "compat": {
                            # PI otherwise uses max_completion_tokens for its
                            # generic OpenRouter path. The pinned endpoints
                            # advertise max_tokens. This changes only the field
                            # name, never the bundled numeric model budget.
                            "maxTokensField": "max_tokens",
                            "openRouterRouting": spec["route"],
                        }
                    }
                }
            }
        }
    }


def compose_overlay(models_path: Path) -> dict[str, Any]:
    return {
        "services": {
            "main": {
                "tmpfs": [f"{PI_REMOTE_DIR}:rw,mode=1777"],
                "volumes": [
                    f"{models_path.resolve()}:{PI_REMOTE_DIR}/models.json:ro"
                ],
            }
        }
    }


def harbor_config(
    logical_model: str,
    *,
    overlay_path: Path,
    install_only: bool = False,
    jobs_dir: Path | None = None,
) -> dict[str, Any]:
    spec = MODEL_SPECS[logical_model]
    return {
        "job_name": job_name(logical_model, install_only=install_only),
        "jobs_dir": str(jobs_dir or HARBOR_JOBS_DIR),
        "n_attempts": 1,
        "install_only": install_only,
        "timeout_multiplier": 1.0,
        "n_concurrent_trials": 1,
        "retry": {"max_retries": 0},
        "environment": {
            "type": "docker",
            "force_build": False,
            "delete": True,
            "cpu_enforcement_policy": "auto",
            "memory_enforcement_policy": "auto",
            "extra_docker_compose": [str(overlay_path)],
        },
        "verifier": {},
        "agents": [
            {
                "name": "pi",
                "model_name": f"openrouter/{spec['model']}",
                "n_concurrent": 1,
                "skills": [],
                "resume_trajectory": False,
                "include_logs": ["**/*"],
                "kwargs": {"version": PI_VERSION, "thinking": "high"},
                "env": {
                    "OPENROUTER_API_KEY": "${OPENROUTER_API_KEY}",
                    "PI_CODING_AGENT_DIR": PI_REMOTE_DIR,
                    "PI_OFFLINE": "1",
                    "PI_SKIP_VERSION_CHECK": "1",
                    "PI_TELEMETRY": "0",
                },
                "mcp_servers": [],
            }
        ],
        "datasets": [
            {
                "path": str(ROOT / "vendor" / "terminal-bench"),
                "task_names": list(TASKS),
            }
        ],
        "tasks": [],
        "artifacts": [],
        "extra_instruction_paths": [],
        "extra_instructions": [],
    }


def manifest_model_path(logical_model: str) -> dict[str, Any]:
    spec = MODEL_SPECS[logical_model]
    return {
        "requested_model_id": spec["model"],
        "expected_actual_model": spec["expected_model"],
        "provider": f"OpenRouter pinned to {spec['provider_name']}",
        "base_url": "https://openrouter.ai/api/v1",
        "protocol": "OpenAI Chat Completions",
        "classification": "PI-native OpenRouter provider; no protocol translation",
        "route": spec["route"],
        "quantization": spec["quantization"],
        "model_advertised_context": spec["pi_context_window"],
        "model_advertised_max_output": spec["endpoint_max_tokens"],
        "pi_bundled_context_window": spec["pi_context_window"],
        "pi_bundled_max_output": spec["pi_max_tokens"],
        "wire_output_field_expected": {
            "name": "max_tokens",
            "value": spec["pi_max_tokens"],
            "qualification": "must be confirmed from the paid request capture",
        },
        "input_modalities_from_pi_catalog": spec["input"],
        "compatibility_configuration": (
            "read-only PI models.json adds strict openRouterRouting and the "
            "name-only maxTokensField=max_tokens mapping; it does not override "
            "context, numeric output budget, prompt, tools, reasoning, retry, or loop"
        ),
    }


def build_manifest() -> dict[str, Any]:
    resolved_configs = {
        name: harbor_config(
            name,
            overlay_path=RUN_DIR / "configs" / f"{name}.compose.json",
        )
        for name in MODEL_SPECS
    }
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "campaign_id": CAMPAIGN_ID,
        "kind": "paid-real-benchmark-qualification-pilot-not-for-primary-scores",
        "prepared_at_utc": PREPARED_AT_UTC,
        "approval": {
            "required_before_launch": True,
            "target": "campaign ID plus exact manifest SHA256",
        },
        "scope": {
            "benchmark": "Terminal-Bench 4.0",
            "dataset_tag": "v4.0.0",
            "dataset_commit": common.TB4_COMMIT,
            "tasks": [common.load_task_metadata(name) for name in TASKS],
            "task_modality": "text-only software task; no task image attachment",
            "replicate": 1,
            "planned_trials": len(MODEL_SPECS),
            "n_attempts": 1,
            "concurrency": {
                "global": CAMPAIGN_CONCURRENCY,
                "per_provider_model_lane": PER_PROVIDER_CONCURRENCY,
                "policy": (
                    "four different first-party provider lanes run in parallel; "
                    "one Harbor job and one trial per provider"
                ),
            },
        },
        "runner": {
            "name": "Harbor",
            "version": HARBOR_VERSION,
            "commit": "4407eb5",
            "entry": (
                "uvx --offline --from harbor==0.22.0 harbor run "
                "--config <frozen-per-model-config> --yes"
            ),
            "integration": "Harbor built-in pi agent; no custom Harbor adapter",
            "resolved_job_configs": resolved_configs,
            "outer_trial_retries": 0,
        },
        "repository": {
            "git_head": "ba4fb2e28a3705d0ae0a07ae67cf50f6fa273e3a",
            "working_tree": (
                "dirty shared workspace; immutable manifest and source hashes below "
                "are authoritative for the execution path"
            ),
        },
        "harness": {
            "name": "PI",
            "package": "@earendil-works/pi-coding-agent",
            "version": PI_VERSION,
            "source": "npm exact version",
            "entry": (
                "pi --print --mode json --session-dir /logs/agent/pi/sessions "
                "--provider openrouter --model <exact-id> --thinking high"
            ),
            "launch_behavior": "Harbor built-in; upstream PI defaults retained",
        },
        "model_paths": {
            name: manifest_model_path(name) for name in MODEL_SPECS
        },
        "controls": {
            "reasoning_effort": (
                "study target high via Harbor kwargs.thinking=high -> PI --thinking high; "
                "paid request must show OpenRouter reasoning.effort=high"
            ),
            "thinking_token_limit": "unset; PI/model/provider high behavior",
            "context": (
                "PI 0.84.4 bundled per-model values; no common context override; "
                "see model_paths"
            ),
            "output_budget": (
                "PI 0.84.4 bundled per-model values; no numeric override; Kimi is "
                "therefore 131072 in PI despite the endpoint advertising 943718"
            ),
            "compaction": {
                "enabled": True,
                "trigger": "context tokens > active model context window - 16384",
                "reserve_tokens": 16384,
                "keep_recent_tokens": 20000,
                "model": "same active model and thinking level",
            },
            "max_turns": "unset; PI default",
            "budget_management": (
                "no PI dollar cap and no dollar kill guard; structurally bounded to "
                "four fixed one-task trials; approval covers the disclosed exposure"
            ),
            "sampling": {
                "temperature": "omitted/provider default",
                "top_p": "omitted/provider default",
                "seed": None,
            },
            "timeouts": {
                "tb4_agent_seconds": 28800,
                "harbor_agent_override": None,
                "timeout_multiplier": 1.0,
                "pi_openai_sdk_request_seconds": 600,
                "verifier_seconds": 600,
                "host_job_watchdog_seconds": WHOLE_RUN_TIMEOUT_SECONDS,
            },
            "retries": {
                "harbor_outer_trial": 0,
                "provider_sdk": 0,
                "pi_agent": "native auto retry, max 3, exponential 2s/4s/8s",
                "compatibility_layer": 0,
            },
            "tools": "PI default coding tools; no override",
            "skills": [],
            "extensions": [],
            "mcp_servers": [],
            "subagents": "none in the unextended PI harness",
            "memory": "fresh per-trial session; Harbor resume disabled",
            "prompt_files": [],
            "benchmark_specific_augmentation": False,
        },
        "sandbox": {
            "provider": "local Docker via Harbor",
            "per_task_resources_and_image": common.load_task_metadata(TASKS[0]),
            "network_policy": "task-default Docker outbound network",
            "task_timeout_seconds": 28800,
        },
        "accounting": {
            "primary": "OpenRouter settled per-generation records",
            "secondary": ["PI native message usage/cost", "Harbor AgentResult"],
            "litellm": "not present",
            "double_count_rule": "never sum duplicate representations of one call",
            "estimated_provider_cost_usd": (
                "$5-$25 total, highly uncertain and Claude-dominated; another harness "
                "on this task is not a reliable PI cost predictor"
            ),
            "hard_dollar_stop": None,
            "known_uncertainty": (
                "OpenRouter settlement can lag; harness-native estimates can disagree; "
                "the four trials all start together so a campaign soft line cannot "
                "prevent already in-flight spend"
            ),
        },
        "qualification_claim": (
            "each accepted and fully audited cell may advance only its exact PI/model/"
            "provider/TB4 path from T to B; no quality or multimodal claim"
        ),
        "post_run_audit": {
            "required": True,
            "scope": "all four trajectories and every upstream request",
            "checks": [
                "actual model/provider, strict route, fallback and quantization evidence",
                "wire reasoning and exact context/output behavior",
                "tool-call/result closure and thinking continuity",
                "compaction, output cap, retry, 429, timeout and verifier outcome",
                "default tools and absence of skills/MCP/prompt contamination",
                "OpenRouter versus PI versus Harbor token/cost disagreement",
            ],
        },
        "source_sha256": {
            "pilot/tb4_pi_openrouter_readiness.py": sha256_file(Path(__file__)),
            "pilot/tb4_pi_glm.py": sha256_file(ROOT / "pilot" / "tb4_pi_glm.py"),
            "pilot/tb4_claude_code_glm.py": sha256_file(
                ROOT / "pilot" / "tb4_claude_code_glm.py"
            ),
            "pilot/provider_smoke.py": sha256_file(
                ROOT / "pilot" / "provider_smoke.py"
            ),
            "harbor-pi.py": sha256_file(
                Path(
                    "/tmp/harness-test-uv-cache/archive-v0/Hb8_Z16BVisZs96hPm47x/"
                    "harbor/agents/installed/pi.py"
                )
            ),
        },
    }
    manifest["manifest_sha256"] = hashlib.sha256(canonical_json(manifest)).hexdigest()
    return manifest


def static_model_check(
    logical_model: str, models_path: Path
) -> tuple[bool, str]:
    spec = MODEL_SPECS[logical_model]
    completed = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--tmpfs",
            f"{PI_REMOTE_DIR}:rw,mode=1777",
            "-e",
            "OPENROUTER_API_KEY=preflight-dummy",
            "-e",
            f"PI_CODING_AGENT_DIR={PI_REMOTE_DIR}",
            "-e",
            "PI_OFFLINE=1",
            "-v",
            f"{models_path}:{PI_REMOTE_DIR}/models.json:ro",
            smoke.IMAGE,
            "pi",
            "--list-models",
            spec["model"],
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    expected = (spec["model"], spec["list_context"], spec["list_max_out"])
    ok = completed.returncode == 0 and all(value in completed.stdout for value in expected)
    return ok, completed.stdout + completed.stderr


def preflight() -> int:
    manifest = build_manifest()
    with tempfile.TemporaryDirectory(prefix="tb4-pi-readiness-", dir="/tmp") as raw:
        temp = Path(raw)
        for logical_model, spec in MODEL_SPECS.items():
            models_path = temp / logical_model / "models.json"
            overlay_path = temp / logical_model / "compose.json"
            config_path = temp / logical_model / "job.json"
            write_json(models_path, pi_models_json(logical_model))
            write_json(overlay_path, compose_overlay(models_path))
            config = harbor_config(
                logical_model,
                overlay_path=overlay_path,
                jobs_dir=temp / "print-config-jobs",
            )
            write_json(config_path, config)
            completed = subprocess.run(
                legacy.harbor_command(config_path, print_config=True),
                cwd=ROOT,
                env=legacy.tool_env("preflight-dummy"),
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if completed.returncode != 0:
                print(completed.stdout[-3000:] + completed.stderr[-3000:], file=sys.stderr)
                return 2
            resolved = json.loads(completed.stdout)
            agent = (resolved.get("agents") or [{}])[0]
            if (
                agent.get("name") != "pi"
                or agent.get("model_name") != f"openrouter/{spec['model']}"
                or agent.get("kwargs", {}).get("version") != PI_VERSION
                or agent.get("kwargs", {}).get("thinking") != "high"
            ):
                raise RuntimeError(
                    f"Harbor resolution drift for {logical_model}: "
                    + json.dumps(
                        {
                            "agent": agent,
                            "retry": resolved.get("retry"),
                            "timeout_multiplier": resolved.get("timeout_multiplier"),
                        },
                        sort_keys=True,
                    )
                )
            if (
                config["retry"]["max_retries"] != 0
                or config["timeout_multiplier"] != 1.0
                or config["n_concurrent_trials"] != 1
            ):
                raise RuntimeError(f"raw Harbor controls drift for {logical_model}")
            compat = (
                pi_models_json(logical_model)["providers"]["openrouter"]
                ["modelOverrides"][spec["model"]]["compat"]
            )
            if (
                compat.get("maxTokensField") != "max_tokens"
                or compat.get("openRouterRouting") != spec["route"]
            ):
                raise RuntimeError(f"PI compatibility drift for {logical_model}")
            ok, output = static_model_check(logical_model, models_path)
            if not ok:
                print(output, file=sys.stderr)
                raise RuntimeError(f"PI bundled model metadata drift for {logical_model}")

        # One install-only lifecycle is sufficient because all four cells use
        # the same exact harness package and task image.
        install_model = "claude-opus-5"
        models_path = temp / install_model / "models.json"
        overlay_path = temp / install_model / "compose.json"
        install_path = temp / "install-only.json"
        write_json(
            install_path,
            harbor_config(
                install_model,
                overlay_path=overlay_path,
                install_only=True,
                jobs_dir=temp / "install-only-jobs",
            ),
        )
        installed = subprocess.run(
            legacy.harbor_command(install_path),
            cwd=ROOT,
            env=legacy.tool_env("preflight-dummy"),
            capture_output=True,
            text=True,
            timeout=1200,
            check=False,
        )
        if installed.returncode != 0:
            print(installed.stdout[-6000:] + installed.stderr[-6000:], file=sys.stderr)
            raise RuntimeError("Harbor PI install-only preflight failed")

    print(
        "Zero-cost preflight passed: four Harbor configs, PI 0.84.4 install, "
        "high reasoning, strict routes, native catalog context/output, outer retry=0."
    )
    print(f"Prospective manifest SHA256: {manifest['manifest_sha256']}")
    print("No model generation was made.")
    return 0


def freeze(approved_sha256: str) -> tuple[dict[str, Path], dict[str, Any]]:
    manifest = build_manifest()
    if approved_sha256 != manifest["manifest_sha256"]:
        raise RuntimeError("approved manifest hash does not match resolved configuration")
    if RUN_DIR.exists():
        raise RuntimeError(f"immutable run directory already exists: {RUN_DIR}")
    RUN_DIR.mkdir(parents=True)
    configs: dict[str, Path] = {}
    for logical_model in MODEL_SPECS:
        models_path = RUN_DIR / "configs" / f"{logical_model}.models.json"
        overlay_path = RUN_DIR / "configs" / f"{logical_model}.compose.json"
        config_path = RUN_DIR / "configs" / f"{logical_model}.job.json"
        write_json(models_path, pi_models_json(logical_model))
        write_json(overlay_path, compose_overlay(models_path))
        write_json(
            config_path,
            harbor_config(logical_model, overlay_path=overlay_path),
        )
        configs[logical_model] = config_path
    write_json(RUN_DIR / "manifest.json", manifest)
    write_json(
        RUN_DIR / "approval.json",
        {
            "campaign_id": CAMPAIGN_ID,
            "approved_manifest_sha256": approved_sha256,
            "scope": "exact resolved four-cell PI readiness campaign",
        },
    )
    return configs, manifest


def run_cell(logical_model: str, config_path: Path, api_key: str) -> int:
    log_path = RUN_DIR / "logs" / f"{logical_model}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w") as log:
        process = subprocess.Popen(
            legacy.harbor_command(config_path),
            cwd=ROOT,
            env=legacy.tool_env(api_key),
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            return process.wait(timeout=WHOLE_RUN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            return 124


def audit_cell(logical_model: str, api_key: str, return_code: int) -> dict[str, Any]:
    spec = MODEL_SPECS[logical_model]
    root = HARBOR_JOBS_DIR / job_name(logical_model)
    trials: list[dict[str, Any]] = []
    for result_path in sorted(root.glob("*/result.json")):
        trial_dir = result_path.parent
        result = json.loads(result_path.read_text())
        generations = smoke.fetch_openrouter_generation_records(trial_dir, api_key)
        records = generations.get("records") or []
        session_paths = sorted(trial_dir.glob("agent/pi/sessions/**/*.jsonl"))
        session_records = [
            item for path in session_paths for item in smoke.parse_json_lines(path)
        ]
        messages = [item.get("message") or {} for item in session_records if item.get("type") == "message"]
        assistant = [item for item in messages if item.get("role") == "assistant"]
        tool_results = [item for item in messages if item.get("role") == "toolResult"]
        compactions = [item for item in session_records if item.get("type") == "compaction"]
        trials.append(
            {
                "trial_name": result.get("trial_name"),
                "task_name": result.get("task_name"),
                "reward": (result.get("verifier_result") or {}).get("rewards"),
                "exception_info": result.get("exception_info"),
                "agent_result": result.get("agent_result"),
                "generation_ids": generations.get("generation_ids_found"),
                "generation_lookup_errors": generations.get("errors"),
                "provider_generation_count": len(records),
                "provider_cost_usd": sum(
                    float(item.get("total_cost") or 0) for item in records
                ),
                "actual_models": sorted(
                    {str(item.get("model")) for item in records if item.get("model")}
                ),
                "actual_providers": sorted(
                    {
                        str(item.get("provider_name"))
                        for item in records
                        if item.get("provider_name")
                    }
                ),
                "expected_model": spec["expected_model"],
                "expected_provider": spec["provider_name"],
                "quantization_evidence": (
                    "strict request route; use provider metadata if it exposes a "
                    "separate effective-quantization field"
                ),
                "pi_session_count": len(session_paths),
                "assistant_messages": len(assistant),
                "reasoning_messages": sum(
                    any(
                        isinstance(block, dict) and block.get("type") == "thinking"
                        for block in (item.get("content") or [])
                    )
                    for item in assistant
                ),
                "tool_calls": sum(
                    sum(
                        isinstance(block, dict) and block.get("type") == "toolCall"
                        for block in (item.get("content") or [])
                    )
                    for item in assistant
                ),
                "tool_results": len(tool_results),
                "compaction_count": len(compactions),
                "pi_retry_events": sum(
                    item.get("type") == "auto_retry_start" for item in session_records
                ),
                "output_cap_messages": sum(
                    item.get("stopReason") in ("length", "max_tokens", "max-tokens")
                    for item in assistant
                ),
            }
        )
    return {
        "logical_model": logical_model,
        "harbor_return_code": return_code,
        "trials": trials,
    }


def run(approved_sha256: str) -> int:
    api_key = legacy.load_key()
    configs, manifest = freeze(approved_sha256)
    before = common.current_key_usage(api_key)
    write_json(RUN_DIR / "openrouter-usage-before.json", before, mode=0o600)
    return_codes: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=CAMPAIGN_CONCURRENCY) as pool:
        futures = {
            pool.submit(run_cell, name, path, api_key): name
            for name, path in configs.items()
        }
        for future in as_completed(futures):
            name = futures[future]
            return_codes[name] = future.result()
    after = common.current_key_usage(api_key)
    write_json(RUN_DIR / "openrouter-usage-after.json", after, mode=0o600)
    write_json(
        RUN_DIR / "audit.json",
        {
            "campaign_id": CAMPAIGN_ID,
            "manifest_sha256": manifest["manifest_sha256"],
            "decision": "pending mandatory full trajectory review; never primary-score data",
            "cells": [
                audit_cell(name, api_key, return_codes.get(name, 125))
                for name in MODEL_SPECS
            ],
        },
    )
    write_json(
        RUN_DIR / "run-status.json",
        {
            "campaign_id": CAMPAIGN_ID,
            "manifest_sha256": manifest["manifest_sha256"],
            "return_codes": return_codes,
            "completed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )
    return 0 if all(code == 0 for code in return_codes.values()) else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight")
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--approved-manifest-sha256", required=True)
    args = parser.parse_args()
    if args.command == "preflight":
        return preflight()
    return run(args.approved_manifest_sha256)


if __name__ == "__main__":
    raise SystemExit(main())
