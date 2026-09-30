#!/usr/bin/env python3
"""Freeze and orchestrate four independent full-TUA OpenJiuwen model jobs."""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tomllib
from typing import Any

import tua_openjiuwen_four_model as canary


ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN_ID = "pilot-tua-openjiuwen-four-model-full120-20260918-r2"
RUN_DIR = ROOT / "runs" / CAMPAIGN_ID
TASKS_ROOT = ROOT / "vendor" / "tua-bench" / "tasks"
CONCURRENCY_PER_MODEL = 16
TOTAL_CONCURRENCY = CONCURRENCY_PER_MODEL * len(canary.MODELS)
QUALIFIED_CAMPAIGN = canary.CAMPAIGN_ID
QUALIFIED_MANIFEST_SHA256 = (
    "b6958d080e28a5e20dd111459dae83d42466b22eebd1f340472e8233e059aba7"
)
PREPARE_ID = "qualification-tua-openjiuwen-images-20260915-r1"
PREPARE_DIR = ROOT / "runs" / PREPARE_ID
TASK_ID_HASH = "3eed93a667320de4563339dd7644b11d29b2bba3ffdd011baec4f05be608ad4a"
TASK_TREE_HASH = "04179f8784a1b791ca8c8cf178fc8d328c1b5f726913abf25c47b6fa90f7e62d"
ASSET_HASH = "e043bf796f027ab758f976fc19ba1f199d709951720f62be0adb68046f45ed3a"
COST_ESTIMATES = {
    "claude-opus-5": [50.0, 1500.0],
    "gpt-6-astra": [40.0, 1500.0],
    "kimi-k3": [15.0, 800.0],
    "glm-5.3": [10.0, 500.0],
}


def task_names() -> list[str]:
    return sorted(path.name for path in TASKS_ROOT.iterdir() if path.is_dir())


def task_resource_summary() -> dict[str, dict[str, int]]:
    counters: dict[str, Counter[Any]] = {
        key: Counter()
        for key in (
            "cpus", "memory_mb", "storage_mb", "gpus", "allow_internet",
            "mcp_server_count", "agent_seconds", "verifier_seconds", "build_seconds",
        )
    }
    for name in task_names():
        data = tomllib.loads((TASKS_ROOT / name / "task.toml").read_text())
        environment = data["environment"]
        for key in ("cpus", "memory_mb", "storage_mb", "gpus", "allow_internet"):
            counters[key][environment[key]] += 1
        counters["mcp_server_count"][len(environment["mcp_servers"])] += 1
        counters["agent_seconds"][data["agent"]["timeout_sec"]] += 1
        counters["verifier_seconds"][data["verifier"]["timeout_sec"]] += 1
        counters["build_seconds"][environment["build_timeout_sec"]] += 1
    return {
        key: {str(value): count for value, count in sorted(counter.items())}
        for key, counter in counters.items()
    }


def agent_config(logical: str) -> dict[str, Any]:
    value = canary.agent_config(logical)
    value["n_concurrent"] = CONCURRENCY_PER_MODEL
    return value


def harbor_config(logical: str) -> dict[str, Any]:
    job_name = f"{CAMPAIGN_ID}--{logical}"
    value = canary.harbor_config(
        logical,
        install_only=False,
        jobs_dir=RUN_DIR / "raw" / logical,
    )
    value["job_name"] = job_name
    value["n_concurrent_trials"] = CONCURRENCY_PER_MODEL
    value["agents"] = [agent_config(logical)]
    value["datasets"] = [{"path": str(TASKS_ROOT), "task_names": task_names()}]
    return value


def source_hashes() -> dict[str, str]:
    paths = [
        Path(__file__).resolve(),
        ROOT / "pilot" / "tua_openjiuwen_four_model.py",
        ROOT / "integrations" / "harbor_openjiuwen_tua.py",
        ROOT / "integrations" / "harbor_openjiuwen.py",
        ROOT / "integrations" / "openjiuwen_agent.py",
        ROOT / "integrations" / "openjiuwen-runtime.lock",
    ]
    return {str(path.relative_to(ROOT)): canary.sha256_file(path) for path in paths}


def qualified_evidence() -> dict[str, Any]:
    manifest_path = ROOT / "runs" / QUALIFIED_CAMPAIGN / "manifest.json"
    audit_path = ROOT / "runs" / QUALIFIED_CAMPAIGN / "audit.json"
    manifest = json.loads(manifest_path.read_text())
    audit = json.loads(audit_path.read_text())
    if manifest.get("manifest_sha256") != QUALIFIED_MANIFEST_SHA256:
        raise RuntimeError("four-model canary manifest drift")
    if audit.get("manifest_sha256") != QUALIFIED_MANIFEST_SHA256:
        raise RuntimeError("four-model canary audit/manifest mismatch")
    if audit.get("accepted") is not True:
        raise RuntimeError("four-model canary audit is not accepted")
    return {
        "campaign_id": QUALIFIED_CAMPAIGN,
        "manifest_sha256": QUALIFIED_MANIFEST_SHA256,
        "manifest_file_sha256": canary.sha256_file(manifest_path),
        "audit_file_sha256": canary.sha256_file(audit_path),
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
        raise RuntimeError("TUA image preparation evidence is incomplete")
    if any(
        stats.get(key) is not None
        for key in ("n_input_tokens", "n_cache_tokens", "n_output_tokens", "cost_usd")
    ):
        raise RuntimeError("image preparation unexpectedly used a model")
    return {
        "campaign_id": PREPARE_ID,
        "completed_trials": 120,
        "errored_trials": 0,
        "paid_model_calls": 0,
        "finished_at": result["finished_at"],
        "config_file_sha256": canary.sha256_file(config_path),
        "result_file_sha256": canary.sha256_file(result_path),
    }


def job_manifest(logical: str) -> dict[str, Any]:
    spec = canary.MODELS[logical]
    config = harbor_config(logical)
    return {
        "logical": logical,
        "job_name": config["job_name"],
        "planned_trials": 120,
        "replicate": 1,
        "requested_model": spec["model"],
        "expected_actual_model": spec["expected_model"],
        "expected_provider": spec["provider"],
        "base_url": "https://openrouter.ai/api/v1",
        "protocol": "OpenAI Chat Completions streaming",
        "provider_route": {
            "only": spec["only"],
            "quantizations": spec["quantizations"],
            "allow_fallbacks": False,
            "require_parameters": True,
        },
        "context_window": spec["context"],
        "advertised_output_tokens": spec["advertised_output"],
        "wire_max_tokens": None,
        "trial_concurrency": CONCURRENCY_PER_MODEL,
        "agent_concurrency": CONCURRENCY_PER_MODEL,
        "estimated_cost_usd": COST_ESTIMATES[logical],
        "resolved_config_sha256": hashlib.sha256(canary.canonical(config)).hexdigest(),
    }


def manifest_body() -> dict[str, Any]:
    names = task_names()
    estimates = list(COST_ESTIMATES.values())
    return {
        "schema_version": 1,
        "campaign_id": CAMPAIGN_ID,
        "kind": "paid-four-atomic-model-jobs-full-tua-openjiuwen-candidate-score",
        "approval": {"required": True, "status": "pending"},
        "benchmark": {
            "name": "TUA-Bench",
            "commit": canary.TUA_COMMIT,
            "task_count_per_job": len(names),
            "task_names": names,
            "task_id_hash": TASK_ID_HASH,
            "task_tree_hash": TASK_TREE_HASH,
            "setup_asset_hash": ASSET_HASH,
            "planned_trials_total": len(names) * len(canary.MODELS),
            "resources": task_resource_summary(),
        },
        "runner": {
            "name": "Harbor",
            "version": "0.22.0",
            "integration": "integrations.harbor_openjiuwen_tua:TUAOpenJiuwenCodingAgent",
            "atomic_jobs": 4,
            "outer_trial_retries": 0,
            "concurrency_per_model": CONCURRENCY_PER_MODEL,
            "total_trial_concurrency": TOTAL_CONCURRENCY,
            "agent_setup_timeout_seconds": 720,
        },
        "harness": {
            "name": "study OpenJiuwen Coding Agent candidate",
            "version": "0.1.18",
            "upstream_commit": "1d37ae3007f9df9a8489a7ab271141b03be08f66",
            "classification": "study composition from public openjiuwen.harness API",
            "tools": [
                "read_file", "write_file", "edit_file", "glob",
                "list_files", "grep", "bash",
            ],
            "rails": [
                "runtime-budget", "native-context-processor",
                "two-completion-confirmations", "security",
                "model-anomaly", "tool-resilience", "audit",
            ],
            "skills": [],
            "mcp_servers": [],
            "memory": False,
            "subagents": False,
            "benchmark_specific_prompt_or_tool": False,
        },
        "jobs": [job_manifest(logical) for logical in canary.MODELS],
        "controls": {
            "reasoning_effort": "high via reasoning.effort",
            "reasoning_replay": "plaintext-or-structured exclusive",
            "sampling": "temperature/top_p/top_k/seed omitted; provider default",
            "output": "max_tokens omitted; endpoint behavior retained",
            "context_compression": (
                "native ContextProcessorRail preset; 80% whole-context trigger; "
                "same routed model; no session memory or recall tool"
            ),
            "runtime_budget_rail": True,
            "max_outer_rounds": 8,
            "parallel_tool_calls": "harness native",
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
            "native_model_anomaly_retries": 2,
            "native_tool_timeout_seconds": 300,
            "whole_trial_retries": 0,
        },
        "sandbox": {
            "provider": "Harbor local Docker",
            "resources": "task-native; 120/120 are 1 CPU, 2 GiB RAM, 10 GiB storage, no GPU",
            "network": "task-native allow_internet=true",
            "workspace_and_filesystem_boundary": "/",
            "runtime_mount": "/opt/openjiuwen-runtime:ro",
        },
        "credential_boundary": {
            "transport": "root-only-file-to-one-shot-fifo-to-model-process-stdin",
            "task_environment_key_visible": False,
            "task_shell_secret_file_visible_after_handoff": False,
        },
        "accounting": {
            "primary": "OpenRouter settled per-generation metadata",
            "secondary": "OpenJiuwen native usage and Harbor aggregation",
            "estimated_total_usd": [
                sum(value[0] for value in estimates),
                sum(value[1] for value in estimates),
            ],
            "hard_dollar_stop": None,
            "uncertainty": (
                "wide because the qualification task is short and long-horizon task cost is heavy-tailed; "
                "there is no safe mid-response dollar stop"
            ),
        },
        "qualification_evidence": qualified_evidence(),
        "image_preparation_evidence": image_preparation_evidence(),
        "required_live_monitoring": [
            "first ten minutes: starts/completions/errors by model",
            "first provider generation route per model",
            "429/5xx/timeout/retry and host resource pressure",
            "tool schema/reasoning replay/credential self-test on completed early trials",
        ],
        "required_post_run_audit": [
            "all generation IDs and settled actual provider/model/fallback",
            "tool schema stability and tool-call/result closure",
            "reasoning replay, compression, output limits, retries and timeouts",
            "provider/native/Harbor token and cost reconciliation",
            "credential isolation and absence of auxiliary model API calls",
            "verifier outcomes and infrastructure-failure classification",
        ],
        "claim_limit": (
            "candidate full-TUA scores only after provider reconciliation and trajectory audit; "
            "multimodal capability and natural compaction/output recovery remain measured outcomes"
        ),
        "source_sha256": source_hashes(),
    }


def materialize() -> dict[str, Any]:
    if RUN_DIR.exists():
        return verify()
    body = manifest_body()
    manifest = dict(body)
    manifest["manifest_sha256"] = hashlib.sha256(canary.canonical(body)).hexdigest()
    for logical in canary.MODELS:
        canary.write_json(RUN_DIR / "configs" / f"{logical}.json", harbor_config(logical))
    canary.write_json(RUN_DIR / "manifest.json", manifest)
    return manifest


def verify() -> dict[str, Any]:
    manifest = json.loads((RUN_DIR / "manifest.json").read_text())
    recorded = manifest["manifest_sha256"]
    body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if hashlib.sha256(canary.canonical(body)).hexdigest() != recorded:
        raise RuntimeError("manifest hash mismatch")
    if source_hashes() != manifest["source_sha256"]:
        raise RuntimeError("source changed after campaign freeze")
    specs = {item["logical"]: item for item in manifest["jobs"]}
    for logical in canary.MODELS:
        config = json.loads((RUN_DIR / "configs" / f"{logical}.json").read_text())
        digest = hashlib.sha256(canary.canonical(config)).hexdigest()
        if digest != specs[logical]["resolved_config_sha256"]:
            raise RuntimeError(f"resolved config drift: {logical}")
    return manifest


def preflight() -> dict[str, Any]:
    canary.check_fixed_environment()
    canary.verify()
    names = task_names()
    if len(names) != 120:
        raise RuntimeError(f"expected 120 TUA tasks, found {len(names)}")
    expected_id_hash = hashlib.sha256("\n".join(names).encode()).hexdigest()
    if expected_id_hash != TASK_ID_HASH:
        raise RuntimeError("TUA task ID set drift")
    resources = task_resource_summary()
    if resources["gpus"] != {"0": 120} or resources["mcp_server_count"] != {"0": 120}:
        raise RuntimeError("TUA resource surface drift")
    qualified_evidence()
    image_preparation_evidence()
    manifest = materialize()
    for logical, spec in canary.MODELS.items():
        config_path = RUN_DIR / "configs" / f"{logical}.json"
        resolved = canary.resolved_config(config_path)
        agent = resolved["agents"][0]
        expected_route = {
            "only": spec["only"],
            "allow_fallbacks": False,
            "require_parameters": True,
        }
        if spec["quantizations"]:
            expected_route["quantizations"] = spec["quantizations"]
        if (
            resolved["n_concurrent_trials"] != CONCURRENCY_PER_MODEL
            or agent["n_concurrent"] != CONCURRENCY_PER_MODEL
            or agent["model_name"] != f"openrouter/{spec['model']}"
            or agent["kwargs"]["openrouter_route"] != expected_route
            or agent["kwargs"]["runtime_budget_rail_enabled"] is not True
            or agent["kwargs"]["context_compression_enabled"] is not True
            or len(resolved["datasets"][0]["task_names"]) != 120
        ):
            raise RuntimeError(f"resolved full config drift: {logical}")
        frozen = json.loads(config_path.read_text())
        if frozen["retry"]["max_retries"] != 0:
            raise RuntimeError(f"frozen outer retry drift: {logical}")
    verify()
    print("Zero-cost four-model full-TUA preflight passed; paid calls: 0")
    print(f"Manifest SHA256 {manifest['manifest_sha256']}")
    return manifest


def run_one(logical: str, api_key: str) -> tuple[str, int]:
    log_path = RUN_DIR / "operator" / f"{logical}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("wb") as log:
        result = subprocess.run(
            canary.command(RUN_DIR / "configs" / f"{logical}.json"),
            cwd=ROOT,
            env=canary.launch_env(api_key),
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
    return logical, result.returncode


def run(approved_hash: str) -> int:
    manifest = verify()
    if approved_hash != manifest["manifest_sha256"]:
        raise RuntimeError("approved hash does not match frozen manifest")
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is required")
    results: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(run_one, logical, api_key): logical for logical in canary.MODELS}
        for future in as_completed(futures):
            logical, code = future.result()
            results[logical] = code
            print(f"{logical}: exit {code}", flush=True)
    canary.write_json(RUN_DIR / "operator" / "exit-codes.json", results)
    return 0 if all(code == 0 for code in results.values()) else 1


def launch(approved_hash: str) -> int:
    verify()
    exists = subprocess.run(
        ["tmux", "has-session", "-t", CAMPAIGN_ID],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if exists.returncode == 0:
        raise RuntimeError(f"tmux session already exists: {CAMPAIGN_ID}")
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
    output: dict[str, Any] = {
        "campaign_id": CAMPAIGN_ID,
        "planned_trials_total": 480,
        "concurrency_total": TOTAL_CONCURRENCY,
        "models": {},
    }
    for logical in canary.MODELS:
        job_dir = RUN_DIR / "raw" / logical / f"{CAMPAIGN_ID}--{logical}"
        task_dirs = [path for path in job_dir.iterdir() if path.is_dir()] if job_dir.is_dir() else []
        results = [path / "result.json" for path in task_dirs if (path / "result.json").is_file()]
        errors = 0
        rewards: Counter[str] = Counter()
        for path in results:
            data = json.loads(path.read_text())
            errors += int(data.get("exception") is not None)
            reward = (data.get("verifier_result") or {}).get("rewards", {}).get("reward")
            rewards[str(reward)] += 1
        output["models"][logical] = {
            "started": len(task_dirs),
            "completed": len(results),
            "errors": errors,
            "rewards": dict(rewards),
        }
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight")
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--approved-manifest-sha256", required=True)
    launch_parser = sub.add_parser("launch")
    launch_parser.add_argument("--approved-manifest-sha256", required=True)
    sub.add_parser("status")
    args = parser.parse_args()
    try:
        if args.command == "preflight":
            preflight()
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
