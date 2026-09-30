#!/usr/bin/env python3
"""Run the first exact TUA-Bench lifecycle qualification for OpenHands.

This is intentionally a thin benchmark-specific wrapper around the already
qualified five-model OpenHands/OpenRouter configuration used by the TB4 pilot.
"""

from __future__ import annotations

from datetime import datetime, timezone
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from typing import Any

import tb4_openhands_five_model_b as base


ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN_ID = "pilot-tua-openhands-five-model-20260901-r2"
RUN_DIR = ROOT / "runs" / CAMPAIGN_ID
HARBOR_DIR = RUN_DIR / "harbor"
TASK = "106-create-charles-ssh-user"
BENCHMARK_ROOT = ROOT / "vendor" / "tua-bench"
MODELS = base.MODELS


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def job_name(logical: str) -> str:
    return f"tua--openhands-patched--{logical}--{TASK}--rep-1"


def harbor_config(logical: str) -> dict[str, Any]:
    target = MODELS[logical]
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
        "job_name": job_name(logical),
        "jobs_dir": str(HARBOR_DIR),
        "n_attempts": 1,
        "install_only": False,
        "timeout_multiplier": 1.0,
        "agent_timeout_multiplier": 1.0,
        "verifier_timeout_multiplier": 1.0,
        "environment_build_timeout_multiplier": 1.0,
        "agent_setup_timeout_multiplier": 2.0,
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
            {"path": str(BENCHMARK_ROOT / "tasks"), "task_names": [TASK]}
        ],
        "tasks": [],
        "artifacts": [],
        "extra_instruction_paths": [],
        "extra_instructions": [],
    }


def manifest_body() -> dict[str, Any]:
    task_dir = BENCHMARK_ROOT / "tasks" / TASK
    oracle_result = (
        ROOT
        / "runs/pilot-tua-oracle-20260901-r2/2026-09-01__20-04-52/result.json"
    )
    install_result = (
        ROOT
        / "runs/pilot-tua-install-only-20260901-r2/openhands/2026-09-01__20-12-05/result.json"
    )
    return {
        "campaign_id": CAMPAIGN_ID,
        "kind": "paid-benchmark-lifecycle-qualification-pilot-not-formal-score",
        "approval": {
            "status": "explicitly approved by user",
            "scope": "TUA task 106 x OpenHands 1.44.1 x five study models, concurrency 5",
            "approved_at_local_date": "2026-09-01",
            "continuation_note": "r1 resolved the repository root instead of the tasks root and exited before Docker/model execution; r2 changes only that local dataset path",
        },
        "repository": {
            "head": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
            ).strip(),
            "dirty_paths_at_freeze": subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=ROOT, text=True
            ).splitlines(),
        },
        "benchmark": {
            "name": "TUA-Bench",
            "commit": "3497fd320abcafaf4797424192c891a593fd7964",
            "task": TASK,
            "task_digest": "sha256:d9d5390f5b8388bc960efba88f8b8aeffbcc8b0baa5482d14ed1cd931bdb0d82",
            "task_toml_sha256": sha256_file(task_dir / "task.toml"),
            "instruction_sha256": sha256_file(task_dir / "instruction.md"),
            "task_tree_sha256": "04179f8784a1b791ca8c8cf178fc8d328c1b5f726913abf25c47b6fa90f7e62d",
            "asset_manifest_sha256": "e043bf796f027ab758f976fc19ba1f199d709951720f62be0adb68046f45ed3a",
            "replicate": 1,
            "planned_trials": 5,
            "zero_cost_evidence": {
                "doctor": "passed 120/120 tasks and 73/73 setup assets before approval",
                "oracle_result": str(oracle_result.relative_to(ROOT)),
                "oracle_result_sha256": sha256_file(oracle_result),
                "openhands_install_result": str(install_result.relative_to(ROOT)),
                "openhands_install_result_sha256": sha256_file(install_result),
            },
        },
        "runner": {"name": "Harbor", "version": "0.22.0", "commit": "4407eb5"},
        "harness": {
            "name": "OpenHands SDK/Tools",
            "version": "1.44.1",
            "integration": "Harbor built-in plus controlled LLM/audit overlay and reasoning replay patch",
            "entry": "integrations.harbor:ControlledOpenHandsSDK",
            "adapter_sha256": sha256_file(ROOT / "integrations/harbor.py"),
            "reasoning_patch_id": "openhands-1.44.1-openrouter-reasoning-roundtrip-v2",
            "reasoning_patch_sha256": sha256_file(
                ROOT / "integrations/openhands_reasoning_details_patch.py"
            ),
        },
        "models": [
            {
                "logical": logical,
                "id": target["model"],
                "provider_route": target["provider"],
                "base_url": "https://openrouter.ai/api/v1",
                "api_mode": "auto",
            }
            for logical, target in MODELS.items()
        ],
        "controls": {
            "reasoning_effort": "high; exact transmitted field audited post-run",
            "context_window": 1_000_000,
            "max_output_tokens": 128_000,
            "temperature_top_p_seed": "omitted; provider defaults",
            "max_iterations": 500,
            "compaction": "none; upstream Harbor OpenHands runner has no condenser",
            "task_timeout_seconds": 2400,
            "verifier_timeout_seconds": 2400,
            "environment_build_timeout_seconds": 2400,
            "agent_setup_timeout_seconds": 720,
            "llm_timeout_seconds": 300,
            "llm_attempt_limit": 5,
            "harbor_outer_retries": 0,
            "concurrency": 5,
            "tools": ["terminal", "file_editor", "task_tracker", "finish", "think"],
            "skills": False,
            "mcp": [],
            "subagents": False,
            "benchmark_specific_augmentation": False,
        },
        "sandbox": {
            "provider": "Harbor Docker",
            "cpus": 1,
            "memory_mb": 2048,
            "storage_mb": 10240,
            "gpus": 0,
            "network": "task-native allow_internet=true",
        },
        "accounting": {
            "primary": "OpenRouter settled generation records",
            "secondary": "OpenHands native metrics and Harbor aggregate, source-qualified and never summed",
            "campaign_monitor_usd": 15,
            "budget_behavior": "monitor only; no mid-agent soft-stop",
        },
        "acceptance": {
            "claim_limit": "TUA lifecycle qualification only; not a formal model score",
            "required": "valid Harbor/verifier result plus full routing, wire, reasoning, tools, retry, cost and trajectory audit for every arm",
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
            "base_launcher_sha256": sha256_file(ROOT / "pilot/tb4_openhands_five_model_b.py"),
        },
    }


def materialize() -> dict[str, Any]:
    manifest_path = RUN_DIR / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for logical, item in manifest["configs"].items():
            path = RUN_DIR / item["path"]
            resolved = json.loads(path.read_text(encoding="utf-8"))
            if hashlib.sha256(canonical(resolved)).hexdigest() != item["sha256"]:
                raise RuntimeError(f"frozen config hash mismatch: {path}")
        return manifest
    for logical in MODELS:
        base.write_frozen(RUN_DIR / "configs" / f"{logical}.json", harbor_config(logical))
    body = manifest_body()
    manifest = dict(body)
    manifest["manifest_sha256"] = hashlib.sha256(canonical(body)).hexdigest()
    base.write_frozen(RUN_DIR / "manifest.json", manifest)
    return manifest


def write_state(value: dict[str, Any]) -> None:
    path = RUN_DIR / "run-state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def start_job(logical: str, env: dict[str, str]):
    output = HARBOR_DIR / job_name(logical)
    if output.exists():
        raise RuntimeError(f"refusing to reuse output: {output}")
    log_path = RUN_DIR / "logs" / f"{logical}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        base.command(RUN_DIR / "configs" / f"{logical}.json"),
        cwd=ROOT,
        env=env,
        text=True,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return process, log


def run(approved_hash: str) -> int:
    manifest = materialize()
    if manifest["manifest_sha256"] != approved_hash:
        raise RuntimeError("manifest hash mismatch")
    env = base.paid_env()
    before = base.key_usage(env["OPENROUTER_API_KEY"])
    base.write_frozen(RUN_DIR / "openrouter-usage-before.json", before, mode=0o600)
    state: dict[str, Any] = {
        "campaign_id": CAMPAIGN_ID,
        "manifest_sha256": approved_hash,
        "status": "running",
        "started_at_utc": utc_now(),
        "jobs": {},
    }
    processes = {}
    for logical in MODELS:
        process, log = start_job(logical, env)
        processes[logical] = (process, log)
        state["jobs"][logical] = {"pid": process.pid, "status": "running"}
        write_state(state)
    rc = 0
    for logical, (process, log) in processes.items():
        job_rc = process.wait()
        log.close()
        state["jobs"][logical].update({"return_code": job_rc, "status": "finished"})
        write_state(state)
        rc = rc or job_rc
    after = base.key_usage(env["OPENROUTER_API_KEY"])
    base.write_frozen(RUN_DIR / "openrouter-usage-after.json", after, mode=0o600)
    if isinstance(before.get("usage"), (int, float)) and isinstance(after.get("usage"), (int, float)):
        state["shared_key_usage_delta_usd"] = float(after["usage"]) - float(before["usage"])
    state.update({
        "status": "finished",
        "return_code": rc,
        "finished_at_utc": utc_now(),
        "trajectory_audit_required": True,
    })
    write_state(state)
    return rc


def launch(approved_hash: str) -> int:
    if subprocess.run(["tmux", "has-session", "-t", CAMPAIGN_ID], check=False).returncode == 0:
        raise RuntimeError(f"tmux session already exists: {CAMPAIGN_ID}")
    command_line = (
        f"set -a; source {shlex.quote(str(ROOT / '.env'))}; set +a; "
        f"exec {shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} "
        f"run --approved-manifest-sha256 {shlex.quote(approved_hash)}"
    )
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", CAMPAIGN_ID, "-c", str(ROOT), "bash", "-lc", command_line],
        check=True,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("materialize")
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--approved-manifest-sha256", required=True)
    launch_parser = sub.add_parser("launch")
    launch_parser.add_argument("--approved-manifest-sha256", required=True)
    args = parser.parse_args()
    if args.command == "materialize":
        print(materialize()["manifest_sha256"])
        return 0
    if args.command == "run":
        return run(args.approved_manifest_sha256)
    return launch(args.approved_manifest_sha256)


if __name__ == "__main__":
    raise SystemExit(main())
