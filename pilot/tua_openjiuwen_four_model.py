#!/usr/bin/env python3
"""Four-model OpenJiuwen qualification on one fixed TUA-Bench text task."""

from __future__ import annotations

import argparse
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


ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN_ID = "pilot-tua-openjiuwen-four-model-20260918-r1"
QUALIFICATION_ID = "qualification-tua-openjiuwen-current-credential-20260918-r1"
RUN_DIR = ROOT / "runs" / CAMPAIGN_ID
QUALIFICATION_DIR = ROOT / "runs" / QUALIFICATION_ID
TASKS_ROOT = ROOT / "vendor" / "tua-bench" / "tasks"
TASK = "106-create-charles-ssh-user"
TUA_COMMIT = "3497fd320abcafaf4797424192c891a593fd7964"
HARBOR_BIN = ROOT / ".cache" / "harbor-tua-0.22.0" / "bin" / "harbor"
RUNTIME = ROOT / ".cache" / "openjiuwen" / "runtime-0.1.18-r1"

MODELS: dict[str, dict[str, Any]] = {
    "claude-opus-5": {
        "model": "anthropic/claude-opus-5",
        "expected_model": "anthropic/claude-opus-5-20260723",
        "provider": "Anthropic",
        "only": ["anthropic"],
        "quantizations": None,
        "context": 1_000_000,
        "advertised_output": 128_000,
    },
    "gpt-6-astra": {
        "model": "openai/gpt-6-astra",
        "expected_model": "openai/gpt-6-astra-20260903",
        "provider": "OpenAI",
        "only": ["openai"],
        "quantizations": None,
        "context": 1_050_000,
        "advertised_output": 128_000,
    },
    "kimi-k3": {
        "model": "moonshotai/kimi-k3",
        "expected_model": "moonshotai/kimi-k3-20260715",
        "provider": "Moonshot AI",
        "only": ["moonshotai/mxfp4"],
        "quantizations": ["mxfp4"],
        "context": 1_048_576,
        "advertised_output": 943_718,
    },
    "glm-5.3": {
        "model": "z-ai/glm-5.3",
        "expected_model": "z-ai/glm-5.3-20260816",
        "provider": "Z.AI",
        "only": ["z-ai/fp8"],
        "quantizations": ["fp8"],
        "context": 1_048_576,
        "advertised_output": 131_072,
    },
}


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any, *, mode: int = 0o444) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    path.chmod(mode)


def command(config: Path, *, print_config: bool = False) -> list[str]:
    return [
        str(HARBOR_BIN),
        "run",
        "--config",
        str(config),
        "--print-config" if print_config else "--yes",
    ]


def launch_env(api_key: str) -> dict[str, str]:
    return {
        **os.environ,
        "OPENROUTER_API_KEY": api_key,
        "PYTHONPATH": str(ROOT),
        "UV_CACHE_DIR": str(ROOT / ".cache" / "uv"),
        "UV_TOOL_DIR": str(ROOT / ".cache" / "uv-tools"),
        "UV_PYTHON_INSTALL_DIR": str(ROOT / ".cache" / "uv-python"),
    }


def agent_config(logical: str) -> dict[str, Any]:
    spec = MODELS[logical]
    route = {
        "only": spec["only"],
        "allow_fallbacks": False,
        "require_parameters": True,
    }
    if spec["quantizations"]:
        route["quantizations"] = spec["quantizations"]
    return {
        "import_path": (
            "integrations.harbor_openjiuwen_tua:TUAOpenJiuwenCodingAgent"
        ),
        "model_name": f"openrouter/{spec['model']}",
        "n_concurrent": 1,
        "include_logs": ["**/*"],
        "skills": [],
        "mcp_servers": [],
        "kwargs": {
            "version": "0.1.18",
            "context_window": spec["context"],
            "openrouter_route": route,
            "runtime_budget_seconds": 2400,
            "runtime_budget_rail_enabled": True,
            "context_compression_enabled": True,
            "completion_timeout_seconds": 2400,
            "max_outer_rounds": 8,
            "llm_request_timeout_seconds": 360,
            "llm_stream_first_chunk_timeout_seconds": 300,
            "llm_stream_idle_timeout_seconds": 300,
            "llm_http_max_retries": 5,
            "prompt_language": "en",
        },
    }


def harbor_config(
    logical: str, *, install_only: bool, jobs_dir: Path
) -> dict[str, Any]:
    job_name = (
        QUALIFICATION_ID
        if install_only
        else f"{CAMPAIGN_ID}--{logical}"
    )
    config: dict[str, Any] = {
        "job_name": job_name,
        "jobs_dir": str(jobs_dir),
        "n_attempts": 1,
        "install_only": install_only,
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
        "agents": [agent_config(logical)],
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
        ROOT / "integrations" / "harbor_openjiuwen_tua.py",
        ROOT / "integrations" / "openjiuwen_agent.py",
        ROOT / "integrations" / "openjiuwen-runtime.lock",
        TASKS_ROOT / TASK / "task.toml",
        TASKS_ROOT / TASK / "instruction.md",
    ]
    return {str(path.relative_to(ROOT)): sha256_file(path) for path in paths}


def task_metadata() -> dict[str, Any]:
    task_dir = TASKS_ROOT / TASK
    parsed = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))
    env = parsed["environment"]
    return {
        "name": TASK,
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


def check_fixed_environment() -> None:
    tua_head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT / "vendor" / "tua-bench", text=True
    ).strip()
    if tua_head != TUA_COMMIT:
        raise RuntimeError(f"TUA commit drift: {tua_head}")
    version = subprocess.run(
        [str(HARBOR_BIN), "--version"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if version.returncode != 0 or version.stdout.strip() != "0.22.0":
        raise RuntimeError(f"Harbor version drift: {version.stdout.strip()!r}")
    metadata = json.loads((RUNTIME / "runtime.json").read_text(encoding="utf-8"))
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
        raise RuntimeError("OpenJiuwen runtime metadata drift")
    expected_task = {
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
    task = task_metadata()
    for key, value in expected_task.items():
        if task[key] != value:
            raise RuntimeError(f"TUA task drift: {key}={task[key]!r}")


def resolved_config(config_path: Path) -> dict[str, Any]:
    result = subprocess.run(
        command(config_path, print_config=True),
        cwd=ROOT,
        env=launch_env("preflight-not-a-secret"),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout)[-2000:])
    return json.loads(result.stdout)


def qualification_result() -> tuple[Path, dict[str, Any]]:
    for path in sorted((QUALIFICATION_DIR / "raw").rglob("result.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data.get("stats"), dict):
            return path, data
    raise RuntimeError("current-source install-only qualification result is missing")


def credential_self_test() -> tuple[Path, dict[str, Any]]:
    paths = sorted(
        (QUALIFICATION_DIR / "raw").rglob("credential-handoff-self-test.json")
    )
    if len(paths) != 1:
        raise RuntimeError(f"expected one credential self-test; found {len(paths)}")
    data = json.loads(paths[0].read_text(encoding="utf-8"))
    expected = {
        "status": "passed",
        "transport": "root-file-to-fifo-to-stdin",
        "environment_key_visible": False,
    }
    if data != expected:
        raise RuntimeError(f"credential self-test failed: {data}")
    return paths[0], data


def manifest_body() -> dict[str, Any]:
    q_path, q_result = qualification_result()
    c_path, _ = credential_self_test()
    q_stats = q_result["stats"]
    if q_stats.get("n_completed_trials") != 1 or q_stats.get("n_errored_trials") != 0:
        raise RuntimeError("current-source install-only qualification did not pass")
    configs = {
        logical: harbor_config(
            logical, install_only=False, jobs_dir=RUN_DIR / "raw" / logical
        )
        for logical in MODELS
    }
    return {
        "schema_version": 1,
        "campaign_id": CAMPAIGN_ID,
        "kind": "paid-four-model-tua-openjiuwen-current-source-qualification",
        "approval": {"required": True, "status": "pending"},
        "benchmark": {
            "name": "TUA-Bench",
            "commit": TUA_COMMIT,
            "task": task_metadata(),
            "replicate": 1,
            "planned_trials": 4,
            "primary_scores": False,
        },
        "runner": {
            "name": "Harbor",
            "version": "0.22.0",
            "integration": (
                "integrations.harbor_openjiuwen_tua:TUAOpenJiuwenCodingAgent"
            ),
            "outer_trial_retries": 0,
            "agent_setup_timeout_seconds": 720,
        },
        "harness": {
            "name": "study OpenJiuwen Coding Agent candidate",
            "version": "0.1.18",
            "upstream_commit": "1d37ae3007f9df9a8489a7ab271141b03be08f66",
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
        "models": [
            {
                "logical": logical,
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
            }
            for logical, spec in MODELS.items()
        ],
        "controls": {
            "reasoning_effort": "high via reasoning.effort",
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
            "cpus": 1,
            "memory_mb": 2048,
            "storage_mb": 10240,
            "gpus": 0,
            "network": "task-native allow_internet=true",
            "workspace_and_filesystem_boundary": "/",
            "runtime_mount": "/opt/openjiuwen-runtime:ro",
        },
        "credential_boundary": {
            "transport": "root-only-file-to-one-shot-fifo-to-model-process-stdin",
            "task_environment_key_visible": False,
            "task_shell_secret_file_visible_after_handoff": False,
            "compatibility_only": True,
        },
        "concurrency": {
            "campaign": 4,
            "per_model": 1,
            "pacing": "four distinct first-party provider routes launched together",
        },
        "accounting": {
            "primary": "OpenRouter settled per-generation metadata",
            "secondary": "OpenJiuwen native usage and Harbor aggregation",
            "estimated_total_usd": [0.1, 15.0],
            "hard_dollar_stop": None,
            "uncertainty": "no safe mid-response dollar stop; each task is deadline bounded",
        },
        "zero_cost_preflight": {
            "install_only_result": str(q_path.relative_to(ROOT)),
            "install_only_result_sha256": sha256_file(q_path),
            "credential_self_test": str(c_path.relative_to(ROOT)),
            "credential_self_test_sha256": sha256_file(c_path),
            "paid_model_calls": 0,
        },
        "required_post_run_audit": [
            "all generation IDs and settled actual provider/model/fallback",
            "tool schema stability and tool-call/result closure",
            "reasoning replay, compression, output limits, retries and timeouts",
            "provider/native/Harbor token and cost reconciliation",
            "credential isolation and absence of auxiliary model API calls",
            "independent verifier validity and trajectory outcome classification",
        ],
        "claim_limit": (
            "one text TUA task per model; no full-benchmark quality, multimodal, "
            "compaction-recovery or formal-concurrency claim"
        ),
        "source_sha256": source_hashes(),
        "resolved_config_sha256": {
            logical: hashlib.sha256(canonical(config)).hexdigest()
            for logical, config in configs.items()
        },
    }


def materialize() -> dict[str, Any]:
    if RUN_DIR.exists():
        return verify()
    body = manifest_body()
    manifest = dict(body)
    manifest["manifest_sha256"] = hashlib.sha256(canonical(body)).hexdigest()
    for logical in MODELS:
        write_json(
            RUN_DIR / "configs" / f"{logical}.json",
            harbor_config(
                logical, install_only=False, jobs_dir=RUN_DIR / "raw" / logical
            ),
        )
    write_json(RUN_DIR / "manifest.json", manifest)
    return manifest


def verify() -> dict[str, Any]:
    manifest = json.loads((RUN_DIR / "manifest.json").read_text(encoding="utf-8"))
    recorded = manifest["manifest_sha256"]
    body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if hashlib.sha256(canonical(body)).hexdigest() != recorded:
        raise RuntimeError("manifest hash mismatch")
    if source_hashes() != manifest["source_sha256"]:
        raise RuntimeError("source changed after campaign freeze")
    for logical in MODELS:
        config = json.loads(
            (RUN_DIR / "configs" / f"{logical}.json").read_text(encoding="utf-8")
        )
        if hashlib.sha256(canonical(config)).hexdigest() != manifest[
            "resolved_config_sha256"
        ][logical]:
            raise RuntimeError(f"resolved config drift: {logical}")
    return manifest


def preflight() -> dict[str, Any]:
    check_fixed_environment()
    qualification_config = harbor_config(
        "gpt-6-astra", install_only=True, jobs_dir=QUALIFICATION_DIR / "raw"
    )
    q_config_path = QUALIFICATION_DIR / "config.json"
    if not q_config_path.exists():
        if QUALIFICATION_DIR.exists():
            raise RuntimeError(f"refusing partial qualification dir: {QUALIFICATION_DIR}")
        write_json(q_config_path, qualification_config)
    elif canonical(json.loads(q_config_path.read_text())) != canonical(
        qualification_config
    ):
        raise RuntimeError("qualification config drift")
    resolved = resolved_config(q_config_path)
    agent = resolved["agents"][0]
    if (
        agent["import_path"]
        != "integrations.harbor_openjiuwen_tua:TUAOpenJiuwenCodingAgent"
        or agent["kwargs"]["runtime_budget_rail_enabled"] is not True
        or agent["kwargs"]["context_compression_enabled"] is not True
        or qualification_config["retry"]["max_retries"] != 0
    ):
        raise RuntimeError("resolved qualification config drift")
    try:
        qualification_result()
    except RuntimeError:
        result = subprocess.run(
            command(q_config_path),
            cwd=ROOT,
            env=launch_env("preflight-not-a-secret"),
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"install-only qualification failed: {result.returncode}")
    qualification_result()
    credential_self_test()
    for logical in MODELS:
        temp = QUALIFICATION_DIR / f"resolved-{logical}.json"
        write_json(
            temp,
            harbor_config(logical, install_only=False, jobs_dir=Path("/tmp/preflight")),
        )
        model_resolved = resolved_config(temp)
        resolved_agent = model_resolved["agents"][0]
        if resolved_agent["model_name"] != f"openrouter/{MODELS[logical]['model']}":
            raise RuntimeError(f"resolved model drift: {logical}")
    manifest = materialize()
    print("Zero-cost four-model OpenJiuwen TUA preflight passed; paid calls: 0")
    print(f"Manifest SHA256 {manifest['manifest_sha256']}")
    return manifest


def run_one(logical: str, api_key: str) -> tuple[str, int]:
    log_path = RUN_DIR / "operator" / f"{logical}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("wb") as log:
        result = subprocess.run(
            command(RUN_DIR / "configs" / f"{logical}.json"),
            cwd=ROOT,
            env=launch_env(api_key),
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
        futures = {pool.submit(run_one, logical, api_key): logical for logical in MODELS}
        for future in as_completed(futures):
            logical, code = future.result()
            results[logical] = code
            print(f"{logical}: exit {code}", flush=True)
    write_json(RUN_DIR / "operator" / "exit-codes.json", results)
    return 0 if all(code == 0 for code in results.values()) else 1


def launch(approved_hash: str) -> int:
    verify()
    session = CAMPAIGN_ID
    exists = subprocess.run(
        ["tmux", "has-session", "-t", session],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if exists.returncode == 0:
        raise RuntimeError(f"tmux session already exists: {session}")
    shell_command = (
        f"set -a; source {shlex.quote(str(ROOT / '.env'))}; set +a; "
        f"exec {shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} "
        f"run --approved-manifest-sha256 {shlex.quote(approved_hash)}"
    )
    subprocess.run(
        [
            "tmux", "new-session", "-d", "-s", session,
            "-c", str(ROOT), "bash", "-lc", shell_command,
        ],
        check=True,
    )
    print(f"Started tmux session {session}")
    return 0


def status() -> int:
    print(f"campaign={CAMPAIGN_ID}")
    for logical in MODELS:
        roots = sorted((RUN_DIR / "raw" / logical).rglob("result.json"))
        summaries = []
        for path in roots:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data.get("stats"), dict):
                summaries.append(data["stats"])
        print(f"{logical}: summaries={len(summaries)} {summaries[-1] if summaries else ''}")
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
    if args.command == "preflight":
        preflight()
        return 0
    if args.command == "run":
        return run(args.approved_manifest_sha256)
    if args.command == "launch":
        return launch(args.approved_manifest_sha256)
    return status()


if __name__ == "__main__":
    raise SystemExit(main())
