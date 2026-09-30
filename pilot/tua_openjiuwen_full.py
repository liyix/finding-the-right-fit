#!/usr/bin/env python3
"""Freeze and run the full TUA-Bench OpenJiuwen × DeepSeek campaign."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tomllib
from typing import Any

import tua_openjiuwen as base


ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN_ID = "pilot-tua-openjiuwen-deepseek-full120-20260916-r3"
RUN_DIR = ROOT / "runs" / CAMPAIGN_ID
PREPARE_ID = "qualification-tua-openjiuwen-images-20260915-r1"
PREPARE_DIR = ROOT / "runs" / PREPARE_ID
TASKS_ROOT = ROOT / "vendor" / "tua-bench" / "tasks"
CONCURRENCY = 16
PREPARE_CONCURRENCY = 2
QUALIFIED_CAMPAIGN = "pilot-tua-openjiuwen-deepseek-20260915-r2"
QUALIFIED_MANIFEST_SHA256 = (
    "a035825800977fffd4ddff9386e72d1708729f50b946dbdcb23ff674e448c7b5"
)
TASK_ID_HASH = "3eed93a667320de4563339dd7644b11d29b2bba3ffdd011baec4f05be608ad4a"
TASK_TREE_HASH = "04179f8784a1b791ca8c8cf178fc8d328c1b5f726913abf25c47b6fa90f7e62d"
ASSET_HASH = "e043bf796f027ab758f976fc19ba1f199d709951720f62be0adb68046f45ed3a"


def task_names() -> list[str]:
    return sorted(path.name for path in TASKS_ROOT.iterdir() if path.is_dir())


def task_resource_summary() -> dict[str, Any]:
    counters: dict[str, Counter[Any]] = {
        "cpus": Counter(),
        "memory_mb": Counter(),
        "storage_mb": Counter(),
        "gpus": Counter(),
    }
    internet = Counter()
    mcp = Counter()
    timeouts: dict[str, Counter[Any]] = {
        "agent_seconds": Counter(),
        "verifier_seconds": Counter(),
        "build_seconds": Counter(),
    }
    for name in task_names():
        data = tomllib.loads((TASKS_ROOT / name / "task.toml").read_text())
        environment = data["environment"]
        for key in counters:
            counters[key][environment[key]] += 1
        internet[environment["allow_internet"]] += 1
        mcp[len(environment["mcp_servers"])] += 1
        timeouts["agent_seconds"][data["agent"]["timeout_sec"]] += 1
        timeouts["verifier_seconds"][data["verifier"]["timeout_sec"]] += 1
        timeouts["build_seconds"][environment["build_timeout_sec"]] += 1
    return {
        key: {str(value): count for value, count in sorted(counter.items())}
        for key, counter in {
            **counters,
            "allow_internet": internet,
            "mcp_server_count": mcp,
            **timeouts,
        }.items()
    }


def agent_config() -> dict[str, Any]:
    value = base.agent_config()
    value["import_path"] = (
        "integrations.harbor_openjiuwen_tua:TUAOpenJiuwenCodingAgent"
    )
    value["n_concurrent"] = CONCURRENCY
    return value


def harbor_config() -> dict[str, Any]:
    value = base.harbor_config(install_only=False, jobs_dir=RUN_DIR / "raw")
    value["job_name"] = CAMPAIGN_ID
    value["n_concurrent_trials"] = CONCURRENCY
    value["agents"] = [agent_config()]
    value["datasets"] = [{"path": str(TASKS_ROOT), "task_names": task_names()}]
    return value


def prepare_config() -> dict[str, Any]:
    """Build/cache every task image without invoking a model."""
    value = base.harbor_config(install_only=True, jobs_dir=PREPARE_DIR / "raw")
    value["job_name"] = PREPARE_ID
    value["n_concurrent_trials"] = PREPARE_CONCURRENCY
    prepared_agent = agent_config()
    prepared_agent["n_concurrent"] = PREPARE_CONCURRENCY
    value["agents"] = [prepared_agent]
    value["datasets"] = [{"path": str(TASKS_ROOT), "task_names": task_names()}]
    # Retries are permitted only for this zero-model-call image preparation.
    value["retry"] = {
        "max_retries": 3,
        "wait_multiplier": 1.0,
        "min_wait_sec": 20.0,
        "max_wait_sec": 60.0,
    }
    value["verifier"] = {"disable": True}
    return value


def source_hashes() -> dict[str, str]:
    paths = [
        Path(__file__).resolve(),
        ROOT / "integrations" / "harbor_openjiuwen_tua.py",
        ROOT / "integrations" / "harbor_openjiuwen.py",
        ROOT / "integrations" / "openjiuwen_agent.py",
        ROOT / "integrations" / "openjiuwen-runtime.lock",
    ]
    return {str(path.relative_to(ROOT)): base.sha256_file(path) for path in paths}


def qualified_evidence() -> dict[str, Any]:
    evidence_dir = ROOT / "runs" / QUALIFIED_CAMPAIGN
    manifest_path = evidence_dir / "manifest.json"
    audit_path = evidence_dir / "audit.json"
    manifest = json.loads(manifest_path.read_text())
    audit = json.loads(audit_path.read_text())
    if manifest.get("manifest_sha256") != QUALIFIED_MANIFEST_SHA256:
        raise RuntimeError("qualified TUA manifest drift")
    if audit.get("manifest_sha256") != QUALIFIED_MANIFEST_SHA256:
        raise RuntimeError("qualified TUA audit does not match its manifest")
    if not str(audit.get("decision", "")).startswith("accept-as-B"):
        raise RuntimeError("qualified TUA audit is not accepted")
    return {
        "campaign_id": QUALIFIED_CAMPAIGN,
        "manifest_sha256": QUALIFIED_MANIFEST_SHA256,
        "manifest_file_sha256": base.sha256_file(manifest_path),
        "audit_file_sha256": base.sha256_file(audit_path),
        "scope": audit["claim_limit"],
    }


def image_preparation_evidence() -> dict[str, Any]:
    config_path = PREPARE_DIR / "config.json"
    result_path = PREPARE_DIR / "raw" / PREPARE_ID / "result.json"
    result = json.loads(result_path.read_text())
    stats = result.get("stats", {})
    expected = {
        "n_completed_trials": 120,
        "n_errored_trials": 0,
        "n_running_trials": 0,
        "n_pending_trials": 0,
        "n_cancelled_trials": 0,
        "n_retries": 0,
    }
    if any(stats.get(key) != value for key, value in expected.items()):
        raise RuntimeError("full TUA image preparation is incomplete or failed")
    if any(stats.get(key) is not None for key in (
        "n_input_tokens", "n_cache_tokens", "n_output_tokens", "cost_usd"
    )):
        raise RuntimeError("image preparation unexpectedly recorded model usage")
    return {
        "campaign_id": PREPARE_ID,
        "install_only": True,
        "concurrency": PREPARE_CONCURRENCY,
        "completed_trials": 120,
        "errored_trials": 0,
        "retries": 0,
        "paid_model_calls": 0,
        "finished_at": result["finished_at"],
        "config_file_sha256": base.sha256_file(config_path),
        "result_file_sha256": base.sha256_file(result_path),
    }


def manifest_body() -> dict[str, Any]:
    names = task_names()
    config = harbor_config()
    return {
        "schema_version": 1,
        "campaign_id": CAMPAIGN_ID,
        "kind": "paid-full-tua-openjiuwen-candidate-score",
        "approval": {"required": True, "status": "pending"},
        "benchmark": {
            "name": "TUA-Bench",
            "commit": base.TUA_COMMIT,
            "task_count": len(names),
            "task_names": names,
            "task_id_hash": TASK_ID_HASH,
            "task_tree_hash": TASK_TREE_HASH,
            "setup_asset_hash": ASSET_HASH,
            "replicate": 1,
            "planned_trials": len(names),
            "resources": task_resource_summary(),
        },
        "runner": {
            "name": "Harbor",
            "version": base.HARBOR_VERSION,
            "commit": base.HARBOR_COMMIT,
            "integration": "custom thin TUA full-container workspace adapter",
            "outer_trial_retries": 0,
            "trial_concurrency": CONCURRENCY,
            "agent_concurrency": CONCURRENCY,
            "agent_setup_timeout_seconds": 720,
            "resume_semantics": (
                "completed trials are retained; an interrupted in-flight trial "
                "restarts from the beginning after its directory is archived"
            ),
        },
        "harness": {
            "name": "study openJiuwen Coding Agent candidate",
            "version": "0.1.18",
            "runtime_version": "0.1.18-r1",
            "classification": (
                "study composition from public openjiuwen.harness API; not a "
                "stock preset or exact Huawei paper reproduction"
            ),
            "rails": [
                "SysOperationRail", "ContextProcessorRail(preset=True)",
                "ConfirmedCompletionRail-study", "AuditRail-study",
                "SecurityRail", "ModelAnomalyDetectionRail",
            ],
            "tools": [
                "read_file", "write_file", "edit_file", "glob",
                "list_files", "grep", "bash",
            ],
            "skills": [],
            "mcp_servers": [],
            "memory": False,
            "subagents": False,
            "task_planning": False,
            "completion_confirmations": 2,
            "max_outer_rounds": 8,
        },
        "model_transport": {
            "requested_model": base.MODEL,
            "expected_actual_model": base.EXPECTED_MODEL,
            "gateway": "OpenRouter",
            "expected_endpoint_provider": base.EXPECTED_PROVIDER,
            "base_url": "https://openrouter.ai/api/v1",
            "protocol": "OpenAI Chat Completions streaming",
            "provider_route": {
                "only": ["deepseek"],
                "allow_fallbacks": False,
                "require_parameters": True,
            },
            "protocol_translation": False,
        },
        "controls": {
            "reasoning_effort": {
                "value": "high",
                "wire": {"reasoning": {"effort": "high"}},
            },
            "reasoning_replay": (
                "exclusive: unsigned reasoning.text via reasoning_content; "
                "structured blocks via exact reasoning_details"
            ),
            "context_window": base.CONTEXT_WINDOW,
            "context_compression": {
                "enabled": True,
                "preset": "openjiuwen-0.1.18-native",
                "whole_context_trigger_ratio": 0.8,
                "single_tool_result_offload_ratio": 0.1,
                "session_memory_enabled": False,
                "compression_recall_enabled": False,
                "context_debug_enabled": True,
            },
            "max_output_tokens": None,
            "temperature": None,
            "top_p": None,
            "seed": None,
            "parallel_tool_calls": True,
            "runtime_budget_seconds": base.RUNTIME_BUDGET_SECONDS,
            "runtime_budget_prompt_rail_enabled": False,
            "extra_prompts": [],
        },
        "timeouts_and_retries": {
            "task_agent_seconds": 2400,
            "verifier_seconds": 2400,
            "environment_build_seconds": 2400,
            "agent_setup_seconds": 720,
            "llm_total_seconds": 360,
            "llm_first_chunk_seconds": 300,
            "llm_stream_idle_seconds": 300,
            "native_http_max_retries": 5,
            "native_tool_timeout_seconds": 300,
            "whole_trial_retries": 0,
        },
        "sandbox": {
            "provider": "Harbor local Docker",
            "workspace_security_boundary": "/",
            "resources": "task-native; see benchmark.resources",
            "gpus": 0,
            "network": "task-native allow_internet=true",
            "runtime_mount": "/opt/openjiuwen-runtime:ro",
        },
        "accounting": {
            "primary": "OpenRouter settled per-generation metadata",
            "secondary": "openJiuwen native usage and Harbor aggregation",
            "estimated_total_usd": [2.5, 120.0],
            "per_task_cost_anomaly_usd": 3.0,
            "hard_dollar_stop": None,
        },
        "qualification_evidence": qualified_evidence(),
        "image_preparation_evidence": image_preparation_evidence(),
        "required_post_run_audit": [
            "all generation IDs and settled actual provider/model",
            "tool schema stability and every tool-call/result closure",
            "reasoning_details replay, retries, 429/5xx and timeout events",
            "native/Harbor/provider token and cost reconciliation",
            "context/output events, verifier validity, successes, failures and outliers",
        ],
        "claim_limit": (
            "full TUA score for this candidate configuration after trajectory audit; "
            "the prior B evidence covers one text lifecycle at concurrency 1, so "
            "concurrency-16 failures remain infrastructure failures until audited"
        ),
        "source_sha256": source_hashes(),
        "resolved_config_sha256": hashlib.sha256(base.canonical(config)).hexdigest(),
    }


def materialize() -> dict[str, Any]:
    if RUN_DIR.exists():
        path = RUN_DIR / "manifest.json"
        if not path.is_file():
            raise RuntimeError(f"refusing partial campaign directory: {RUN_DIR}")
        return json.loads(path.read_text())
    body = manifest_body()
    manifest = dict(body)
    manifest["manifest_sha256"] = hashlib.sha256(base.canonical(body)).hexdigest()
    base.write_json(RUN_DIR / "config.json", harbor_config())
    base.write_json(RUN_DIR / "manifest.json", manifest)
    return manifest


def verify() -> dict[str, Any]:
    manifest = json.loads((RUN_DIR / "manifest.json").read_text())
    body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if hashlib.sha256(base.canonical(body)).hexdigest() != manifest["manifest_sha256"]:
        raise RuntimeError("manifest hash mismatch")
    if source_hashes() != manifest["source_sha256"]:
        raise RuntimeError("source changed after campaign freeze")
    config = json.loads((RUN_DIR / "config.json").read_text())
    if hashlib.sha256(base.canonical(config)).hexdigest() != manifest["resolved_config_sha256"]:
        raise RuntimeError("resolved Harbor config changed after campaign freeze")
    return manifest


def preflight() -> dict[str, Any]:
    base.check_tua_checkout()
    base.check_runtime()
    names = task_names()
    if len(names) != 120:
        raise RuntimeError(f"expected 120 TUA tasks, found {len(names)}")
    resources = task_resource_summary()
    if resources["gpus"] != {"0": 120} or resources["mcp_server_count"] != {"0": 120}:
        raise RuntimeError("TUA resource surface drift")
    qualified_evidence()
    image_preparation_evidence()
    manifest = materialize()
    resolved = base.resolve_config(RUN_DIR / "config.json")
    agent = resolved["agents"][0]
    if (
        resolved["n_concurrent_trials"] != CONCURRENCY
        or agent["n_concurrent"] != CONCURRENCY
        or agent["import_path"]
        != "integrations.harbor_openjiuwen_tua:TUAOpenJiuwenCodingAgent"
        or agent["kwargs"]["runtime_budget_rail_enabled"] is not False
        or agent["kwargs"]["context_compression_enabled"] is not True
        or len(resolved["datasets"][0]["task_names"]) != 120
    ):
        raise RuntimeError("resolved full TUA configuration drift")
    frozen_config = json.loads((RUN_DIR / "config.json").read_text())
    if frozen_config["retry"]["max_retries"] != 0:
        raise RuntimeError("resolved full TUA retry policy drift")
    verify()
    print("Zero-cost full TUA preflight passed; no paid model call was made.")
    print(f"Manifest SHA256 {manifest['manifest_sha256']}")
    return manifest


def run(approved_hash: str) -> int:
    manifest = verify()
    if approved_hash != manifest["manifest_sha256"]:
        raise RuntimeError("approved manifest hash does not match frozen campaign")
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is required")
    return subprocess.run(
        base.harbor_command(RUN_DIR / "config.json"),
        cwd=ROOT,
        env=base.uv_environment(api_key=api_key),
        check=False,
    ).returncode


def prepare() -> int:
    config_path = PREPARE_DIR / "config.json"
    expected = prepare_config()
    if config_path.is_file():
        existing = json.loads(config_path.read_text())
        if base.canonical(existing) != base.canonical(expected):
            raise RuntimeError("image-preparation config drift")
    elif PREPARE_DIR.exists():
        raise RuntimeError(f"refusing partial preparation directory: {PREPARE_DIR}")
    else:
        base.write_json(config_path, expected)
    return subprocess.run(
        base.harbor_command(config_path),
        cwd=ROOT,
        env=base.uv_environment(api_key="preflight-not-a-secret"),
        check=False,
    ).returncode


def launch_prepare() -> int:
    config_path = PREPARE_DIR / "config.json"
    if not config_path.is_file():
        base.write_json(config_path, prepare_config())
    command = (
        f"exec {shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} "
        "prepare"
    )
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", PREPARE_ID,
         "-c", str(ROOT), "bash", "-lc", command],
        check=True,
    )
    print(f"Started tmux session {PREPARE_ID}")
    return 0


def launch(approved_hash: str) -> int:
    verify()
    command = (
        f"set -a; source {shlex.quote(str(ROOT / '.env'))}; set +a; "
        f"exec {shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} "
        f"run --approved-manifest-sha256 {shlex.quote(approved_hash)}"
    )
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", CAMPAIGN_ID,
         "-c", str(ROOT), "bash", "-lc", command],
        check=True,
    )
    print(f"Started tmux session {CAMPAIGN_ID}")
    return 0


def status() -> int:
    job_dir = RUN_DIR / "raw" / CAMPAIGN_ID
    completed = len(list(job_dir.glob("*/result.json"))) if job_dir.is_dir() else 0
    started = len([path for path in job_dir.iterdir() if path.is_dir()]) if job_dir.is_dir() else 0
    print(json.dumps({
        "campaign_id": CAMPAIGN_ID,
        "planned": 120,
        "started_trial_directories": started,
        "completed_trial_results": completed,
        "manual_trajectory_review_required": True,
        "image_preparation": {
            "campaign_id": PREPARE_ID,
            "concurrency": PREPARE_CONCURRENCY,
            "completed": len(list((PREPARE_DIR / "raw" / PREPARE_ID).glob("*/result.json")))
            if (PREPARE_DIR / "raw" / PREPARE_ID).is_dir() else 0,
        },
    }, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("preflight")
    commands.add_parser("prepare")
    commands.add_parser("launch-prepare")
    run_parser = commands.add_parser("run")
    run_parser.add_argument("--approved-manifest-sha256", required=True)
    launch_parser = commands.add_parser("launch")
    launch_parser.add_argument("--approved-manifest-sha256", required=True)
    commands.add_parser("status")
    args = parser.parse_args()
    try:
        if args.command == "preflight":
            preflight()
        elif args.command == "prepare":
            return prepare()
        elif args.command == "launch-prepare":
            return launch_prepare()
        elif args.command == "run":
            return run(args.approved_manifest_sha256)
        elif args.command == "launch":
            return launch(args.approved_manifest_sha256)
        else:
            return status()
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"ERROR  {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
