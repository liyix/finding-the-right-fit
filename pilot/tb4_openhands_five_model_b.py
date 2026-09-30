#!/usr/bin/env python3
"""Qualify the final patched OpenHands integration on five OpenRouter models."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
from typing import Any
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN_ID = "pilot-tb4-openhands-five-model-b-20260901-r1"
RUN_DIR = ROOT / "runs" / CAMPAIGN_ID
HARBOR_DIR = RUN_DIR / "harbor"
TASK = "vllm-deepseek-streaming"
CANARY = "deepseek-v4-pro"
STAGGER_SECONDS = 15
MODELS: dict[str, dict[str, Any]] = {
    "claude-opus-5": {
        "model": "anthropic/claude-opus-5",
        "provider": {
            "only": ["anthropic"],
            "allow_fallbacks": False,
            "require_parameters": True,
        },
        "capabilities": {"supports_vision": True},
    },
    "gpt-6-astra": {
        "model": "openai/gpt-6-astra",
        "provider": {
            "only": ["openai"],
            "allow_fallbacks": False,
            "require_parameters": True,
        },
        "capabilities": {"supports_vision": True},
    },
    "glm-5.3": {
        "model": "z-ai/glm-5.3",
        "provider": {
            "only": ["z-ai/fp8"],
            "quantizations": ["fp8"],
            "allow_fallbacks": False,
            "require_parameters": True,
        },
        "capabilities": {
            "supports_reasoning_effort": True,
            "supports_vision": False,
        },
    },
    "kimi-k3": {
        "model": "moonshotai/kimi-k3",
        "provider": {
            "only": ["moonshotai/mxfp4"],
            "quantizations": ["mxfp4"],
            "allow_fallbacks": False,
            "require_parameters": True,
        },
        "capabilities": {
            "supports_reasoning_effort": True,
            "supports_vision": True,
        },
        "inline_image_urls": True,
    },
    "deepseek-v4-pro": {
        "model": "deepseek/deepseek-v4-pro-0813",
        "provider": {
            "only": ["deepseek"],
            "allow_fallbacks": False,
            "require_parameters": True,
        },
        "capabilities": {
            "supports_reasoning_effort": True,
            "supports_vision": False,
        },
    },
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_frozen(path: Path, value: Any, mode: int = 0o444) -> None:
    encoded = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != encoded:
            raise RuntimeError(f"refusing to mutate frozen file: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(encoded, encoding="utf-8")
    path.chmod(mode)


def write_state(value: Any) -> None:
    path = RUN_DIR / "run-state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def job_name(logical_model: str) -> str:
    return f"tb4--openhands-patched--{logical_model}--{TASK}--rep-1"


def harbor_config(logical_model: str, *, jobs_dir: Path = HARBOR_DIR) -> dict[str, Any]:
    target = MODELS[logical_model]
    kwargs: dict[str, Any] = {
        "version": "1.44.1",
        "reasoning_effort": "high",
        "max_input_tokens": 1_000_000,
        "max_output_tokens": 128_000,
        "api_mode": "auto",
        "capability_overrides": target["capabilities"],
        "openrouter_provider": target["provider"],
        "load_skills": False,
        "max_iterations": 500,
        "temperature": None,
    }
    if target.get("inline_image_urls"):
        kwargs["inline_image_urls"] = True
    return {
        "job_name": job_name(logical_model),
        "jobs_dir": str(jobs_dir),
        "n_attempts": 1,
        "install_only": False,
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
        "agents": [
            {
                "import_path": "integrations.harbor:ControlledOpenHandsSDK",
                "model_name": f"openrouter/{target['model']}",
                "n_concurrent": 1,
                "skills": [],
                "resume_trajectory": False,
                "include_logs": ["**/*"],
                "kwargs": kwargs,
                "mcp_servers": [],
            }
        ],
        "datasets": [
            {
                "path": str(ROOT / "vendor" / "terminal-bench"),
                "task_names": [TASK],
            }
        ],
        "tasks": [],
        "artifacts": [],
        "extra_instruction_paths": [],
        "extra_instructions": [],
    }


def manifest_body() -> dict[str, Any]:
    task_dir = ROOT / "vendor" / "terminal-bench" / TASK
    task_toml = task_dir / "task.toml"
    instruction = task_dir / "instruction.md"
    return {
        "campaign_id": CAMPAIGN_ID,
        "kind": "paid-benchmark-qualification-pilot-not-formal-score",
        "benchmark": {
            "name": "Terminal-Bench 4.0",
            "tag": "v4.0.0",
            "commit": "452bf305c6daa62fc59061d22133a7cbc7c1572e",
            "task": TASK,
            "task_toml_sha256": sha256_file(task_toml),
            "instruction_sha256": sha256_file(instruction),
            "task_modality": "text-only",
            "agent_image": "harborframework/terminal-bench:vllm-deepseek-streaming-environment-a6e209d1612afee4@sha256:2bbccf4d1c264df16294be81c91be386961938dcc1d58c6b74a04425a7ff0bf3",
            "verifier_image": "harborframework/terminal-bench:vllm-deepseek-streaming-verifier-83c62854223894b0@sha256:bf50893791878aef03381ee543698baf0c2c16ff5e94ac6c03d9eba3b735c2d5",
            "replicate": 1,
            "planned_trials": 5,
        },
        "runner": {"name": "Harbor", "version": "0.22.0", "commit": "4407eb5"},
        "harness": {
            "name": "OpenHands SDK/Tools",
            "version": "1.44.1",
            "install": "openhands-sdk==1.44.1 openhands-tools==1.44.1 fastapi",
            "integration": "Harbor built-in plus controlled LLM/audit overlay and reasoning replay patch",
            "entry": "Harbor OpenHands SDK runner",
            "adapter_sha256": sha256_file(ROOT / "integrations" / "harbor.py"),
            "reasoning_patch": "openhands-1.44.1-openrouter-reasoning-details-v1",
            "reasoning_patch_sha256": sha256_file(
                ROOT / "integrations" / "openhands_reasoning_details_patch.py"
            ),
        },
        "models": [
            {
                "logical": logical,
                "id": target["model"],
                "provider_route": target["provider"],
                "base_url": "https://openrouter.ai/api/v1",
                "protocol": "OpenAI Chat Completions via OpenRouter/LiteLLM",
                "path": "provider-compatible plus complete reasoning_details replay",
            }
            for logical, target in MODELS.items()
        ],
        "controls": {
            "reasoning_effort": "high study control; reasoning_effort=high on wire",
            "reasoning_continuity": "complete reasoning_details JSON-value replay when emitted",
            "context_window": "1,000,000 study override",
            "compaction": "none; Harbor upstream OpenHands runner has no condenser",
            "max_output_tokens": "128,000 OpenHands/OpenRouter compatibility study override",
            "sampling": "temperature/top_p/top_k/seed absent; fixed provider defaults",
            "max_iterations": 500,
            "task_timeout_seconds": 28_800,
            "harbor_agent_timeout_override": None,
            "verifier_timeout_seconds": 300,
            "llm_timeout_seconds": 300,
            "llm_attempt_limit": "5 total attempts / at most 4 retries",
            "llm_backoff_seconds": "exponential multiplier 8, min 8, max 64",
            "harbor_outer_retries": 0,
            "tools": ["terminal", "file_editor", "task_tracker", "finish", "think"],
            "skills": False,
            "mcp": [],
            "subagents": False,
            "memory": "upstream OpenHands default only; no project memory mounted",
            "benchmark_specific_augmentation": False,
        },
        "scheduling": {
            "stage_1": "DeepSeek canary, one trial",
            "stage_2_gate": "start only if canary has one completed non-errored Harbor trial",
            "stage_2": "Claude/GPT/GLM/Kimi, four model jobs in parallel",
            "stage_2_start_stagger_seconds": STAGGER_SECONDS,
            "maximum_model_request_lanes": 4,
            "harbor_trials_per_model": 1,
        },
        "sandbox": {
            "provider": "Harbor Docker",
            "agent_cpu": 2,
            "agent_memory_mb": 4096,
            "agent_storage_mb": 10240,
            "gpus": 0,
            "network": "TB4/Harbor task default",
            "timeout_multiplier": 1.0,
        },
        "accounting": {
            "primary": "OpenRouter provider usage/cost embedded in each captured completion and settled generation records",
            "secondary": "OpenHands native metrics and Harbor aggregate; never summed",
            "estimate_usd": 15.0,
            "conservative_uncertainty_usd": 30.0,
            "budget_behavior": "monitor and report; do not kill active agent/verifier for a soft estimate",
            "shared_key_limit": "account-level delta can include concurrent campaigns and is not attributable",
        },
        "acceptance": {
            "lifecycle": "each arm reaches native finish, valid OpenHands events/trajectory/final metrics, Harbor result, artifact collection, and verifier",
            "routing": "every generation matches the frozen requested model and provider with no fallback",
            "wire": "reasoning_effort=high and max_tokens=128000 on every request; sampling fields absent",
            "reasoning": "every emitted reasoning_details block is preserved across the next tool turn with canonical JSON equality; no signature/history error",
            "tools": "all observed tool calls have matching results and the five upstream-default tool schemas remain stable",
            "recovery": "all retries, 429s, timeouts, compaction and cap events are classified and costed; Harbor outer retry remains zero",
            "audit": "full trajectory audit accepts the arm as qualification evidence only",
        },
        "configs": {
            logical: {
                "path": f"configs/{logical}.json",
                "sha256": hashlib.sha256(canonical(harbor_config(logical))).hexdigest(),
            }
            for logical in MODELS
        },
        "source": {
            "launcher_sha256": sha256_file(Path(__file__).resolve()),
            "experiment_yaml_sha256": sha256_file(ROOT / "experiment.yaml"),
            "compatibility_report_sha256": sha256_file(
                ROOT / "reports" / "provider-compatibility.md"
            ),
            "runbook_sha256": sha256_file(ROOT / "RUNBOOK.md"),
        },
    }


def materialize() -> dict[str, Any]:
    for logical in MODELS:
        write_frozen(RUN_DIR / "configs" / f"{logical}.json", harbor_config(logical))
    body = manifest_body()
    manifest = dict(body)
    manifest["manifest_sha256"] = hashlib.sha256(canonical(body)).hexdigest()
    write_frozen(RUN_DIR / "manifest.json", manifest)
    return manifest


def tool_env(key: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "LLM_API_KEY": key,
            "LLM_BASE_URL": "https://openrouter.ai/api/v1",
            "UV_CACHE_DIR": "/tmp/harness-test-uv-cache",
            "UV_TOOL_DIR": "/tmp/harness-test-uv-tools",
            "PYTHONPATH": str(ROOT),
        }
    )
    return env


def paid_env() -> dict[str, str]:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is not loaded")
    return tool_env(key)


def command(config: Path, *extra: str) -> list[str]:
    return [
        "uvx", "--offline", "--from", "harbor==0.22.0", "harbor", "run",
        "--config", str(config), *extra, "--yes",
    ]


def key_usage(key: str) -> dict[str, Any]:
    request = urllib.request.Request(
        "https://openrouter.ai/api/v1/auth/key",
        headers={"Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.load(response).get("data", {})
    return {
        "captured_at_utc": utc_now(),
        "usage": data.get("usage"),
        "limit": data.get("limit"),
        "limit_remaining": data.get("limit_remaining"),
    }


def preflight() -> int:
    manifest = materialize()
    with tempfile.TemporaryDirectory(prefix="oh-five-b-preflight-", dir="/tmp") as raw:
        completed = subprocess.run(
            command(
                RUN_DIR / "configs" / f"{CANARY}.json",
                "--install-only",
                "--job-name", "qualification-openhands-five-model-install",
                "--jobs-dir", str(Path(raw) / "jobs"),
            ),
            cwd=ROOT,
            env=tool_env("preflight-not-a-secret"),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30 * 60,
            check=False,
        )
    evidence = {
        "status": "passed" if completed.returncode == 0 else "failed",
        "completed_at_utc": utc_now(),
        "manifest_sha256": manifest["manifest_sha256"],
        "exact_task_image": TASK,
        "paid_model_calls": 0,
        "return_code": completed.returncode,
        "log_tail": completed.stdout[-4000:],
    }
    write_frozen(RUN_DIR / "preflight.json", evidence)
    return completed.returncode


def start_job(logical: str, env: dict[str, str]) -> tuple[subprocess.Popen[Any], Any]:
    output = HARBOR_DIR / job_name(logical)
    if output.exists():
        raise RuntimeError(f"refusing to reuse output: {output}")
    log_path = RUN_DIR / "logs" / f"{logical}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        command(RUN_DIR / "configs" / f"{logical}.json"),
        cwd=ROOT,
        env=env,
        text=True,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return process, log


def canary_passed() -> bool:
    result_path = HARBOR_DIR / job_name(CANARY) / "result.json"
    if not result_path.is_file():
        return False
    result = json.loads(result_path.read_text(encoding="utf-8"))
    stats = result.get("stats") or {}
    return stats.get("n_completed_trials") == 1 and stats.get("n_errored_trials") == 0


def run(approved_hash: str) -> int:
    manifest = materialize()
    if manifest["manifest_sha256"] != approved_hash:
        raise RuntimeError("manifest hash mismatch")
    preflight_record = json.loads((RUN_DIR / "preflight.json").read_text())
    if preflight_record.get("status") != "passed":
        raise RuntimeError("matching preflight has not passed")
    if preflight_record.get("manifest_sha256") != approved_hash:
        raise RuntimeError("preflight manifest mismatch")

    env = paid_env()
    before = key_usage(env["OPENROUTER_API_KEY"])
    write_frozen(RUN_DIR / "openrouter-usage-before.json", before, mode=0o600)
    state: dict[str, Any] = {
        "campaign_id": CAMPAIGN_ID,
        "manifest_sha256": approved_hash,
        "status": "canary_running",
        "started_at_utc": utc_now(),
        "jobs": {},
    }

    canary_process, canary_log = start_job(CANARY, env)
    state["jobs"][CANARY] = {"pid": canary_process.pid, "status": "running"}
    write_state(state)
    canary_rc = canary_process.wait()
    canary_log.close()
    state["jobs"][CANARY].update({"return_code": canary_rc, "status": "finished"})
    if canary_rc != 0 or not canary_passed():
        state.update(
            {
                "status": "canary_failed_stage_2_not_started",
                "finished_at_utc": utc_now(),
            }
        )
        write_state(state)
        return canary_rc or 2

    state["status"] = "stage_2_running"
    write_state(state)
    processes: dict[str, tuple[subprocess.Popen[Any], Any]] = {}
    for logical in MODELS:
        if logical == CANARY:
            continue
        process, log = start_job(logical, env)
        processes[logical] = (process, log)
        state["jobs"][logical] = {"pid": process.pid, "status": "running"}
        write_state(state)
        time.sleep(STAGGER_SECONDS)

    return_code = 0
    for logical, (process, log) in processes.items():
        rc = process.wait()
        log.close()
        state["jobs"][logical].update({"return_code": rc, "status": "finished"})
        write_state(state)
        return_code = return_code or rc

    after = key_usage(env["OPENROUTER_API_KEY"])
    write_frozen(RUN_DIR / "openrouter-usage-after.json", after, mode=0o600)
    if isinstance(before.get("usage"), (int, float)) and isinstance(
        after.get("usage"), (int, float)
    ):
        state["shared_key_usage_delta_usd"] = float(after["usage"]) - float(
            before["usage"]
        )
    state.update(
        {
            "status": "finished",
            "return_code": return_code,
            "finished_at_utc": utc_now(),
            "trajectory_audit_required": True,
        }
    )
    write_state(state)
    return return_code


def launch(approved_hash: str) -> int:
    materialize()
    if subprocess.run(
        ["tmux", "has-session", "-t", CAMPAIGN_ID],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0:
        raise RuntimeError(f"tmux session already exists: {CAMPAIGN_ID}")
    command_line = (
        f"set -a; source {shlex.quote(str(ROOT / '.env'))}; set +a; "
        f"exec {shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} "
        f"run --approved-manifest-sha256 {shlex.quote(approved_hash)}"
    )
    subprocess.run(
        [
            "tmux", "new-session", "-d", "-s", CAMPAIGN_ID,
            "-c", str(ROOT), "bash", "-lc", command_line,
        ],
        check=True,
    )
    print(CAMPAIGN_ID)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("materialize")
    sub.add_parser("preflight")
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--approved-manifest-sha256", required=True)
    launch_parser = sub.add_parser("launch")
    launch_parser.add_argument("--approved-manifest-sha256", required=True)
    args = parser.parse_args()
    if args.command == "materialize":
        print(json.dumps(materialize(), indent=2, sort_keys=True))
        return 0
    if args.command == "preflight":
        return preflight()
    if args.command == "run":
        return run(args.approved_manifest_sha256)
    if args.command == "launch":
        return launch(args.approved_manifest_sha256)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
