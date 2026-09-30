#!/usr/bin/env python3
"""Credential-isolated replacement run for two polluted TUA trials."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tomllib
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
WORKTREE = ROOT / ".worktrees" / "openjiuwen-paired-89249"
CAMPAIGN_ID = "pilot-tua-openjiuwen-deepseek-clean2-20260917-r2"
QUALIFICATION_ID = "qualification-tua-openjiuwen-credential-isolation-20260917-r3"
RUN_DIR = ROOT / "runs" / CAMPAIGN_ID
QUALIFICATION_DIR = ROOT / "runs" / QUALIFICATION_ID
TASKS_ROOT = ROOT / "vendor" / "tua-bench" / "tasks"
TASKS = ["008-find-bird-chase-frames", "100-name-mountain-photos"]
PARENT_DIR = ROOT / "runs" / "pilot-tua-openjiuwen-deepseek-full120-20260916-r3"
PARENT_MANIFEST_SHA256 = (
    "0f2891ccd1c489c929727a2043c4df2ff7e38b8ed75a79323042c3042eb261ac"
)
FAILED_ATTEMPT_ID = "pilot-tua-openjiuwen-deepseek-clean2-20260917-r1"
FAILED_ATTEMPT_MANIFEST_SHA256 = (
    "27a06e7c2b8b46130e4d582a6746481c23e32c14581a3b8362fca35d776918d0"
)
ORIGINAL_RUNNER = (
    ROOT / "runs" / "pilot-ale-openjiuwen-deepseek-20260914-r4" / "source"
    / "ale_run" / "agents" / "study_openjiuwen" / "openjiuwen_agent.py"
)
ORIGINAL_RUNNER_SHA256 = (
    "c50aca996c14f49b01759e8d3910d1aa10b08084d0e4390c32d1b51c87d0141b"
)
HARBOR_BIN = ROOT / ".cache" / "harbor-tua-0.22.0" / "bin" / "harbor"
RUNTIME = ROOT / ".cache" / "openjiuwen" / "runtime-0.1.18-r1"
RUNTIME_PYTHON = RUNTIME / "python" / "bin" / "python3.12"
RUNTIME_SITE_PACKAGES = RUNTIME / "site-packages"
MODEL = "deepseek/deepseek-v4-pro-0813"
CONTEXT_WINDOW = 1_048_576
RUNTIME_BUDGET_SECONDS = 2_400
CONCURRENCY = 2


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o444)


def environment(api_key: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "OPENROUTER_API_KEY": api_key,
        "PYTHONPATH": str(WORKTREE),
        "UV_CACHE_DIR": str(ROOT / ".cache" / "uv"),
        "UV_TOOL_DIR": str(ROOT / ".cache" / "uv-tools"),
        "UV_PYTHON_INSTALL_DIR": str(ROOT / ".cache" / "uv-python"),
    })
    return env


def command(config_path: Path, *, print_config: bool = False) -> list[str]:
    return [
        str(HARBOR_BIN), "run", "--config", str(config_path),
        "--print-config" if print_config else "--yes",
    ]


def agent_config() -> dict[str, Any]:
    return {
        "import_path": (
            "integrations.harbor_openjiuwen_tua:TUAOpenJiuwenCodingAgent"
        ),
        "model_name": f"openrouter/{MODEL}",
        "n_concurrent": CONCURRENCY,
        "include_logs": ["**/*"],
        "skills": [],
        "mcp_servers": [],
        "kwargs": {
            "version": "0.1.18",
            "context_window": CONTEXT_WINDOW,
            "openrouter_route": {
                "only": ["deepseek"],
                "allow_fallbacks": False,
                "require_parameters": True,
            },
            "runtime_budget_seconds": RUNTIME_BUDGET_SECONDS,
            "completion_timeout_seconds": RUNTIME_BUDGET_SECONDS,
            "max_outer_rounds": 8,
            "llm_request_timeout_seconds": 360,
            "prompt_language": "en",
        },
    }


def harbor_config(*, install_only: bool, jobs_dir: Path) -> dict[str, Any]:
    config: dict[str, Any] = {
        "job_name": QUALIFICATION_ID if install_only else CAMPAIGN_ID,
        "jobs_dir": str(jobs_dir),
        "n_attempts": 1,
        "install_only": install_only,
        "timeout_multiplier": 1.0,
        "agent_timeout_multiplier": 1.0,
        "verifier_timeout_multiplier": 1.0,
        "environment_build_timeout_multiplier": 1.0,
        "agent_setup_timeout_multiplier": 2.0,
        "n_concurrent_trials": CONCURRENCY,
        "retry": {"max_retries": 0},
        "environment": {
            "type": "docker",
            "force_build": False,
            "delete": True,
            "cpu_enforcement_policy": "auto",
            "memory_enforcement_policy": "auto",
            "mounts": [{
                "type": "bind",
                "source": str(RUNTIME),
                "target": "/opt/openjiuwen-runtime",
                "read_only": True,
                "bind": {"create_host_path": False},
            }],
        },
        "agents": [agent_config()],
        "datasets": [{"path": str(TASKS_ROOT), "task_names": TASKS}],
        "tasks": [],
        "artifacts": [],
        "extra_instruction_paths": [],
        "extra_instructions": [],
    }
    if install_only:
        config["verifier"] = {"disable": True}
    return config


def source_hashes() -> dict[str, str]:
    paths = [
        Path(__file__).resolve(),
        WORKTREE / "integrations" / "openjiuwen_agent.py",
        WORKTREE / "integrations" / "harbor_openjiuwen.py",
        WORKTREE / "integrations" / "harbor_openjiuwen_tua.py",
        WORKTREE / "integrations" / "openjiuwen-runtime.lock",
        ORIGINAL_RUNNER,
        PARENT_DIR / "manifest.json",
        PARENT_DIR / "audit.json",
    ]
    for task in TASKS:
        paths.extend([
            TASKS_ROOT / task / "task.toml",
            TASKS_ROOT / task / "instruction.md",
        ])
    result: dict[str, str] = {}
    for path in paths:
        if path.is_relative_to(ROOT):
            name = str(path.relative_to(ROOT))
        else:
            name = str(path)
        result[name] = sha256_file(path)
    return result


def task_metadata(name: str) -> dict[str, Any]:
    task_dir = TASKS_ROOT / name
    parsed = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))
    env = parsed["environment"]
    return {
        "name": name,
        "task_toml_sha256": sha256_file(task_dir / "task.toml"),
        "instruction_sha256": sha256_file(task_dir / "instruction.md"),
        "agent_timeout_seconds": parsed["agent"]["timeout_sec"],
        "verifier_timeout_seconds": parsed["verifier"]["timeout_sec"],
        "environment_build_timeout_seconds": env["build_timeout_sec"],
        "cpus": env["cpus"],
        "memory_mb": env["memory_mb"],
        "storage_mb": env["storage_mb"],
        "gpus": env["gpus"],
        "allow_internet": env["allow_internet"],
        "mcp_servers": env["mcp_servers"],
    }


def result_with_stats(root: Path) -> tuple[Path, dict[str, Any]]:
    for path in sorted(root.rglob("result.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value.get("stats"), dict):
            return path, value
    raise RuntimeError(f"Harbor job result missing below {root}")


def run_offline_self_test() -> Path:
    record = QUALIFICATION_DIR / "offline-self-test.json"
    if record.is_file():
        value = json.loads(record.read_text(encoding="utf-8"))
        if value.get("status") != "passed":
            raise RuntimeError("cached credential-isolation self-test failed")
        return record
    if QUALIFICATION_DIR.exists() and any(QUALIFICATION_DIR.iterdir()):
        raise RuntimeError(f"refusing partial qualification: {QUALIFICATION_DIR}")
    QUALIFICATION_DIR.mkdir(parents=True, exist_ok=True)
    self_env = environment("self-test-not-a-secret")
    self_env["PYTHONPATH"] = str(RUNTIME_SITE_PACKAGES)
    self_env["PATH"] = f"{RUNTIME / 'bin'}:{self_env.get('PATH', '')}"
    subprocess.run([
        str(RUNTIME_PYTHON),
        str(WORKTREE / "integrations" / "openjiuwen_agent.py"),
        "--self-test", "--self-test-output", str(record),
    ], cwd=WORKTREE, env=self_env, check=True, timeout=180)
    value = json.loads(record.read_text(encoding="utf-8"))
    if value.get("tool_subprocess_api_key_visible") is not False:
        raise RuntimeError("tool subprocess credential isolation was not proven")
    record.chmod(0o444)
    return record


def install_qualification() -> tuple[Path, dict[str, Any], list[dict[str, str]]]:
    config_path = QUALIFICATION_DIR / "config.json"
    expected = harbor_config(
        install_only=True, jobs_dir=QUALIFICATION_DIR / "raw"
    )
    if config_path.is_file():
        if canonical(json.loads(config_path.read_text())) != canonical(expected):
            raise RuntimeError("qualification config drift")
    else:
        write_json(config_path, expected)
    try:
        path, result = result_with_stats(QUALIFICATION_DIR / "raw")
    except RuntimeError:
        subprocess.run(
            command(config_path), cwd=WORKTREE,
            env=environment("preflight-not-a-secret"), check=True,
        )
        path, result = result_with_stats(QUALIFICATION_DIR / "raw")
    stats = result["stats"]
    if stats.get("n_completed_trials") != 2 or stats.get("n_errored_trials") != 0:
        raise RuntimeError(f"install-only qualification failed: {stats}")
    if any(stats.get(key) is not None for key in (
        "n_input_tokens", "n_cache_tokens", "n_output_tokens", "cost_usd",
    )):
        raise RuntimeError("install-only qualification unexpectedly used a model")
    handoff_records: list[dict[str, str]] = []
    for record_path in sorted(
        (QUALIFICATION_DIR / "raw").rglob("credential-handoff-self-test.json")
    ):
        record = json.loads(record_path.read_text(encoding="utf-8"))
        if record != {
            "status": "passed",
            "transport": "root-file-to-fifo-to-stdin",
            "environment_key_visible": False,
        }:
            raise RuntimeError(f"credential handoff self-test failed: {record_path}")
        handoff_records.append({
            "path": str(record_path.relative_to(ROOT)),
            "sha256": sha256_file(record_path),
        })
    if len(handoff_records) != len(TASKS):
        raise RuntimeError(
            "expected one credential handoff record per real task image; "
            f"found {len(handoff_records)}"
        )
    return path, result, handoff_records


def manifest_body(
    self_test_path: Path,
    install_path: Path,
    install_result: dict[str, Any],
    handoff_records: list[dict[str, str]],
) -> dict[str, Any]:
    parent = json.loads((PARENT_DIR / "manifest.json").read_text())
    if parent.get("manifest_sha256") != PARENT_MANIFEST_SHA256:
        raise RuntimeError("parent campaign manifest drift")
    if sha256_file(ORIGINAL_RUNNER) != ORIGINAL_RUNNER_SHA256:
        raise RuntimeError("preserved r3 runner drift")
    return {
        "schema_version": 1,
        "campaign_id": CAMPAIGN_ID,
        "kind": "paid-supplemental-replacement-for-two-contaminated-tua-trials",
        "approval": {"required": True, "status": "pending"},
        "parent_campaign": {
            "campaign_id": parent["campaign_id"],
            "manifest_sha256": PARENT_MANIFEST_SHA256,
            "replacement_tasks": TASKS,
            "reason": (
                "parent trajectories used benchmark-visible OPENROUTER_API_KEY "
                "to call auxiliary Qwen models"
            ),
        },
        "superseded_attempt": {
            "campaign_id": FAILED_ATTEMPT_ID,
            "manifest_sha256": FAILED_ATTEMPT_MANIFEST_SHA256,
            "status": "rejected-before-verifier",
            "reason": (
                "the task user could not delete the agent-owned installed key file; "
                "a live trajectory proved direct auxiliary model access remained possible"
            ),
        },
        "benchmark": {
            "name": "TUA-Bench",
            "commit": "3497fd320abcafaf4797424192c891a593fd7964",
            "tasks": [task_metadata(name) for name in TASKS],
            "replicate": 1,
            "planned_trials": 2,
        },
        "runner": {
            "name": "Harbor", "version": "0.22.0", "commit": "4407eb5",
            "integration": "custom thin TUA full-container workspace adapter",
            "outer_trial_retries": 0,
            "trial_concurrency": CONCURRENCY,
            "agent_setup_timeout_seconds": 720,
        },
        "harness": {
            "name": "study openJiuwen Coding Agent candidate",
            "version": "0.1.18", "runtime_version": "0.1.18-r1",
            "rails": [
                "SysOperationRail", "RuntimeBudgetRail-study",
                "ConfirmedCompletionRail-study", "AuditRail-study",
                "SecurityRail", "ModelAnomalyDetectionRail",
            ],
            "tools": [
                "read_file", "write_file", "edit_file", "glob",
                "list_files", "grep", "bash",
            ],
            "skills": [], "mcp_servers": [], "memory": False,
            "subagents": False, "task_planning": False,
            "completion_confirmations": 2, "max_outer_rounds": 8,
        },
        "model_transport": {
            "requested_model": MODEL,
            "expected_actual_model": "deepseek/deepseek-v4-pro-20260813",
            "gateway": "OpenRouter", "expected_endpoint_provider": "DeepSeek",
            "base_url": "https://openrouter.ai/api/v1",
            "protocol": "OpenAI Chat Completions streaming",
            "provider_route": {
                "only": ["deepseek"], "allow_fallbacks": False,
                "require_parameters": True,
            },
            "protocol_translation": False,
        },
        "controls": {
            "reasoning_effort": {
                "value": "high", "wire": {"reasoning": {"effort": "high"}},
            },
            "context_window": CONTEXT_WINDOW,
            "context_compression": None,
            "max_output_tokens": None,
            "temperature": None, "top_p": None, "seed": None,
            "parallel_tool_calls": True,
            "runtime_budget_seconds": RUNTIME_BUDGET_SECONDS,
            "extra_prompts": [],
        },
        "timeouts_and_retries": {
            "task_agent_seconds": 2400, "verifier_seconds": 2400,
            "environment_build_seconds": 2400, "agent_setup_seconds": 720,
            "llm_total_seconds": 360, "llm_first_chunk_seconds": 300,
            "llm_stream_idle_seconds": 60, "native_http_max_retries": 1,
            "native_tool_timeout_seconds": 300, "whole_trial_retries": 0,
        },
        "credential_boundary": {
            "root_only_secret_file_mode": "0600",
            "transport": "root-only-file-to-one-shot-fifo-to-process-stdin",
            "secret_file_and_fifo_removed_after_stdin_handoff": True,
            "key_absent_from_process_environment": True,
            "key_absent_from_process_command_line": True,
            "model_client_receives_key_from_explicit_process_argument": True,
            "tool_subprocess_api_key_visible": False,
            "offline_self_test": str(self_test_path.relative_to(ROOT)),
            "offline_self_test_sha256": sha256_file(self_test_path),
            "real_task_image_self_test": (
                "each install-only trial must contain "
                "credential-handoff-self-test.json with status=passed"
            ),
            "real_task_image_self_test_records": handoff_records,
        },
        "source_equivalence": {
            "r3_runner_sha256": ORIGINAL_RUNNER_SHA256,
            "supplement_runner_sha256": sha256_file(
                WORKTREE / "integrations" / "openjiuwen_agent.py"
            ),
            "inactive_difference": (
                "r3 preserved source includes optional ALE CUA MCP parsing; "
                "the supplemental TUA config has no MCP servers"
            ),
            "intentional_live_difference": (
                "OpenRouter credential is delivered through a root-controlled "
                "one-shot stdin pipe and is unavailable to benchmark tools"
            ),
        },
        "accounting": {
            "primary": "OpenRouter settled per-generation metadata",
            "secondary": "openJiuwen native usage and Harbor aggregation",
            "estimated_total_usd": [0.10, 6.0],
            "per_task_cost_anomaly_usd": 3.0,
        },
        "zero_cost_preflight": {
            "install_only_result": str(install_path.relative_to(ROOT)),
            "install_only_result_sha256": sha256_file(install_path),
            "completed_trials": install_result["stats"]["n_completed_trials"],
            "paid_model_calls": 0,
        },
        "required_post_run_audit": [
            "verify no non-DeepSeek generation IDs and no task-visible key",
            "verify actual DeepSeek provider/model, tool closure and verifier",
            "reconcile provider/native/Harbor usage and replace only two rows",
        ],
        "claim_limit": (
            "replacement evidence for these two TUA tasks only; combined "
            "120-task score requires post-run audit"
        ),
        "source_sha256": source_hashes(),
        "resolved_config_sha256": hashlib.sha256(canonical(
            harbor_config(install_only=False, jobs_dir=RUN_DIR / "raw")
        )).hexdigest(),
    }


def materialize() -> dict[str, Any]:
    self_test_path = run_offline_self_test()
    install_path, install_result, handoff_records = install_qualification()
    if RUN_DIR.exists():
        return verify()
    body = manifest_body(
        self_test_path, install_path, install_result, handoff_records
    )
    manifest = dict(body)
    manifest["manifest_sha256"] = hashlib.sha256(canonical(body)).hexdigest()
    write_json(
        RUN_DIR / "config.json",
        harbor_config(install_only=False, jobs_dir=RUN_DIR / "raw"),
    )
    write_json(RUN_DIR / "manifest.json", manifest)
    return manifest


def verify() -> dict[str, Any]:
    manifest = json.loads((RUN_DIR / "manifest.json").read_text())
    recorded = manifest["manifest_sha256"]
    body = {key: value for key, value in manifest.items()
            if key != "manifest_sha256"}
    if hashlib.sha256(canonical(body)).hexdigest() != recorded:
        raise RuntimeError("manifest hash mismatch")
    if source_hashes() != manifest["source_sha256"]:
        raise RuntimeError("source changed after campaign freeze")
    config = json.loads((RUN_DIR / "config.json").read_text())
    if hashlib.sha256(canonical(config)).hexdigest() != manifest[
        "resolved_config_sha256"
    ]:
        raise RuntimeError("resolved config changed after campaign freeze")
    return manifest


def launch(approved_hash: str) -> int:
    manifest = verify()
    if approved_hash != manifest["manifest_sha256"]:
        raise RuntimeError("approved manifest hash mismatch")
    shell_command = (
        f"set -a; source {shlex.quote(str(ROOT / '.env'))}; set +a; "
        f"exec {shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__)))} "
        f"run --approved-manifest-sha256 {shlex.quote(approved_hash)}"
    )
    subprocess.run([
        "tmux", "new-session", "-d", "-s", CAMPAIGN_ID,
        "-c", str(WORKTREE), "bash", "-lc", shell_command,
    ], check=True)
    print(f"Started tmux session {CAMPAIGN_ID}")
    return 0


def run(approved_hash: str) -> int:
    manifest = verify()
    if approved_hash != manifest["manifest_sha256"]:
        raise RuntimeError("approved manifest hash mismatch")
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is required")
    return subprocess.run(
        command(RUN_DIR / "config.json"), cwd=WORKTREE,
        env=environment(api_key), check=False,
    ).returncode


def status() -> int:
    try:
        _, result = result_with_stats(RUN_DIR / "raw")
    except RuntimeError:
        print("not-started-or-running: no Harbor result.json yet")
        return 0
    print(json.dumps(result["stats"], indent=2, sort_keys=True))
    print("manual-trajectory-and-provider-audit-required")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command_name", required=True)
    commands.add_parser("preflight")
    for name in ("run", "launch"):
        item = commands.add_parser(name)
        item.add_argument("--approved-manifest-sha256", required=True)
    commands.add_parser("status")
    args = parser.parse_args()
    try:
        if args.command_name == "preflight":
            manifest = materialize()
            print("Zero-cost credential-isolated two-task preflight passed.")
            print(f"Manifest SHA256 {manifest['manifest_sha256']}")
            return 0
        if args.command_name == "run":
            return run(args.approved_manifest_sha256)
        if args.command_name == "launch":
            return launch(args.approved_manifest_sha256)
        return status()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"ERROR  {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
