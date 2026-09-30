#!/usr/bin/env python3
"""TB4 qualification for OpenHands GPT Responses multi-item reasoning replay."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN_ID = "pilot-tb4-openhands-gpt-reasoning-roundtrip-20260901-r2"
RUN_DIR = ROOT / "runs" / CAMPAIGN_ID
HARBOR_DIR = RUN_DIR / "harbor"
TASK = "vllm-deepseek-streaming"
MODELS: dict[str, dict[str, Any]] = {
    "gpt-6-astra": {
        "model": "openai/gpt-6-astra",
        "provider": {
            "only": ["openai"],
            "allow_fallbacks": False,
            "require_parameters": True,
        },
        "capabilities": {"supports_vision": True},
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


def write_state(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def job_name(model: str) -> str:
    return f"tb4--openhands--{model}--{TASK}--reasoning-replay--rep-1"


def harbor_config(model: str, *, jobs_dir: Path = HARBOR_DIR) -> dict[str, Any]:
    target = MODELS[model]
    return {
        "job_name": job_name(model),
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
                "kwargs": {
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
                },
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
    task_toml = ROOT / "vendor" / "terminal-bench" / TASK / "task.toml"
    return {
        "campaign_id": CAMPAIGN_ID,
        "kind": "paid-compatibility-pilot-not-formal-score",
        "approval": "pending explicit approval of this immutable manifest",
        "benchmark": {
            "name": "Terminal-Bench 4.0",
            "tag": "v4.0.0",
            "commit": "452bf305c6daa62fc59061d22133a7cbc7c1572e",
            "task": TASK,
            "task_toml_sha256": sha256_file(task_toml),
            "instruction_sha256": sha256_file(task_toml.with_name("instruction.md")),
            "task_modality": "text-only",
            "agent_image": "harborframework/terminal-bench:vllm-deepseek-streaming-environment-a6e209d1612afee4@sha256:2bbccf4d1c264df16294be81c91be386961938dcc1d58c6b74a04425a7ff0bf3",
            "verifier_image": "harborframework/terminal-bench:vllm-deepseek-streaming-verifier-83c62854223894b0@sha256:bf50893791878aef03381ee543698baf0c2c16ff5e94ac6c03d9eba3b735c2d5",
            "replicate": 1,
            "planned_trials": 1,
        },
        "runner": {"name": "Harbor", "version": "0.22.0", "commit": "4407eb5"},
        "harness": {
            "name": "OpenHands SDK/Tools",
            "version": "1.44.1",
            "install": "openhands-sdk==1.44.1 openhands-tools==1.44.1 fastapi",
            "integration": "Harbor built-in plus controlled compatibility/audit overlay",
            "entry": "Harbor OpenHands SDK runner",
            "adapter_sha256": sha256_file(ROOT / "integrations" / "harbor.py"),
            "reasoning_patch": "openhands-1.44.1-openrouter-reasoning-roundtrip-v2",
            "reasoning_patch_sha256": sha256_file(
                ROOT / "integrations" / "openhands_reasoning_details_patch.py"
            ),
            "install_self_test": (
                "mandatory before runner launch: 3 ordered reasoning items plus "
                "3 parallel tool calls through capture, dispatch, event JSON, merge, replay"
            ),
        },
        "models": [
            {
                "logical": logical,
                "id": target["model"],
                "provider_route": target["provider"],
                "base_url": "https://openrouter.ai/api/v1",
                "protocol": "OpenAI Responses via OpenRouter/LiteLLM",
                "path": "provider-compatible plus ordered multi-item reasoning replay patch",
            }
            for logical, target in MODELS.items()
        ],
        "controls": {
            "reasoning_effort": "high study control; exact request field required",
            "reasoning_continuity": (
                "all Responses reasoning items preserved in provider output order; "
                "legacy singular field retained only for old-event compatibility"
            ),
            "context_window": 1_000_000,
            "compaction": "OpenHands default; no condenser in Harbor runner",
            "max_output_tokens": 128_000,
            "sampling": "temperature/top_p/top_k/seed unset; provider defaults",
            "max_iterations": 500,
            "task_timeout_seconds": 28_800,
            "harbor_agent_timeout_override": None,
            "llm_timeout_seconds": 300,
            "llm_native_retries": 4,
            "verifier_timeout_seconds": 300,
            "harbor_outer_retries": 0,
            "concurrency": 1,
            "tools": ["terminal", "file_editor", "task_tracker", "finish", "think"],
            "skills": False,
            "mcp": [],
            "subagents": False,
            "benchmark_specific_augmentation": False,
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
            "primary": "OpenRouter key usage and generation records",
            "secondary": "OpenHands native metrics and Harbor aggregate; never summed",
            "estimated_usd": 3.0,
            "conservative_uncertainty_usd": 8.0,
        },
        "acceptance": {
            "route": "every generation matches exact model and pinned provider",
            "wire": "reasoning_effort=high and max_output_tokens=128000 on every request; sampling fields absent",
            "reasoning": (
                "at least one response contains multiple reasoning items; every item is "
                "replayed exactly once on the immediately following tool turn with the "
                "same order, id, encrypted_content, summary, content, and status"
            ),
            "lifecycle": "valid native events, Harbor trajectory/result, and verifier result",
            "failures": "no missing/duplicate reasoning item, replay 4xx, hidden fallback, or malformed history",
            "audit": "full trajectory audit accepts this exact path as qualification evidence only",
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
    for model in MODELS:
        write_frozen(RUN_DIR / "configs" / f"{model}.json", harbor_config(model))
    body = manifest_body()
    manifest = dict(body)
    manifest["manifest_sha256"] = hashlib.sha256(canonical(body)).hexdigest()
    write_frozen(RUN_DIR / "manifest.json", manifest)
    return manifest


def environment() -> dict[str, str]:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is not loaded")
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
    env = environment()
    with tempfile.TemporaryDirectory(prefix="oh-reasoning-preflight-", dir="/tmp") as raw:
        completed = subprocess.run(
            command(
                RUN_DIR / "configs" / "gpt-6-astra.json",
                "--install-only",
                "--job-name", "qualification-openhands-reasoning-replay-install",
                "--jobs-dir", str(Path(raw) / "jobs"),
            ),
            cwd=ROOT,
            env=env,
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
        "paid_model_calls": 0,
        "return_code": completed.returncode,
        "log_tail": completed.stdout[-4000:],
    }
    write_frozen(RUN_DIR / "preflight.json", evidence)
    return completed.returncode


def run(approved_hash: str) -> int:
    manifest = materialize()
    if manifest["manifest_sha256"] != approved_hash:
        raise RuntimeError("manifest hash mismatch")
    preflight_record = json.loads((RUN_DIR / "preflight.json").read_text())
    if preflight_record.get("status") != "passed":
        raise RuntimeError("matching preflight has not passed")
    if preflight_record.get("manifest_sha256") != approved_hash:
        raise RuntimeError("preflight manifest mismatch")

    env = environment()
    before = key_usage(env["OPENROUTER_API_KEY"])
    write_frozen(RUN_DIR / "openrouter-usage-before.json", before, mode=0o600)
    state: dict[str, Any] = {
        "campaign_id": CAMPAIGN_ID,
        "manifest_sha256": approved_hash,
        "status": "running",
        "started_at_utc": utc_now(),
        "jobs": {},
    }
    processes: dict[str, tuple[subprocess.Popen[Any], Any]] = {}
    for model in MODELS:
        output = HARBOR_DIR / job_name(model)
        if output.exists():
            raise RuntimeError(f"refusing to reuse output: {output}")
        log_path = RUN_DIR / "logs" / f"{model}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = log_path.open("w", encoding="utf-8")
        process = subprocess.Popen(
            command(RUN_DIR / "configs" / f"{model}.json"),
            cwd=ROOT,
            env=env,
            text=True,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        processes[model] = (process, log)
        state["jobs"][model] = {"pid": process.pid, "status": "running"}
        write_state(RUN_DIR / "run-state.json", state)

    return_code = 0
    for model, (process, log) in processes.items():
        rc = process.wait()
        log.close()
        state["jobs"][model].update({"return_code": rc, "status": "finished"})
        write_state(RUN_DIR / "run-state.json", state)
        return_code = return_code or rc

    after = key_usage(env["OPENROUTER_API_KEY"])
    write_frozen(RUN_DIR / "openrouter-usage-after.json", after, mode=0o600)
    if isinstance(before.get("usage"), (int, float)) and isinstance(
        after.get("usage"), (int, float)
    ):
        state["usage_delta_usd"] = float(after["usage"]) - float(before["usage"])
    state.update(
        {
            "status": "finished",
            "return_code": return_code,
            "finished_at_utc": utc_now(),
        }
    )
    write_state(RUN_DIR / "run-state.json", state)
    return return_code


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("materialize")
    sub.add_parser("preflight")
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--approved-manifest-sha256", required=True)
    args = parser.parse_args()
    if args.command == "materialize":
        print(json.dumps(materialize(), indent=2, sort_keys=True))
        return 0
    if args.command == "preflight":
        return preflight()
    if args.command == "run":
        return run(args.approved_manifest_sha256)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
