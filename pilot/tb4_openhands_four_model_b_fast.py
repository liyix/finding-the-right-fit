#!/usr/bin/env python3
"""Schedule-only amendment: release the four non-DeepSeek OpenHands arms now."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pilot import tb4_openhands_five_model_b as base


CAMPAIGN_ID = "pilot-tb4-openhands-four-model-b-fast-20260901-r1"
PARENT_ID = base.CAMPAIGN_ID
RUN_DIR = ROOT / "runs" / CAMPAIGN_ID
HARBOR_DIR = RUN_DIR / "harbor"
MODELS = tuple(model for model in base.MODELS if model != base.CANARY)
PARENT_CONTROLLER_PID = 2238851
PARENT_CANARY_PID = 2238959


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


def config(model: str) -> dict[str, Any]:
    return base.harbor_config(model, jobs_dir=HARBOR_DIR)


def manifest_body() -> dict[str, Any]:
    parent_manifest = ROOT / "runs" / PARENT_ID / "manifest.json"
    parent_preflight = ROOT / "runs" / PARENT_ID / "preflight.json"
    frozen_parent = json.loads(parent_manifest.read_text(encoding="utf-8"))
    return {
        "campaign_id": CAMPAIGN_ID,
        "kind": "paid-schedule-amendment-to-qualification-pilot-not-formal-score",
        "parent_campaign": PARENT_ID,
        "parent_manifest_sha256": frozen_parent["manifest_sha256"],
        "parent_manifest_file_sha256": sha256_file(parent_manifest),
        "parent_preflight_file_sha256": sha256_file(parent_preflight),
        "scope": {
            "benchmark": "Terminal-Bench 4.0 v4.0.0",
            "task": base.TASK,
            "harness": "OpenHands SDK/Tools 1.44.1 patched",
            "models": list(MODELS),
            "planned_trials": 4,
        },
        "unchanged_controls": "All model, provider, routing, reasoning, context, output, sampling, tools, timeout, retry, sandbox, logging and acceptance controls are byte-derived from the approved parent configs.",
        "schedule_change_only": {
            "old": "wait for the complete DeepSeek TB4 trial, then start four arms",
            "new": "preserve the in-flight DeepSeek trial; stop only its waiting scheduler parent; immediately start the four other arms with the approved 15-second start stagger",
            "maximum_new_model_request_lanes": 4,
            "deepseek_trial_restarted": False,
            "duplicate_prevention": "abort if any parent non-DeepSeek Harbor job directory already exists",
        },
        "parent_process_snapshot": {
            "scheduler_pid": PARENT_CONTROLLER_PID,
            "canary_pid": PARENT_CANARY_PID,
            "relationship": "canary is a start_new_session child and continues after scheduler termination",
        },
        "accounting": {
            "estimate_usd": 12.0,
            "conservative_uncertainty_usd": 24.0,
            "source": "per-generation OpenRouter usage/cost; never add Harbor duplicates",
        },
        "configs": {
            model: {
                "path": f"configs/{model}.json",
                "sha256": hashlib.sha256(canonical(config(model))).hexdigest(),
                "parent_behavior_config_sha256": hashlib.sha256(
                    canonical(base.harbor_config(model))
                ).hexdigest(),
            }
            for model in MODELS
        },
        "source": {
            "launcher_sha256": sha256_file(Path(__file__).resolve()),
            "parent_launcher_sha256": sha256_file(
                ROOT / "pilot" / "tb4_openhands_five_model_b.py"
            ),
            "adapter_sha256": sha256_file(ROOT / "integrations" / "harbor.py"),
            "reasoning_patch_sha256": sha256_file(
                ROOT / "integrations" / "openhands_reasoning_details_patch.py"
            ),
        },
    }


def materialize() -> dict[str, Any]:
    for model in MODELS:
        write_frozen(RUN_DIR / "configs" / f"{model}.json", config(model))
    body = manifest_body()
    manifest = dict(body)
    manifest["manifest_sha256"] = hashlib.sha256(canonical(body)).hexdigest()
    write_frozen(RUN_DIR / "manifest.json", manifest)
    return manifest


def pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def parent_stage2_outputs() -> list[str]:
    return [
        model
        for model in MODELS
        if (base.HARBOR_DIR / base.job_name(model)).exists()
    ]


def start_job(model: str, env: dict[str, str]) -> tuple[subprocess.Popen[Any], Any]:
    output = HARBOR_DIR / base.job_name(model)
    if output.exists():
        raise RuntimeError(f"refusing to reuse output: {output}")
    log_path = RUN_DIR / "logs" / f"{model}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        base.command(RUN_DIR / "configs" / f"{model}.json"),
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
    if parent_stage2_outputs():
        raise RuntimeError("parent stage 2 has already started; refusing duplicates")
    if not pid_exists(PARENT_CONTROLLER_PID) or not pid_exists(PARENT_CANARY_PID):
        raise RuntimeError("frozen parent/canary process identity is no longer valid")

    env = base.paid_env()
    before = base.key_usage(env["OPENROUTER_API_KEY"])
    write_frozen(RUN_DIR / "openrouter-usage-before.json", before, mode=0o600)

    os.kill(PARENT_CONTROLLER_PID, signal.SIGTERM)
    time.sleep(1)
    if not pid_exists(PARENT_CANARY_PID):
        raise RuntimeError("DeepSeek canary did not survive scheduler termination")
    if parent_stage2_outputs():
        raise RuntimeError("parent stage 2 raced with amendment; refusing duplicates")

    amendment = {
        "recorded_at_utc": utc_now(),
        "scheduler_pid": PARENT_CONTROLLER_PID,
        "scheduler_terminated": not pid_exists(PARENT_CONTROLLER_PID),
        "canary_pid": PARENT_CANARY_PID,
        "canary_continues": pid_exists(PARENT_CANARY_PID),
        "replacement_campaign": CAMPAIGN_ID,
        "replacement_manifest_sha256": approved_hash,
    }
    write_frozen(
        ROOT / "runs" / PARENT_ID / "schedule-amendment.json", amendment
    )

    state: dict[str, Any] = {
        "campaign_id": CAMPAIGN_ID,
        "manifest_sha256": approved_hash,
        "status": "running",
        "started_at_utc": utc_now(),
        "jobs": {},
    }
    processes: dict[str, tuple[subprocess.Popen[Any], Any]] = {}
    for model in MODELS:
        process, log = start_job(model, env)
        processes[model] = (process, log)
        state["jobs"][model] = {"pid": process.pid, "status": "running"}
        write_state(state)
        time.sleep(base.STAGGER_SECONDS)

    return_code = 0
    for model, (process, log) in processes.items():
        rc = process.wait()
        log.close()
        state["jobs"][model].update({"return_code": rc, "status": "finished"})
        write_state(state)
        return_code = return_code or rc

    after = base.key_usage(env["OPENROUTER_API_KEY"])
    write_frozen(RUN_DIR / "openrouter-usage-after.json", after, mode=0o600)
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
    manifest = materialize()
    if manifest["manifest_sha256"] != approved_hash:
        raise RuntimeError("manifest hash mismatch")
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
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--approved-manifest-sha256", required=True)
    launch_parser = sub.add_parser("launch")
    launch_parser.add_argument("--approved-manifest-sha256", required=True)
    args = parser.parse_args()
    if args.command == "materialize":
        print(json.dumps(materialize(), indent=2, sort_keys=True))
        return 0
    if args.command == "run":
        return run(args.approved_manifest_sha256)
    if args.command == "launch":
        return launch(args.approved_manifest_sha256)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
