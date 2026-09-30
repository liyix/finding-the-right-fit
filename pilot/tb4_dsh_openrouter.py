#!/usr/bin/env python3
"""One audited TB4 qualification campaign for DSH and the four remaining models."""

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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import provider_smoke as smoke
import tb4_dsh_glm as legacy


CAMPAIGN_ID = "pilot-tb4-dsh-openrouter-other-models-20260831-r1"
PREPARED_AT_UTC = "2026-08-31T09:45:00Z"
RUN_DIR = ROOT / "runs" / CAMPAIGN_ID
HARBOR_JOBS_DIR = RUN_DIR / "harbor"
TASKS = ("bun-sourcemap-leak",)
HARBOR_VERSION = "0.22.0"
DSH_VERSION = "0.1.1-rc.2"
WHOLE_RUN_TIMEOUT_SECONDS = 9 * 60 * 60
CAMPAIGN_CONCURRENCY = 4


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
        "context_window": 1_000_000,
        "max_tokens": 128_000,
        "input": ["text", "image"],
        "endpoint_modalities": ["text", "image", "file"],
        "quantization": "unknown",
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
        "context_window": 1_050_000,
        "max_tokens": 128_000,
        "input": ["text", "image"],
        "endpoint_modalities": ["file", "image", "text"],
        "quantization": "unknown",
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
        "context_window": 1_048_576,
        "max_tokens": 943_718,
        "input": ["text", "image"],
        "endpoint_modalities": ["text", "image", "video"],
        "quantization": "mxfp4",
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
        "context_window": 1_048_576,
        "max_tokens": 384_000,
        "input": ["text"],
        "endpoint_modalities": ["text"],
        "quantization": "unknown",
        "known_account_constraint": (
            "the current OpenRouter account previously returned a pre-generation "
            "data-policy/guardrail 404 for this first-party endpoint; this cell "
            "rechecks the current state without allowing fallback"
        ),
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
    return f"terminal-bench-4--deepseek-harness--{logical_model}--rep-1{suffix}"


def harbor_config(
    logical_model: str,
    *,
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
        },
        "verifier": {},
        "agents": [
            {
                "name": "integrations.harbor_deepseek:DeepSeekHarness",
                "model_name": f"openrouter/{spec['model']}",
                "n_concurrent": 1,
                "skills": [],
                "resume_trajectory": False,
                "include_logs": ["**/*"],
                "kwargs": {
                    "version": DSH_VERSION,
                    "adapter": "pi-ai",
                    "model_api": "openai-completions",
                    "context_window": spec["context_window"],
                    "max_tokens": spec["max_tokens"],
                    "input_modalities": spec["input"],
                    "reasoning_effort": "high",
                    "compat": {
                        "supportsDeveloperRole": False,
                        "supportsReasoningEffort": True,
                        "maxTokensField": "max_tokens",
                        "thinkingFormat": "openrouter",
                    },
                    "openrouter_route": spec["route"],
                    "permission_mode": "danger-full-access",
                },
                "env": {"OPENROUTER_API_KEY": "${OPENROUTER_API_KEY}"},
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


def build_manifest() -> dict[str, Any]:
    cells: list[dict[str, Any]] = []
    for logical_model, spec in MODEL_SPECS.items():
        cells.append(
            {
                "cell_id": f"dsh-standard--{logical_model}",
                "logical_model": logical_model,
                "requested_model": spec["model"],
                "expected_actual_model": spec["expected_model"],
                "provider": spec["provider_name"],
                "base_url": "https://openrouter.ai/api/v1",
                "protocol": "OpenAI Chat Completions with SSE",
                "path_classification": "provider-compatible; no protocol translation",
                "route": spec["route"],
                "endpoint": {
                    "status": 0,
                    "context_window": spec["context_window"],
                    "max_completion_tokens": spec["max_tokens"],
                    "endpoint_modalities": spec["endpoint_modalities"],
                    "dsh_pi_ai_modalities": spec["input"],
                    "quantization": spec["quantization"],
                    "supported_parameters_checked": [
                        "reasoning",
                        "reasoning_effort",
                        "max_tokens",
                        "tools",
                        "tool_choice",
                    ],
                },
                "known_constraint": spec.get("known_account_constraint"),
                "resolved_job_config": harbor_config(logical_model),
            }
        )
    manifest: dict[str, Any] = {
        "campaign_id": CAMPAIGN_ID,
        "kind": "paid-real-benchmark-qualification-not-for-primary-scores",
        "prepared_at_utc": PREPARED_AT_UTC,
        "scope": {
            "benchmark": "Terminal-Bench 4.0",
            "dataset_tag": "v4.0.0",
            "dataset_commit": legacy.common.TB4_COMMIT,
            "tasks_per_cell": [
                legacy.common.load_task_metadata(name) for name in TASKS
            ],
            "replicate": 1,
            "planned_cells": len(cells),
            "planned_trials": len(cells) * len(TASKS),
            "campaign_concurrency": CAMPAIGN_CONCURRENCY,
            "per_provider_concurrency": 1,
            "n_attempts": 1,
        },
        "runner": {
            "name": "Harbor",
            "version": HARBOR_VERSION,
            "commit": "4407eb5",
            "entry": "four frozen harbor run commands launched concurrently",
            "integration": "custom thin installed-agent adapter",
            "outer_trial_retries": 0,
        },
        "harness": {
            "name": "DeepSeek Harness",
            "version": DSH_VERSION,
            "source": "npm @deepseek-ai/dsh@0.1.1-rc.2",
            "upstream_commit": "b150a551b8d465e31e418e1b2eaf5e79bbb7d28e",
            "entry": "dsh --profile headless --patch <resolved> <task>",
            "profile": "headless one-shot",
            "agent_preset": "upstream shipped standard",
            "tools": "26 upstream standard tools; no additions or deletions",
            "standard_mount_patch_sha256": (
                "b072cb7582dcb1c6baa948f4a292d748eed75f349e878842c50791908a801622"
            ),
        },
        "cells": cells,
        "controls": {
            "reasoning_effort": (
                "high via DSH route reasoning=high and model reasoningEfforts high; "
                "wire shape reasoning.effort=high"
            ),
            "thinking_token_limit": "unset; endpoint high behavior",
            "context_window": "endpoint-specific exact catalog metadata in each cell",
            "output_budget": (
                "endpoint-specific model maximum, explicitly configured by DSH and sent "
                "as max_tokens; no common cap"
            ),
            "compaction": {
                "backend": "upstream standard compaction-basic",
                "auto": True,
                "threshold_ratio": 0.8,
                "retain_ratio": 0.16,
                "summary_max_tokens": 8192,
                "compaction_retries": 1,
                "overflow_retries": 1,
                "summary_model": "same routed model",
            },
            "max_turns": "unset; upstream standard loop",
            "timeouts": {
                "benchmark_agent_seconds": 28_800,
                "harbor_agent_override": None,
                "harbor_timeout_multiplier": 1.0,
                "pi_ai_sdk_request_seconds": 600,
                "dsh_stream_idle_seconds": 300,
                "injector": None,
                "campaign_operator_safety_seconds": WHOLE_RUN_TIMEOUT_SECONDS,
                "tool_and_verifier": "upstream/task native",
            },
            "retries": {
                "harbor_trial": 0,
                "dsh_provider": (
                    "native normal policy, up to 5 retries with 500ms-10s exponential "
                    "backoff and 10% jitter"
                ),
                "injector": 0,
            },
            "temperature": "omitted; provider default",
            "top_p": "omitted; provider default",
            "seed": None,
            "skills": "upstream mechanism present; fresh DSH_HOME has no user/project skills",
            "mcp": [],
            "subagents": "upstream standard tools available; use and effort inheritance audited",
            "memory": "fresh per-trial DSH_HOME; resume disabled",
            "benchmark_specific_augmentation": False,
            "auxiliary_calls": "DSH session-title LLM call retained and billed",
            "modality_note": (
                "pi-ai represents text/image only; endpoint file/video modalities are not "
                "invented as DSH capabilities. The selected TB4 task is text-only."
            ),
        },
        "routing_compatibility": {
            "injector": (
                "localhost stream proxy adds only the frozen provider object; it does "
                "not translate protocol, prompt, tools, reasoning, responses, retries, "
                "or timeouts"
            ),
            "compat_fields": {
                "supportsDeveloperRole": False,
                "supportsReasoningEffort": True,
                "maxTokensField": "max_tokens",
                "thinkingFormat": "openrouter",
            },
        },
        "sandbox": {
            "provider": "local Docker via Harbor",
            "per_task_image_cpu_memory": "recorded in scope task metadata",
            "network_policy": "task-default outbound Docker network",
            "dsh_inner_permission": "danger-full-access",
            "safety_boundary": "Harbor task container",
        },
        "accounting": {
            "primary": "OpenRouter settled generation records by captured generation id",
            "secondary": ["DSH native session usage", "Harbor AgentResult"],
            "title_call": "included in OpenRouter total; absent from main DSH session usage",
            "litellm": "not present",
            "double_count_rule": "never sum duplicate representations of one call",
            "estimated_cost_usd": (
                "$5-$15 campaign estimate from the audited GLM task token profile; "
                "no dollar hard-stop guard"
            ),
        },
        "post_run_audit": {
            "required": True,
            "scope": "all four trajectories and every injector request",
            "checks": [
                "actual model/provider/quantization and no fallback",
                "standard preset and 26-tool catalog",
                "tool-result and reasoning replay, including Kimi preserved reasoning",
                "subagent effort inheritance if invoked",
                "compaction, output cap, retries, 429, timeout, and verifier outcome",
                "title/main request separation and provider/DSH/Harbor token accounting",
                "no skills, MCP, prompt augmentation, or modality contamination",
            ],
        },
        "source_sha256": {
            "pilot/tb4_dsh_openrouter.py": sha256_file(Path(__file__)),
            "pilot/tb4_dsh_glm.py": sha256_file(ROOT / "pilot" / "tb4_dsh_glm.py"),
            "integrations/harbor_deepseek.py": sha256_file(
                ROOT / "integrations" / "harbor_deepseek.py"
            ),
            "integrations/openrouter_body_injector.mjs": sha256_file(
                ROOT / "integrations" / "openrouter_body_injector.mjs"
            ),
        },
    }
    manifest["manifest_sha256"] = hashlib.sha256(canonical_json(manifest)).hexdigest()
    return manifest


def preflight() -> int:
    manifest = build_manifest()
    with tempfile.TemporaryDirectory(prefix="tb4-dsh-other-", dir="/tmp") as raw:
        temp = Path(raw)
        # The unchanged log-permission guard already passed the audited GLM B
        # qualification. Re-importing Harbor only to print the shell snippet can
        # leave an SDK cleanup thread alive in this operator environment.
        legacy.injector_self_test(temp)
        for logical_model, spec in MODEL_SPECS.items():
            config_path = temp / f"{logical_model}.json"
            write_json(config_path, harbor_config(logical_model))
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
            kwargs = agent.get("kwargs") or {}
            if (
                agent.get("model_name") != f"openrouter/{spec['model']}"
                or kwargs.get("reasoning_effort") != "high"
                or kwargs.get("input_modalities") != spec["input"]
                or kwargs.get("openrouter_route") != spec["route"]
            ):
                raise RuntimeError(f"Harbor resolution drift for {logical_model}")
        install_path = temp / "install.json"
        write_json(
            install_path,
            harbor_config(
                "claude-opus-5", install_only=True, jobs_dir=temp / "install-only"
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
            raise RuntimeError("Harbor DSH install-only preflight failed")
    print("Zero-cost preflight passed for Claude, GPT, Kimi, and DeepSeek.")
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
        path = RUN_DIR / "configs" / f"{logical_model}.json"
        write_json(path, harbor_config(logical_model))
        configs[logical_model] = path
    write_json(RUN_DIR / "manifest.json", manifest)
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
        session_paths = sorted(
            trial_dir.glob("agent/deepseek-harness/sessions/**/session.jsonl")
        )
        session_records = [
            item
            for path in session_paths
            for item in smoke.parse_json_lines(path)
        ]
        injector_records = [
            item
            for path in trial_dir.glob(
                "agent/deepseek-harness/openrouter-injector.jsonl"
            )
            for item in smoke.parse_json_lines(path)
        ]
        retry_records = [item for item in session_records if item.get("type") == "llm/retry"]
        assistant_messages = [
            item for item in session_records if item.get("type") == "assistant/message"
        ]
        request_headers = [
            (item.get("data") or {}).get("header") or {}
            for item in session_records
            if item.get("type") == "request/header"
        ]
        compactions = [
            item for item in session_records if item.get("type") == "compaction/start"
        ]
        end_records = [
            item for item in injector_records if item.get("event") in (None, "request_end")
        ]
        trials.append(
            {
                "trial_name": result.get("trial_name"),
                "task_name": result.get("task_name"),
                "reward": (result.get("verifier_result") or {}).get("rewards"),
                "exception_info": result.get("exception_info"),
                "agent_result": result.get("agent_result"),
                "provider_generation_count": len(records),
                "provider_cost_usd": sum(float(item.get("total_cost") or 0) for item in records),
                "actual_models": sorted({str(item.get("model")) for item in records if item.get("model")}),
                "actual_providers": sorted({str(item.get("provider_name")) for item in records if item.get("provider_name")}),
                "quantization_evidence": (
                    "strict route tag/endpoint snapshot; OpenRouter generation records "
                    "do not expose a separate quantization field"
                ),
                "generation_lookup_errors": generations.get("errors"),
                "dsh_session_count": len(session_paths),
                "standard_session_headers": sum(
                    (item.get("agentPreset") == "standard")
                    for item in session_records
                    if item.get("type") == "session"
                ),
                "request_tool_catalog_sizes": [len(item.get("tools") or []) for item in request_headers],
                "tool_calls": sum(item.get("type") == "tool/call" for item in session_records),
                "tool_results": sum(item.get("type") == "tool/result" for item in session_records),
                "assistant_messages": len(assistant_messages),
                "reasoning_messages": sum(
                    any(
                        isinstance(block, dict) and block.get("type") == "reasoning"
                        for block in ((item.get("data") or {}).get("message") or {}).get("content", [])
                    )
                    for item in assistant_messages
                ),
                "compaction_count": len(compactions),
                "dsh_retry_events": len(retry_records),
                "injector_requests": len(end_records),
                "injector_non_provider_changes": sum(
                    item.get("changed_fields") not in (["provider"], []) for item in end_records
                ),
                "injector_retries": sum(int(item.get("injector_retry_count") or 0) for item in end_records),
                "expected_model": spec["expected_model"],
                "expected_provider": spec["provider_name"],
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
    before = legacy.common.current_key_usage(api_key)
    write_json(RUN_DIR / "openrouter-usage-before.json", before, mode=0o600)
    return_codes: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=CAMPAIGN_CONCURRENCY) as pool:
        futures = {
            pool.submit(run_cell, logical_model, path, api_key): logical_model
            for logical_model, path in configs.items()
        }
        for future in as_completed(futures):
            logical_model = futures[future]
            return_codes[logical_model] = future.result()
    after = legacy.common.current_key_usage(api_key)
    write_json(RUN_DIR / "openrouter-usage-after.json", after, mode=0o600)
    audit = {
        "campaign_id": CAMPAIGN_ID,
        "manifest_sha256": manifest["manifest_sha256"],
        "decision": "pending mandatory full trajectory review; never primary-score data",
        "cells": [
            audit_cell(name, api_key, return_codes.get(name, 125))
            for name in MODEL_SPECS
        ],
    }
    write_json(RUN_DIR / "audit.json", audit)
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
