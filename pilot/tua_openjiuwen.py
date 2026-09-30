#!/usr/bin/env python3
"""Freeze and run the first openJiuwen Coding Agent TUA lifecycle canary.

This remains a candidate qualification pilot.  It reuses the exact public-API
composition and prebuilt runtime used by the accepted OpenJiuwen TB4 text path;
it does not add TUA-specific prompts, tools, Rails, MCP servers, or retries.
"""

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
CAMPAIGN_ID = "pilot-tua-openjiuwen-deepseek-20260915-r1"
QUALIFICATION_ID = "qualification-tua-openjiuwen-install-20260915-r3"
RUN_DIR = ROOT / "runs" / CAMPAIGN_ID
QUALIFICATION_DIR = ROOT / "runs" / QUALIFICATION_ID
TUA_ROOT = ROOT / "vendor" / "tua-bench"
TASKS_ROOT = TUA_ROOT / "tasks"
TASK = "106-create-charles-ssh-user"
RUNTIME = ROOT / ".cache" / "openjiuwen" / "runtime-0.1.18-r1"
HARBOR_VERSION = "0.22.0"
HARBOR_COMMIT = "4407eb5"
HARBOR_BIN = ROOT / ".cache" / "harbor-tua-0.22.0" / "bin" / "harbor"
TUA_COMMIT = "3497fd320abcafaf4797424192c891a593fd7964"
MODEL = "deepseek/deepseek-v4-pro-0813"
EXPECTED_MODEL = "deepseek/deepseek-v4-pro-20260813"
EXPECTED_PROVIDER = "DeepSeek"
CONTEXT_WINDOW = 1_048_576
RUNTIME_BUDGET_SECONDS = 2_400
SETUP_TIMEOUT_MULTIPLIER = 2.0


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


def write_json(path: Path, value: Any, *, mode: int = 0o444) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    path.chmod(mode)


def uv_environment(*, api_key: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "OPENROUTER_API_KEY": api_key,
            "PYTHONPATH": str(ROOT),
            "UV_CACHE_DIR": str(ROOT / ".cache" / "uv"),
            "UV_TOOL_DIR": str(ROOT / ".cache" / "uv-tools"),
            "UV_PYTHON_INSTALL_DIR": str(ROOT / ".cache" / "uv-python"),
        }
    )
    return env


def harbor_command(config_path: Path, *, print_config: bool = False) -> list[str]:
    return [
        str(HARBOR_BIN),
        "run",
        "--config",
        str(config_path),
        "--print-config" if print_config else "--yes",
    ]


def agent_config() -> dict[str, Any]:
    return {
        "import_path": "integrations.harbor_openjiuwen:OpenJiuwenCodingAgent",
        "model_name": f"openrouter/{MODEL}",
        "n_concurrent": 1,
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
            "runtime_budget_rail_enabled": False,
            "context_compression_enabled": True,
            "completion_timeout_seconds": RUNTIME_BUDGET_SECONDS,
            "max_outer_rounds": 8,
            "llm_request_timeout_seconds": 360,
            "llm_stream_first_chunk_timeout_seconds": 300,
            "llm_stream_idle_timeout_seconds": 300,
            "llm_http_max_retries": 5,
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
        "agent_setup_timeout_multiplier": SETUP_TIMEOUT_MULTIPLIER,
        "n_concurrent_trials": 1,
        "retry": {"max_retries": 0},
        "environment": {
            "type": "docker",
            "force_build": False,
            "delete": True,
            "cpu_enforcement_policy": "auto",
            "memory_enforcement_policy": "auto",
            "mounts": [
                {
                    "type": "bind",
                    "source": str(RUNTIME),
                    "target": "/opt/openjiuwen-runtime",
                    "read_only": True,
                    "bind": {"create_host_path": False},
                }
            ],
        },
        "agents": [agent_config()],
        "datasets": [{"path": str(TASKS_ROOT), "task_names": [TASK]}],
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
        ROOT / "integrations" / "harbor_openjiuwen.py",
        ROOT / "integrations" / "openjiuwen_agent.py",
        ROOT / "integrations" / "openjiuwen-runtime.lock",
        TASKS_ROOT / TASK / "task.toml",
        TASKS_ROOT / TASK / "instruction.md",
    ]
    return {str(path.relative_to(ROOT)): sha256_file(path) for path in paths}


def task_metadata() -> dict[str, Any]:
    task_dir = TASKS_ROOT / TASK
    parsed = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))
    environment = parsed["environment"]
    return {
        "name": TASK,
        "task_toml_sha256": sha256_file(task_dir / "task.toml"),
        "instruction_sha256": sha256_file(task_dir / "instruction.md"),
        "agent_timeout_seconds": parsed["agent"]["timeout_sec"],
        "verifier_timeout_seconds": parsed["verifier"]["timeout_sec"],
        "environment_build_timeout_seconds": environment["build_timeout_sec"],
        "cpus": environment["cpus"],
        "memory_mb": environment["memory_mb"],
        "storage_mb": environment["storage_mb"],
        "gpus": environment["gpus"],
        "allow_internet": environment["allow_internet"],
        "mcp_servers": environment["mcp_servers"],
    }


def qualification_result() -> tuple[Path, dict[str, Any]]:
    candidates = sorted((QUALIFICATION_DIR / "raw").rglob("result.json"))
    for path in candidates:
        result = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(result.get("stats"), dict):
            return path, result
    raise RuntimeError("qualification job result is missing")


def qualification_trial_result() -> tuple[Path, dict[str, Any]]:
    candidates = sorted((QUALIFICATION_DIR / "raw").rglob("result.json"))
    for path in candidates:
        result = json.loads(path.read_text(encoding="utf-8"))
        if result.get("agent_setup") is not None:
            return path, result
    raise RuntimeError("qualification trial result is missing")


def manifest_body() -> dict[str, Any]:
    install_path, install_result = qualification_result()
    trial_path, trial_result = qualification_trial_result()
    stats = install_result.get("stats", {})
    if stats.get("n_completed_trials") != 1 or stats.get("n_errored_trials") != 0:
        raise RuntimeError("OpenJiuwen TUA install-only qualification did not pass")
    return {
        "schema_version": 1,
        "campaign_id": CAMPAIGN_ID,
        "kind": "paid-tua-openjiuwen-lifecycle-qualification-pilot-not-primary-score",
        "approval": {"required": True, "status": "pending"},
        "benchmark": {
            "name": "TUA-Bench",
            "commit": TUA_COMMIT,
            "task": task_metadata(),
            "replicate": 1,
            "planned_trials": 1,
        },
        "runner": {
            "name": "Harbor",
            "version": HARBOR_VERSION,
            "commit": HARBOR_COMMIT,
            "integration": "custom thin BaseInstalledAgent adapter",
            "outer_trial_retries": 0,
            "agent_setup_timeout_seconds": 720,
        },
        "harness": {
            "name": "study openJiuwen Coding Agent candidate",
            "version": "0.1.18",
            "runtime_version": "0.1.18-r1",
            "classification": (
                "study composition from public openjiuwen.harness API; "
                "not a stock preset or exact Huawei paper reproduction"
            ),
            "rails": [
                "SysOperationRail",
                "ContextProcessorRail(preset=True)",
                "ConfirmedCompletionRail-study",
                "AuditRail-study",
                "SecurityRail",
                "ModelAnomalyDetectionRail",
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
            "requested_model": MODEL,
            "expected_actual_model": EXPECTED_MODEL,
            "provider": "OpenRouter",
            "expected_endpoint_provider": EXPECTED_PROVIDER,
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
            "context_window": CONTEXT_WINDOW,
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
            "runtime_budget_seconds": RUNTIME_BUDGET_SECONDS,
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
            "cpus": 1,
            "memory_mb": 2048,
            "storage_mb": 10240,
            "gpus": 0,
            "network": "task-native allow_internet=true",
            "runtime_mount": "/opt/openjiuwen-runtime:ro",
            "workspace": "/app",
        },
        "accounting": {
            "primary": "OpenRouter settled per-generation metadata",
            "secondary": "openJiuwen native usage and Harbor aggregation",
            "estimated_total_usd": [0.02, 1.0],
            "hard_dollar_stop": None,
        },
        "zero_cost_preflight": {
            "install_only_result": str(install_path.relative_to(ROOT)),
            "install_only_result_sha256": sha256_file(install_path),
            "install_only_trial_result": str(trial_path.relative_to(ROOT)),
            "install_only_trial_result_sha256": sha256_file(trial_path),
            "agent_setup": trial_result["agent_setup"],
            "paid_model_calls": 0,
        },
        "required_post_run_audit": [
            "every generation ID and settled actual provider/model",
            "seven-tool schema stability and tool-call/result closure",
            "reasoning_details replay, retries, 429/5xx and timeout events",
            "native/Harbor/provider token and cost reconciliation",
            "verifier validity and trajectory review",
        ],
        "claim_limit": (
            "one text TUA lifecycle only; no full-benchmark quality, "
            "formal concurrency, multimodality, or compaction claim"
        ),
        "source_sha256": source_hashes(),
        "resolved_config_sha256": hashlib.sha256(
            canonical(harbor_config(install_only=False, jobs_dir=RUN_DIR / "raw"))
        ).hexdigest(),
    }


def materialize_paid_campaign() -> dict[str, Any]:
    if RUN_DIR.exists():
        manifest_path = RUN_DIR / "manifest.json"
        if not manifest_path.is_file():
            raise RuntimeError(f"refusing partial campaign directory: {RUN_DIR}")
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    body = manifest_body()
    manifest = dict(body)
    manifest["manifest_sha256"] = hashlib.sha256(canonical(body)).hexdigest()
    write_json(
        RUN_DIR / "config.json",
        harbor_config(install_only=False, jobs_dir=RUN_DIR / "raw"),
    )
    write_json(RUN_DIR / "manifest.json", manifest)
    return manifest


def verify_paid_campaign() -> dict[str, Any]:
    manifest_path = RUN_DIR / "manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError("paid campaign is not materialized; run preflight first")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    recorded = manifest.get("manifest_sha256")
    body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if hashlib.sha256(canonical(body)).hexdigest() != recorded:
        raise RuntimeError("manifest hash mismatch")
    if source_hashes() != manifest["source_sha256"]:
        raise RuntimeError("source changed after campaign freeze")
    config = json.loads((RUN_DIR / "config.json").read_text(encoding="utf-8"))
    if hashlib.sha256(canonical(config)).hexdigest() != manifest["resolved_config_sha256"]:
        raise RuntimeError("resolved Harbor config changed after campaign freeze")
    return manifest


def check_tua_checkout() -> None:
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=TUA_ROOT, text=True
    ).strip()
    if head != TUA_COMMIT:
        raise RuntimeError(f"TUA commit drift: {head}")
    task = task_metadata()
    expected = {
        "agent_timeout_seconds": 2400.0,
        "verifier_timeout_seconds": 2400.0,
        "environment_build_timeout_seconds": 2400.0,
        "cpus": 1,
        "memory_mb": 2048,
        "storage_mb": 10240,
        "gpus": 0,
        "allow_internet": True,
        "mcp_servers": [],
    }
    for key, value in expected.items():
        if task[key] != value:
            raise RuntimeError(f"TUA task control drift: {key}={task[key]!r}")


def check_runtime() -> None:
    if not HARBOR_BIN.is_file():
        raise RuntimeError(f"missing isolated Harbor {HARBOR_VERSION}: {HARBOR_BIN}")
    version = subprocess.run(
        [str(HARBOR_BIN), "--version"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if version.returncode != 0 or version.stdout.strip() != HARBOR_VERSION:
        raise RuntimeError(
            f"isolated Harbor version drift: {version.stdout.strip()!r}"
        )
    metadata_path = RUNTIME / "runtime.json"
    if not metadata_path.is_file():
        raise RuntimeError(f"missing prebuilt OpenJiuwen runtime: {RUNTIME}")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    expected = {
        "format": 1,
        "openjiuwen_version": "0.1.18",
        "openjiuwen_tag_commit": "1d37ae3007f9df9a8489a7ab271141b03be08f66",
        "python_version": "3.12.12",
        "dependency_lock_sha256": sha256_file(
            ROOT / "integrations" / "openjiuwen-runtime.lock"
        ),
    }
    if any(metadata.get(key) != value for key, value in expected.items()):
        raise RuntimeError("prebuilt OpenJiuwen runtime metadata drift")


def resolve_config(config_path: Path) -> dict[str, Any]:
    result = subprocess.run(
        harbor_command(config_path, print_config=True),
        cwd=ROOT,
        env=uv_environment(api_key="preflight-not-a-secret"),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Harbor config resolution failed: " + (result.stderr or result.stdout)[-1200:]
        )
    return json.loads(result.stdout)


def preflight() -> dict[str, Any]:
    check_tua_checkout()
    check_runtime()
    config_path = QUALIFICATION_DIR / "config.json"
    expected_config = harbor_config(
        install_only=True, jobs_dir=QUALIFICATION_DIR / "raw"
    )
    if config_path.is_file():
        existing_config = json.loads(config_path.read_text(encoding="utf-8"))
        if canonical(existing_config) != canonical(expected_config):
            raise RuntimeError(
                f"refusing changed qualification directory: {QUALIFICATION_DIR}"
            )
    elif QUALIFICATION_DIR.exists():
        raise RuntimeError(f"refusing partial qualification directory: {QUALIFICATION_DIR}")
    else:
        write_json(
            config_path,
            expected_config,
        )
    results = sorted((QUALIFICATION_DIR / "raw").rglob("result.json"))
    if not results:
        resolved = resolve_config(config_path)
        frozen = json.loads(config_path.read_text(encoding="utf-8"))
        agent = resolved["agents"][0]
        if (
            agent["import_path"]
            != "integrations.harbor_openjiuwen:OpenJiuwenCodingAgent"
            or agent["model_name"] != f"openrouter/{MODEL}"
            or agent["kwargs"]["runtime_budget_seconds"] != 2400
            or agent["kwargs"]["runtime_budget_rail_enabled"] is not False
            or agent["kwargs"]["context_compression_enabled"] is not True
            or agent["kwargs"]["openrouter_route"]
            != {
                "only": ["deepseek"],
                "allow_fallbacks": False,
                "require_parameters": True,
            }
            or resolved["agent_setup_timeout_multiplier"] != 2.0
            or frozen["retry"]["max_retries"] != 0
        ):
            raise RuntimeError("resolved OpenJiuwen TUA configuration drift")
        result = subprocess.run(
            harbor_command(config_path),
            cwd=ROOT,
            env=uv_environment(api_key="preflight-not-a-secret"),
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"TUA OpenJiuwen install-only run failed: {result.returncode}")
    qualification_result()
    manifest = materialize_paid_campaign()
    verify_paid_campaign()
    print("Zero-cost TUA OpenJiuwen preflight passed; no paid model call was made.")
    print(f"Manifest SHA256 {manifest['manifest_sha256']}")
    return manifest


def run(approved_hash: str) -> int:
    manifest = verify_paid_campaign()
    if approved_hash != manifest["manifest_sha256"]:
        raise RuntimeError("approved manifest hash does not match frozen campaign")
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is required")
    return subprocess.run(
        harbor_command(RUN_DIR / "config.json"),
        cwd=ROOT,
        env=uv_environment(api_key=api_key),
        check=False,
    ).returncode


def launch(approved_hash: str) -> int:
    verify_paid_campaign()
    command = (
        f"set -a; source {shlex.quote(str(ROOT / '.env'))}; set +a; "
        f"exec {shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} "
        f"run --approved-manifest-sha256 {shlex.quote(approved_hash)}"
    )
    subprocess.run(
        [
            "tmux", "new-session", "-d", "-s", CAMPAIGN_ID,
            "-c", str(ROOT), "bash", "-lc", command,
        ],
        check=True,
    )
    print(f"Started tmux session {CAMPAIGN_ID}")
    return 0


def status() -> int:
    results = sorted((RUN_DIR / "raw").rglob("result.json"))
    if not results:
        print("not-started-or-running: no Harbor result.json yet")
        return 0
    result = json.loads(results[-1].read_text(encoding="utf-8"))
    print(json.dumps(result.get("stats", result), indent=2, sort_keys=True))
    print("manual-trajectory-review-required")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("preflight")
    run_parser = commands.add_parser("run")
    run_parser.add_argument("--approved-manifest-sha256", required=True)
    launch_parser = commands.add_parser("launch")
    launch_parser.add_argument("--approved-manifest-sha256", required=True)
    commands.add_parser("status")
    args = parser.parse_args()
    try:
        if args.command == "preflight":
            preflight()
            return 0
        if args.command == "run":
            return run(args.approved_manifest_sha256)
        if args.command == "launch":
            return launch(args.approved_manifest_sha256)
        return status()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"ERROR  {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
