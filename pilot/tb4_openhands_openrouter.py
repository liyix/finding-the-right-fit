#!/usr/bin/env python3
"""Approved TB4 OpenHands x four-model OpenRouter pilot."""

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
import tomllib
import traceback
import urllib.request
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN_ID = "pilot-tb4-openhands-openrouter-four-model-20260831-r1"
RUN_DIR = ROOT / "runs" / CAMPAIGN_ID
HARBOR_DIR = RUN_DIR / "harbor"
HARBOR_VERSION = "0.22.0"
HARBOR_COMMIT = "4407eb5"
OPENHANDS_VERSION = "1.44.1"
TB4_COMMIT = "452bf305c6daa62fc59061d22133a7cbc7c1572e"
TASKS = ("html-js-filter", "music-harmony")
TASK_CHECKSUMS = {
    "html-js-filter": "f9e9f9f97cc4ed197e51c0f79218cba93a9dac01e736e1c1076d7ac91f41f1c7",
    "music-harmony": "9623bde8b8df64f044ba142496347ceb058d393e1a2319669250312e8799783f",
}
MODELS: dict[str, dict[str, Any]] = {
    "claude-opus-5": {
        "model": "anthropic/claude-opus-5",
        "route": {
            "only": ["anthropic"],
            "allow_fallbacks": False,
            "require_parameters": True,
        },
        "capabilities": {"supports_vision": True},
    },
    "gpt-6-astra": {
        "model": "openai/gpt-6-astra",
        "route": {
            "only": ["openai"],
            "allow_fallbacks": False,
            "require_parameters": True,
        },
        "capabilities": {"supports_vision": True},
    },
    "glm-5.3": {
        "model": "z-ai/glm-5.3",
        "route": {
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
        "route": {
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
}
CONTEXT_WINDOW = 1_000_000
CONCURRENCY = 4
SOFT_LIMIT_PER_TRIAL_USD = 3.0
SOFT_LIMIT_TOTAL_USD = 24.0
UV_CACHE_DIR = "/tmp/harness-test-uv-cache"
UV_TOOL_DIR = "/tmp/harness-test-uv-tools"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def write_new_json(path: Path, value: Any, mode: int = 0o444) -> None:
    encoded = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if path.exists():
        if path.read_text() != encoded:
            raise RuntimeError(f"refusing to mutate frozen file: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(encoded)
    path.chmod(mode)


def write_state(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def task_metadata(name: str) -> dict[str, Any]:
    path = ROOT / "vendor" / "terminal-bench" / name / "task.toml"
    raw = tomllib.loads(path.read_text())
    env = raw["environment"]
    verifier_env = raw["verifier"].get("environment", {})
    fields = ("cpus", "memory_mb", "storage_mb", "gpus", "docker_image")
    return {
        "name": name,
        "harbor_checksum": TASK_CHECKSUMS[name],
        "packaged_task_toml_sha256": sha256_file(path),
        "agent_timeout_seconds": raw["agent"]["timeout_sec"],
        "verifier_timeout_seconds": raw["verifier"]["timeout_sec"],
        "agent_environment": {key: env.get(key) for key in fields},
        "verifier_environment": {key: verifier_env.get(key) for key in fields},
    }


def job_name(model: str, task: str) -> str:
    return f"tb4--openhands--{model}--{task}--rep-1"


def harbor_config(model: str, task: str) -> dict[str, Any]:
    target = MODELS[model]
    kwargs: dict[str, Any] = {
        "version": OPENHANDS_VERSION,
        "reasoning_effort": "high",
        "max_input_tokens": CONTEXT_WINDOW,
        "api_mode": "auto",
        "capability_overrides": target["capabilities"],
        "openrouter_provider": target["route"],
        "load_skills": False,
        "max_iterations": 500,
        "temperature": None,
    }
    if "inline_image_urls" in target:
        kwargs["inline_image_urls"] = target["inline_image_urls"]
    return {
        "job_name": job_name(model, task),
        "jobs_dir": str(HARBOR_DIR),
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
                "task_names": [task],
            }
        ],
        "tasks": [],
        "artifacts": [],
        "extra_instruction_paths": [],
        "extra_instructions": [],
    }


def manifest_body() -> dict[str, Any]:
    cells = []
    for task in TASKS:
        for model, target in MODELS.items():
            config_path = RUN_DIR / "configs" / f"harbor-{model}-{task}.json"
            cells.append(
                {
                    "cell_id": f"{model}--{task}",
                    "model": target["model"],
                    "provider_route": target["route"],
                    "task": task,
                    "replicate": 1,
                    "harbor_config": str(config_path.relative_to(ROOT)),
                    "harbor_config_sha256": sha256_bytes(canonical(harbor_config(model, task))),
                }
            )
    return {
        "campaign_id": CAMPAIGN_ID,
        "kind": "paid-pilot-not-formal-score",
        "approval_basis": (
            "User explicitly approved the resolved eight-trial OpenHands pilot "
            "and instructed execution without another approval round."
        ),
        "benchmark": {
            "name": "Terminal-Bench 4.0",
            "tag": "v4.0.0",
            "upstream_commit": TB4_COMMIT,
            "dataset_path": "vendor/terminal-bench (Harbor-packaged task metadata/images)",
            "tasks": [task_metadata(task) for task in TASKS],
        },
        "runner": {"name": "Harbor", "version": HARBOR_VERSION, "commit": HARBOR_COMMIT},
        "harness": {
            "name": "OpenHands SDK/Tools",
            "version": OPENHANDS_VERSION,
            "install": f"openhands-sdk=={OPENHANDS_VERSION} openhands-tools=={OPENHANDS_VERSION}",
            "integration": "Harbor built-in plus thin LLM-control/audit overlay",
            "import_path": "integrations.harbor:ControlledOpenHandsSDK",
            "adapter_sha256": sha256_file(ROOT / "integrations" / "harbor.py"),
            "entry": "Harbor OpenHands SDK runner",
        },
        "controls": {
            "provider": "OpenRouter only",
            "base_url": "https://openrouter.ai/api/v1",
            "protocol": "OpenHands api_mode=auto; resolved per model and captured",
            "reasoning_effort": "high study override",
            "context_window": CONTEXT_WINDOW,
            "output_budget": "OpenHands native/unset; no study max token field",
            "temperature_top_p_top_k_seed": "unset; fixed endpoint provider defaults",
            "tools": ["TerminalTool", "FileEditorTool", "TaskTrackerTool", "internal think"],
            "skills": False,
            "mcp": [],
            "subagents": False,
            "condenser": "none (Harbor upstream runner)",
            "max_iterations": 500,
            "llm_timeout_seconds": 300,
            "llm_attempt_limit": 5,
            "llm_backoff_seconds": "8-64 SDK native",
            "outer_task_timeout_seconds": 28800,
            "timeout_multiplier": 1.0,
            "harbor_max_retries": 0,
            "total_concurrency": CONCURRENCY,
        },
        "budget": {
            "soft_per_trial_usd": SOFT_LIMIT_PER_TRIAL_USD,
            "soft_total_usd": SOFT_LIMIT_TOTAL_USD,
            "policy": (
                "Run four cells concurrently per task. After phase one, query OpenRouter "
                "current-key usage; do not start phase two if phase-one delta plus its "
                "$12 conservative allowance reaches the $24 soft line. Never terminate "
                "an active agent/verifier for a soft budget."
            ),
        },
        "accounting": {
            "primary": "OpenRouter current-key usage and settled generation records",
            "secondary": "OpenHands native metrics and Harbor aggregate; never summed",
        },
        "cells": cells,
        "source": {
            "launcher_sha256": sha256_file(Path(__file__).resolve()),
            "experiment_yaml_sha256": sha256_file(ROOT / "experiment.yaml"),
            "compatibility_report_sha256": sha256_file(ROOT / "reports/provider-compatibility.md"),
            "runbook_sha256": sha256_file(ROOT / "RUNBOOK.md"),
        },
    }


def materialize() -> dict[str, Any]:
    if RUN_DIR.exists() and not (RUN_DIR / "manifest.json").exists():
        raise RuntimeError(f"incomplete pre-existing run directory: {RUN_DIR}")
    for task in TASKS:
        for model in MODELS:
            write_new_json(
                RUN_DIR / "configs" / f"harbor-{model}-{task}.json",
                harbor_config(model, task),
            )
    body = manifest_body()
    manifest = dict(body)
    manifest["manifest_sha256"] = sha256_bytes(canonical(body))
    write_new_json(RUN_DIR / "manifest.json", manifest)
    return manifest


def harbor_env() -> dict[str, str]:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is not loaded")
    env = os.environ.copy()
    env.update(
        {
            "LLM_API_KEY": key,
            "LLM_BASE_URL": "https://openrouter.ai/api/v1",
            "UV_CACHE_DIR": UV_CACHE_DIR,
            "UV_TOOL_DIR": UV_TOOL_DIR,
            "PYTHONPATH": str(ROOT),
        }
    )
    return env


def harbor_command(config: Path) -> list[str]:
    return [
        "uvx", "--offline", "--from", f"harbor=={HARBOR_VERSION}",
        "harbor", "run", "--config", str(config), "--yes",
    ]


def current_key_usage(key: str) -> dict[str, Any]:
    request = urllib.request.Request(
        "https://openrouter.ai/api/v1/auth/key",
        headers={"Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = json.load(response)
    data = raw.get("data", raw)
    return {
        "captured_at_utc": utc_now(),
        "usage": data.get("usage"),
        "usage_daily": data.get("usage_daily"),
        "usage_weekly": data.get("usage_weekly"),
        "usage_monthly": data.get("usage_monthly"),
        "limit": data.get("limit"),
        "limit_remaining": data.get("limit_remaining"),
    }


def preflight() -> dict[str, Any]:
    manifest = materialize()
    for cell in manifest["cells"]:
        path = ROOT / cell["harbor_config"]
        saved = json.loads(path.read_text())
        model, task = cell["cell_id"].split("--", 1)
        if saved != harbor_config(model, task):
            raise RuntimeError(f"frozen Harbor config drift: {cell['cell_id']}")
    with tempfile.TemporaryDirectory(prefix="openhands-install-preflight-", dir="/tmp") as raw:
        temp = Path(raw)
        config = harbor_config("claude-opus-5", TASKS[0])
        config["job_name"] = "qualification-openhands-import-20260831"
        config["jobs_dir"] = str(temp / "harbor")
        config["install_only"] = True
        config_path = temp / "install-only.json"
        config_path.write_text(json.dumps(config, indent=2) + "\n")
        completed = subprocess.run(
            harbor_command(config_path),
            cwd=ROOT,
            env=harbor_env(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30 * 60,
            check=False,
        )
        if completed.returncode:
            raise RuntimeError("Harbor OpenHands install-only preflight failed:\n" + completed.stdout[-8000:])
    evidence = {
        "status": "passed",
        "completed_at_utc": utc_now(),
        "manifest_sha256": manifest["manifest_sha256"],
        "paid_model_calls": 0,
        "checks": [
            "frozen manifest/config parity",
            "exact dataset task metadata and timeouts",
            "Harbor custom-agent import and OpenHands 1.44.1 install-only lifecycle",
        ],
    }
    write_new_json(RUN_DIR / "preflight.json", evidence)
    return evidence


def run_phase(task: str, env: dict[str, str], state: dict[str, Any]) -> int:
    processes: dict[str, tuple[subprocess.Popen[Any], Any]] = {}
    for model in MODELS:
        output = HARBOR_DIR / job_name(model, task)
        if output.exists():
            raise RuntimeError(f"refusing to reuse Harbor output: {output}")
        log_path = RUN_DIR / "logs" / f"harbor-{model}-{task}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = log_path.open("w", encoding="utf-8")
        process = subprocess.Popen(
            harbor_command(RUN_DIR / "configs" / f"harbor-{model}-{task}.json"),
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        processes[model] = (process, log)
        state["jobs"][f"{model}--{task}"] = {
            "status": "running", "pid": process.pid, "output": str(output),
        }
        write_state(RUN_DIR / "run-state.json", state)
    overall = 0
    for model, (process, log) in processes.items():
        return_code = process.wait()
        log.close()
        result_path = HARBOR_DIR / job_name(model, task) / "result.json"
        result = json.loads(result_path.read_text()) if result_path.is_file() else {}
        errored = result.get("stats", {}).get("n_errored_trials")
        state["jobs"][f"{model}--{task}"].update(
            {"status": "finished", "return_code": return_code, "n_errored_trials": errored}
        )
        write_state(RUN_DIR / "run-state.json", state)
        if return_code or errored:
            overall = return_code or 1
    return overall


def controller(manifest_hash: str) -> int:
    manifest = materialize()
    if manifest_hash != manifest["manifest_sha256"]:
        raise RuntimeError("manifest hash mismatch")
    evidence = json.loads((RUN_DIR / "preflight.json").read_text())
    if evidence.get("status") != "passed" or evidence.get("manifest_sha256") != manifest_hash:
        raise RuntimeError("matching zero-cost preflight evidence is missing")
    env = harbor_env()
    key = env["OPENROUTER_API_KEY"]
    state: dict[str, Any] = {
        "campaign_id": CAMPAIGN_ID,
        "manifest_sha256": manifest_hash,
        "status": "running",
        "started_at_utc": utc_now(),
        "jobs": {},
        "phases": [],
    }
    write_state(RUN_DIR / "run-state.json", state)
    before = current_key_usage(key)
    write_new_json(RUN_DIR / "openrouter-usage-before.json", before, mode=0o600)
    overall = 0
    for index, task in enumerate(TASKS):
        phase = {"task": task, "started_at_utc": utc_now(), "status": "running"}
        state["phases"].append(phase)
        write_state(RUN_DIR / "run-state.json", state)
        phase_rc = run_phase(task, env, state)
        overall = overall or phase_rc
        after = current_key_usage(key)
        delta = None
        if isinstance(before.get("usage"), (int, float)) and isinstance(after.get("usage"), (int, float)):
            delta = float(after["usage"]) - float(before["usage"])
        phase.update(
            {"finished_at_utc": utc_now(), "status": "finished", "return_code": phase_rc,
             "campaign_usage_delta_usd": delta}
        )
        write_state(RUN_DIR / "run-state.json", state)
        write_new_json(RUN_DIR / f"openrouter-usage-after-phase-{index + 1}.json", after, mode=0o600)
        if index == 0 and (delta is None or delta + 12.0 >= SOFT_LIMIT_TOTAL_USD):
            state["status"] = "soft-budget-stopped-before-phase-2"
            state["finished_at_utc"] = utc_now()
            write_state(RUN_DIR / "run-state.json", state)
            return overall or 2
    state["status"] = "completed" if overall == 0 else "completed-with-errors"
    state["finished_at_utc"] = utc_now()
    state["trajectory_audit_required"] = True
    write_state(RUN_DIR / "run-state.json", state)
    return overall


def launch() -> int:
    manifest = materialize()
    preflight_path = RUN_DIR / "preflight.json"
    if not preflight_path.is_file():
        raise RuntimeError("run preflight before launch")
    session = CAMPAIGN_ID
    if subprocess.run(["tmux", "has-session", "-t", session], stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL, check=False).returncode == 0:
        raise RuntimeError(f"tmux session already exists: {session}")
    inner = (
        f"set -a; source {shlex.quote(str(ROOT / '.env'))}; set +a; exec "
        + shlex.join([sys.executable, str(Path(__file__).resolve()), "_controller",
                      "--manifest-sha256", manifest["manifest_sha256"]])
    )
    command = shlex.join(["/bin/bash", "-lc", inner])
    write_new_json(
        RUN_DIR / "launcher.json",
        {"launched_at_utc": utc_now(), "tmux_session": session,
         "manifest_sha256": manifest["manifest_sha256"],
         "command": "protected .env loaded; secret values omitted"},
    )
    subprocess.run(["tmux", "new-session", "-d", "-s", session, "-c", str(ROOT), command], check=True)
    print(json.dumps({"campaign_id": CAMPAIGN_ID, "manifest_sha256": manifest["manifest_sha256"],
                      "tmux_session": session}, indent=2))
    return 0


def status() -> int:
    alive = subprocess.run(["tmux", "has-session", "-t", CAMPAIGN_ID],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=False).returncode == 0
    result: dict[str, Any] = {"campaign_id": CAMPAIGN_ID, "tmux_alive": alive}
    for name in ("manifest.json", "preflight.json", "launcher.json", "run-state.json"):
        path = RUN_DIR / name
        if path.is_file():
            result[name.removesuffix(".json")] = json.loads(path.read_text())
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare")
    commands.add_parser("preflight")
    commands.add_parser("launch")
    commands.add_parser("status")
    internal = commands.add_parser("_controller")
    internal.add_argument("--manifest-sha256", required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            print(materialize()["manifest_sha256"])
            return 0
        if args.command == "preflight":
            print(json.dumps(preflight(), indent=2))
            return 0
        if args.command == "launch":
            return launch()
        if args.command == "status":
            return status()
        return controller(args.manifest_sha256)
    except BaseException:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
