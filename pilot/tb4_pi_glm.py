#!/usr/bin/env python3
"""Four-task TB4 pilot: Harbor PI -> OpenRouter-pinned GLM-5.3."""

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
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

import provider_smoke as smoke
import tb4_claude_code_glm as common


ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN_ID = "pilot-tb4-pi-glm-concurrency4-20260831-r2"
PREPARED_AT_UTC = "2026-08-31T03:44:00Z"
RUN_DIR = ROOT / "runs" / CAMPAIGN_ID
HARBOR_JOB_NAME = "terminal-bench-4--pi--glm-5.3--rep-1"
HARBOR_JOBS_DIR = RUN_DIR / "harbor"
HARBOR_JOB_DIR = HARBOR_JOBS_DIR / HARBOR_JOB_NAME
PI_MODELS_PATH = RUN_DIR / "pi-models.json"
PI_REMOTE_DIR = "/tmp/harbor-pi-agent"
TASKS = common.TASKS
HARBOR_VERSION = "0.22.0"
PI_VERSION = "0.84.4"
MODEL = "openrouter/z-ai/glm-5.3"
EXPECTED_MODEL = "z-ai/glm-5.3-20260816"
ROUTE = {
    "only": ["z-ai/fp8"],
    "quantizations": ["fp8"],
    "allow_fallbacks": False,
    "require_parameters": True,
}
N_CONCURRENT = 4
WHOLE_RUN_TIMEOUT_SECONDS = 9 * 60 * 60
UV_CACHE_DIR = "/tmp/harness-test-uv-cache"
UV_TOOL_DIR = "/tmp/harness-test-uv-tools"


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def write_json(path: Path, value: Any, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    path.chmod(mode)


def pi_models_json() -> dict[str, Any]:
    # Keep PI's built-in context/output values, but use the parameter name that
    # the pinned Z.AI endpoint advertises. The r1 omission made PI send
    # max_completion_tokens, so require_parameters rejected every request.
    return {
        "providers": {
            "openrouter": {
                "modelOverrides": {
                    "z-ai/glm-5.3": {
                        "compat": {
                            "maxTokensField": "max_tokens",
                            "openRouterRouting": ROUTE,
                        },
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
                ]
            }
        }
    }


def harbor_config(
    *, overlay_path: Path, task_names: tuple[str, ...] = TASKS, install_only: bool = False
) -> dict[str, Any]:
    return {
        "job_name": HARBOR_JOB_NAME + ("--install-only" if install_only else ""),
        "jobs_dir": str(
            HARBOR_JOBS_DIR if not install_only else overlay_path.parent / "install-jobs"
        ),
        "n_attempts": 1,
        "install_only": install_only,
        "timeout_multiplier": 1.0,
        "n_concurrent_trials": 1 if install_only else N_CONCURRENT,
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
                "model_name": MODEL,
                "n_concurrent": 1 if install_only else N_CONCURRENT,
                "skills": [],
                "resume_trajectory": False,
                "include_logs": ["**/*"],
                "kwargs": {"version": PI_VERSION, "thinking": "high"},
                "env": {
                    "OPENROUTER_API_KEY": "${OPENROUTER_API_KEY}",
                    "PI_CODING_AGENT_DIR": PI_REMOTE_DIR,
                    "PI_OFFLINE": "1",
                    "PI_SKIP_VERSION_CHECK": "1",
                },
                "mcp_servers": [],
            }
        ],
        "datasets": [
            {
                "path": str(ROOT / "vendor" / "terminal-bench"),
                "task_names": list(task_names),
            }
        ],
        "tasks": [],
        "artifacts": [],
        "extra_instruction_paths": [],
        "extra_instructions": [],
    }


def build_manifest() -> dict[str, Any]:
    overlay_path = RUN_DIR / "pi.compose.json"
    manifest: dict[str, Any] = {
        "campaign_id": CAMPAIGN_ID,
        "kind": "paid-real-benchmark-pilot-not-for-primary-scores",
        "prepared_at_utc": PREPARED_AT_UTC,
        "scope": {
            "benchmark": "Terminal-Bench 4.0",
            "dataset_tag": "v4.0.0",
            "dataset_commit": common.TB4_COMMIT,
            "tasks": [common.load_task_metadata(name) for name in TASKS],
            "replicate": 1,
            "planned_trials": len(TASKS),
            "n_attempts": 1,
            "concurrency": N_CONCURRENT,
        },
        "runner": {
            "name": "Harbor",
            "version": HARBOR_VERSION,
            "commit": "4407eb5",
            "entry": "uvx --from harbor==0.22.0 harbor run --config <frozen-config> --yes",
            "integration": "Harbor built-in pi agent; no custom Harbor adapter",
            "resolved_job_config": harbor_config(overlay_path=overlay_path),
        },
        "harness": {
            "name": "PI",
            "version": PI_VERSION,
            "source": "npm @earendil-works/pi-coding-agent@0.84.4",
            "entry": (
                "pi --print --mode json --session-dir /logs/agent/pi/sessions "
                "--provider openrouter --model z-ai/glm-5.3 --thinking high"
            ),
            "launch_behavior": "Harbor built-in; upstream PI defaults retained",
            "install_only_preflight": "passed in the pinned TB4 music-harmony image",
        },
        "model_path": {
            "requested_model_id": "z-ai/glm-5.3",
            "expected_actual_model": EXPECTED_MODEL,
            "provider": "OpenRouter pinned to Z.AI",
            "base_url": "https://openrouter.ai/api/v1",
            "protocol": "OpenAI Chat Completions",
            "classification": "PI-native OpenRouter provider; no protocol translation",
            "route": ROUTE,
            "endpoint_snapshot": {
                "checked_at_utc": PREPARED_AT_UTC,
                "provider_name": "Z.AI",
                "tag": "z-ai/fp8",
                "quantization": "fp8",
                "status": 0,
                "context_length": 1048576,
                "max_completion_tokens": 131072,
                "pricing_usd_per_token": {
                    "prompt": "0.0000014",
                    "completion": "0.0000044",
                    "input_cache_read": "0.00000026",
                },
            },
            "compatibility_configuration": (
                "read-only PI models.json modelOverride adds compat.openRouterRouting plus "
                "the name-only maxTokensField=max_tokens mapping required by the pinned endpoint; "
                "the numeric 131072 output budget is unchanged; "
                "PI_OFFLINE=1 freezes the bundled catalog and disables catalog/version traffic, "
                "not inference traffic"
            ),
            "qualification_parent": {
                "campaign_id": "pilot-tb4-pi-glm-concurrency4-20260831-r1",
                "manifest_sha256": "bc5c841c4ce491728290186cdd0dc01bc246994cae93f9228b9436c7fdbec7b9",
                "outcome": (
                    "all four requests rejected before generation because the max_tokens "
                    "compatibility mapping used in the earlier successful PI tool pilot was omitted"
                ),
                "provider_cost_usd": 0,
            },
        },
        "controls": {
            "reasoning_effort": "high via PI --thinking high -> OpenRouter reasoning.effort=high",
            "thinking_token_limit": "unset; PI/model/provider high default",
            "context_window": "1048576 from PI's built-in OpenRouter catalog",
            "output_budget": "131072 from PI's built-in OpenRouter catalog; no override",
            "compaction": {
                "enabled": True,
                "trigger": "context tokens > context window - 16384",
                "reserve_tokens": 16384,
                "keep_recent_tokens": 20000,
                "model": "same active model and high thinking level",
            },
            "max_turns": "unset; PI default",
            "budget_management": (
                "no PI dollar cap and no kill-on-cost monitor; four simultaneous trials; "
                "approved exposure requested as <= $3/task ($12 campaign), not a hard cap"
            ),
            "task_timeout": "task-native agent timeout 28800 seconds",
            "request_timeout": "OpenAI SDK 600 seconds",
            "retries": {
                "harbor_trial": 0,
                "provider_sdk": 0,
                "pi_agent": "enabled, max 3, exponential 2s/4s/8s",
            },
            "concurrency": N_CONCURRENT,
            "temperature": "omitted; provider default",
            "top_p": "omitted; provider default",
            "seed": None,
            "tools": "PI built-in coding tools; no override",
            "skills": [],
            "extensions": [],
            "mcp_servers": [],
            "subagents": "none in the unextended PI harness",
            "memory": "fresh per-trial PI session; resume disabled",
            "prompt_files": [],
            "benchmark_specific_augmentation": False,
        },
        "sandbox": {
            "provider": "local Docker via Harbor",
            "host": {"cpus": 128, "memory_bytes": 1081331798016},
            "per_task_resources_and_images": "see scope.tasks",
            "network_policy": "task-default Docker outbound network",
            "task_timeout_seconds": 28800,
        },
        "accounting": {
            "primary": "OpenRouter current-key usage delta and settled per-generation records",
            "secondary": ["PI native message usage/cost", "Harbor AgentResult"],
            "litellm": "not present",
            "double_count_rule": "same call never summed across sources",
            "estimated_provider_cost_usd": "approximately $2-$6 total; exposure accepted up to $12",
            "known_uncertainty": (
                "OpenRouter settlement can lag; PI native pricing may disagree with provider"
            ),
        },
        "post_run_audit": {
            "required": True,
            "scope": "all four trajectories",
            "checks": [
                "actual model/provider/quantization and every retry",
                "tool/thinking continuity and PI compaction events",
                "unexpected skills/extensions/prompt context",
                "429, timeout, image/modality, verifier, artifact, and loop failures",
                "provider versus PI versus Harbor token/cost disagreement",
            ],
        },
        "source_sha256": {
            "pilot/tb4_pi_glm.py": common.sha256_file(Path(__file__)),
            "harbor-pi.py": common.sha256_file(
                Path(
                    "/tmp/harness-test-uv-cache/archive-v0/Hb8_Z16BVisZs96hPm47x/"
                    "harbor/agents/installed/pi.py"
                )
            ),
        },
    }
    manifest["manifest_sha256"] = hashlib.sha256(canonical_json(manifest)).hexdigest()
    return manifest


def harbor_command(config_path: Path, *, print_config: bool = False) -> list[str]:
    command = [
        "uvx",
        "--offline",
        "--from",
        f"harbor=={HARBOR_VERSION}",
        "harbor",
        "run",
        "--config",
        str(config_path),
    ]
    command.append("--print-config" if print_config else "--yes")
    return command


def tool_env(api_key: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "UV_CACHE_DIR": UV_CACHE_DIR,
            "UV_TOOL_DIR": UV_TOOL_DIR,
            "OPENROUTER_API_KEY": api_key,
        }
    )
    return env


def load_key() -> str:
    value = dotenv_values(ROOT / ".env").get("OPENROUTER_API_KEY")
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("OPENROUTER_API_KEY is missing")
    return value.strip()


def preflight() -> int:
    manifest = build_manifest()
    with tempfile.TemporaryDirectory(prefix="tb4-pi-glm-", dir="/tmp") as raw:
        temp = Path(raw)
        models_path = temp / "models.json"
        overlay_path = temp / "pi.compose.json"
        config_path = temp / "job.json"
        write_json(models_path, pi_models_json())
        write_json(overlay_path, compose_overlay(models_path))
        write_json(config_path, harbor_config(overlay_path=overlay_path))
        completed = subprocess.run(
            harbor_command(config_path, print_config=True),
            cwd=ROOT,
            env=tool_env("preflight-dummy"),
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
        if agent.get("name") != "pi" or agent.get("kwargs", {}).get("version") != PI_VERSION:
            raise RuntimeError("Harbor resolved the wrong PI configuration")
        if agent.get("kwargs", {}).get("thinking") != "high":
            raise RuntimeError("Harbor did not resolve PI thinking=high")
        compat = (
            pi_models_json()["providers"]["openrouter"]["modelOverrides"]
            ["z-ai/glm-5.3"]["compat"]
        )
        if compat.get("maxTokensField") != "max_tokens":
            raise RuntimeError("PI max_tokens compatibility mapping is missing")
        listed = subprocess.run(
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
                "harness-provider-smoke:20260829-r1",
                "pi",
                "--list-models",
                "glm-5.3",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if listed.returncode != 0 or "1.0M" not in listed.stdout or "131.1K" not in listed.stdout:
            print(listed.stdout + listed.stderr, file=sys.stderr)
            raise RuntimeError("PI model metadata preflight failed")
        install_config_path = temp / "install-job.json"
        write_json(
            install_config_path,
            harbor_config(
                overlay_path=overlay_path,
                task_names=("music-harmony",),
                install_only=True,
            ),
        )
        installed = subprocess.run(
            harbor_command(install_config_path),
            cwd=ROOT,
            env=tool_env("preflight-dummy"),
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
        if installed.returncode != 0:
            print(installed.stdout[-5000:] + installed.stderr[-5000:], file=sys.stderr)
            raise RuntimeError("Harbor PI install-only preflight failed")
    print(
        "Zero-cost preflight passed: Harbor PI install/high, PI GLM 1M/131K metadata, "
        "and max_tokens endpoint mapping"
    )
    print(f"Prospective manifest SHA256: {manifest['manifest_sha256']}")
    print("No model generation was made")
    return 0


def freeze(approved_sha256: str) -> tuple[Path, dict[str, Any]]:
    manifest = build_manifest()
    if approved_sha256 != manifest["manifest_sha256"]:
        raise RuntimeError("approved manifest hash does not match the resolved configuration")
    if RUN_DIR.exists():
        raise RuntimeError(f"immutable run directory already exists: {RUN_DIR}")
    RUN_DIR.mkdir(parents=True)
    overlay_path = RUN_DIR / "pi.compose.json"
    config_path = RUN_DIR / "harbor-job.json"
    write_json(PI_MODELS_PATH, pi_models_json())
    write_json(overlay_path, compose_overlay(PI_MODELS_PATH))
    write_json(config_path, harbor_config(overlay_path=overlay_path))
    write_json(RUN_DIR / "manifest.json", manifest)
    return config_path, manifest


def find_trial_results() -> list[tuple[Path, dict[str, Any]]]:
    found: list[tuple[Path, dict[str, Any]]] = []
    if not HARBOR_JOB_DIR.exists():
        return found
    for path in sorted(HARBOR_JOB_DIR.glob("*/result.json")):
        try:
            result = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if result.get("task_name"):
            found.append((path.parent, result))
    return found


def audit(api_key: str, *, return_code: int) -> dict[str, Any]:
    trials: list[dict[str, Any]] = []
    for trial_dir, result in find_trial_results():
        generations = smoke.fetch_openrouter_generation_records(trial_dir, api_key)
        records = generations.get("records") or []
        session_paths = sorted(trial_dir.glob("agent/pi/sessions/**/*.jsonl"))
        text = "\n".join(path.read_text(errors="replace") for path in session_paths)
        trials.append(
            {
                "trial_name": result.get("trial_name"),
                "task_name": result.get("task_name"),
                "reward": (result.get("verifier_result") or {}).get("rewards"),
                "exception_info": result.get("exception_info"),
                "agent_result": result.get("agent_result"),
                "generation_ids": generations.get("generation_ids_found"),
                "generation_lookup_errors": generations.get("errors"),
                "provider_cost_usd": sum(
                    float(item.get("total_cost") or item.get("usage") or 0)
                    for item in records
                ),
                "actual_models": sorted(
                    {str(item.get("model")) for item in records if item.get("model")}
                ),
                "actual_providers": sorted(
                    {str(item.get("provider_name")) for item in records if item.get("provider_name")}
                ),
                "pi_session_count": len(session_paths),
                "compaction_events": text.count('"type":"compaction"')
                + text.count('"type": "compaction"'),
                "retry_events": text.count("auto_retry_start"),
                "rate_limit_mentions": text.lower().count("429")
                + text.lower().count("rate_limit"),
            }
        )
    return {
        "campaign_id": CAMPAIGN_ID,
        "manifest_sha256": build_manifest()["manifest_sha256"],
        "harbor_return_code": return_code,
        "decision": "pending full trajectory review; never primary-score data",
        "counts": {
            "planned": len(TASKS),
            "results": len(trials),
            "exceptions": sum(item["exception_info"] is not None for item in trials),
        },
        "trials": trials,
    }


def run(approved_sha256: str) -> int:
    api_key = load_key()
    config_path, manifest = freeze(approved_sha256)
    usage_before = common.current_key_usage(api_key)
    write_json(RUN_DIR / "openrouter-usage-before.json", usage_before, mode=0o600)
    command = harbor_command(config_path)
    with (RUN_DIR / "harbor-console.log").open("w") as log:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=tool_env(api_key),
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            return_code = process.wait(timeout=WHOLE_RUN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            return_code = 124
    usage_after = common.current_key_usage(api_key)
    write_json(RUN_DIR / "openrouter-usage-after.json", usage_after, mode=0o600)
    write_json(RUN_DIR / "audit.json", audit(api_key, return_code=return_code))
    write_json(
        RUN_DIR / "run-status.json",
        {
            "campaign_id": CAMPAIGN_ID,
            "manifest_sha256": manifest["manifest_sha256"],
            "harbor_return_code": return_code,
            "completed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )
    return return_code


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
