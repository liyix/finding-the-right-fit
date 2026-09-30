#!/usr/bin/env python3
"""Frozen five-model DSH qualification on one TUA-Bench task."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pilot"))

import tb4_dsh_glm as harbor_base
from integrations.ale_agents.study_deepseek.config import (
    DSH_MODEL_SPECS,
    build_harbor_dataset_config,
)


CAMPAIGN_ID = "pilot-tua-dsh-five-model-qualification-20260902-r3"
RUN_DIR = ROOT / "runs" / CAMPAIGN_ID
TASK_ROOT = ROOT / "vendor" / "tua-bench" / "tasks"
TASK = "106-create-charles-ssh-user"
HARBOR_VERSION = "0.22.0"
DSH_VERSION = "0.1.1-rc.2"
CONCURRENCY = 5
HARBOR_BIN = Path("/tmp/harbor-v0.22.0/.venv/bin/harbor")

MODELS = {
    "claude-opus-5": "anthropic/claude-opus-5",
    "gpt-6-astra": "openai/gpt-6-astra",
    "glm-5.3": "z-ai/glm-5.3",
    "kimi-k3": "moonshotai/kimi-k3",
    "deepseek-v4-pro": "deepseek/deepseek-v4-pro-0813",
}

EXPECTED_ENDPOINTS = {
    "claude-opus-5": ("anthropic/claude-opus-5-20260723", "Anthropic"),
    "gpt-6-astra": ("openai/gpt-6-astra-20260903", "OpenAI"),
    "glm-5.3": ("z-ai/glm-5.3-20260816", "Z.AI"),
    "kimi-k3": ("moonshotai/kimi-k3-20260715", "Moonshot AI"),
    "deepseek-v4-pro": ("deepseek/deepseek-v4-pro-20260813", "DeepSeek"),
}


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any, *, mode: int = 0o444) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    path.chmod(mode)


def job_name(logical: str) -> str:
    return f"tua--deepseek-harness--{logical}--{TASK}--rep-1"


def harbor_command(config_path: Path, *, print_config: bool = False) -> list[str]:
    return [
        str(HARBOR_BIN), "run", "--config", str(config_path),
        "--print-config" if print_config else "--yes",
    ]


def harbor_config(logical: str) -> dict[str, Any]:
    model = MODELS[logical]
    spec = DSH_MODEL_SPECS[model]
    return build_harbor_dataset_config(
        job_name=job_name(logical),
        jobs_dir=RUN_DIR / "raw" / "harbor",
        dataset_path=TASK_ROOT,
        task_names=[TASK],
        model_id=model,
        route=spec["route"],
        context_window=spec["context"],
        max_tokens=spec["max_output"],
        input_modalities=spec["input"],
        compat=spec["compat"],
        cache_retention=spec.get("cache_retention"),
        agent_setup_timeout_multiplier=2.0,
    )


def source_hashes() -> dict[str, str]:
    paths = [
        Path(__file__).resolve(),
        ROOT / "experiment.yaml",
        ROOT / "pyproject.toml",
        ROOT / "uv.lock",
        ROOT / "integrations" / "harbor_deepseek.py",
        ROOT / "integrations" / "openrouter_body_injector.mjs",
        ROOT / "integrations" / "patches" / "dsh-0.1.1-rc.2-headless-standard.patch",
        ROOT / "integrations" / "ale_agents" / "study_deepseek" / "config.py",
    ]
    return {str(path.relative_to(ROOT)): sha256_file(path) for path in paths}


def build_manifest() -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema_version": 1,
        "campaign_id": CAMPAIGN_ID,
        "kind": "paid-tua-lifecycle-qualification-pilot-not-primary-scores",
        "approval": {
            "status": "explicitly approved by user on 2026-09-02",
            "instruction": "ALE task data is unavailable; run TUA only",
            "reviewed_parent_manifest_sha256": (
                "f4fefe66d39b94a5de2c363595e31fef03666937df8a6c861bfc47467d76f1ad"
            ),
            "scope_change": "strict removal of all ALE trials; TUA controls unchanged",
        },
        "benchmark": {
            "name": "TUA-Bench",
            "commit": "3497fd320abcafaf4797424192c891a593fd7964",
            "task": TASK,
            "task_toml_sha256": sha256_file(TASK_ROOT / TASK / "task.toml"),
            "instruction_sha256": sha256_file(TASK_ROOT / TASK / "instruction.md"),
            "replicate": 1,
            "planned_trials": 5,
            "primary_scores": False,
        },
        "runner": {
            "name": "Harbor",
            "version": HARBOR_VERSION,
            "commit": "4407eb5",
            "entry": str(HARBOR_BIN),
            "entry_sha256": sha256_file(HARBOR_BIN),
            "install_source": "dedicated local venv pinned to harbor==0.22.0",
            "integration": "custom thin installed-agent adapter",
            "dataset_path": str(TASK_ROOT),
            "n_attempts": 1,
            "outer_trial_retries": 0,
        },
        "harness": {
            "name": "DeepSeek Harness",
            "version": DSH_VERSION,
            "install": "npm @deepseek-ai/dsh@0.1.1-rc.2",
            "upstream_commit": "b150a551b8d465e31e418e1b2eaf5e79bbb7d28e",
            "entry": "dsh --profile headless --patch <resolved> <task>",
            "profile": "headless one-shot launcher",
            "agent_preset": "unmodified upstream standard",
            "adapter": "integrations.harbor_deepseek:DeepSeekHarness",
            "standard_patch_sha256": sha256_file(
                ROOT / "integrations/patches/dsh-0.1.1-rc.2-headless-standard.patch"
            ),
            "injector_sha256": sha256_file(
                ROOT / "integrations/openrouter_body_injector.mjs"
            ),
        },
        "models": [
            {
                "logical": logical,
                "requested_model": model,
                "expected_actual_model": EXPECTED_ENDPOINTS[logical][0],
                "expected_provider": EXPECTED_ENDPOINTS[logical][1],
                "base_url": "https://openrouter.ai/api/v1",
                "protocol": "OpenAI Chat Completions with SSE",
                "path_classification": "provider-compatible; no protocol translation",
                "route": DSH_MODEL_SPECS[model]["route"],
                "context_tokens": DSH_MODEL_SPECS[model]["context"],
                "max_output_tokens": DSH_MODEL_SPECS[model]["max_output"],
                "input_modalities": DSH_MODEL_SPECS[model]["input"],
                "cache_retention": DSH_MODEL_SPECS[model].get("cache_retention"),
            }
            for logical, model in MODELS.items()
        ],
        "controls": {
            "reasoning_effort": (
                "high via pi-ai provider reasoning=high and model reasoningEfforts mapping"
            ),
            "temperature_top_p_seed": "omitted; provider defaults",
            "max_turns": "standard preset native/unset",
            "compaction": "DSH standard native; audit every event",
            "budget_management": "no harness dollar budget and no campaign dollar stop",
            "tools": "upstream standard catalog (26 initially)",
            "skills": [],
            "mcp_servers": [],
            "subagents": "standard-native; no override",
            "memory": "fresh DSH_HOME/session per trial",
            "extra_prompt_or_benchmark_augmentation": False,
            "permission_mode": "danger-full-access inside Harbor task container",
            "auxiliary_calls": "default title call retained and billed",
        },
        "timeouts_and_retries": {
            "task_agent_seconds": 2400,
            "verifier_seconds": 2400,
            "environment_build_seconds": 2400,
            "agent_setup_seconds": 720,
            "harbor_agent_override": None,
            "timeout_multiplier": 1.0,
            "dsh_llm_request_seconds": 600,
            "dsh_llm_idle_seconds": 300,
            "injector_startup_seconds": 20,
            "injector_retries": 0,
            "dsh_provider_retries": (
                "native normal policy, up to 5 with exponential backoff/jitter"
            ),
            "whole_trial_retries": 0,
        },
        "sandbox": {
            "provider": "Harbor Docker",
            "cpus": 1,
            "memory_mb": 2048,
            "storage_mb": 10240,
            "gpus": 0,
            "network": "task-native allow_internet=true",
            "delete_after_collection": True,
        },
        "concurrency": {
            "campaign": CONCURRENCY,
            "per_model_provider_trial": 1,
            "pacing": "five model routes start once; no duplicate provider arm",
            "rate_limit_rule": "429 is infrastructure failure; no same-run whole-trial retry",
        },
        "accounting": {
            "primary": "OpenRouter settled per-generation route/token/cost metadata",
            "secondary": "DSH native usage and Harbor aggregate, source-qualified only",
            "litellm": "not present",
            "double_count_rule": "never sum duplicate views of one request",
            "estimated_total_usd": "$0.25-$8; trajectory length is uncertain",
            "hard_dollar_stop": None,
        },
        "required_post_run_audit": [
            "actual model/provider/generation ID, quantization route and fallback",
            "standard preset/tool catalog and tool-call/result closure",
            "reasoning continuity, cache write/read, compaction and output-cap events",
            "429/5xx, native retries, title calls, timeouts and verifier validity",
            "provider/native/Harbor token-cost reconciliation without double counting",
            "no secret, skill, prompt, MCP or modality contamination",
        ],
        "known_limits": {
            "qualification_only": True,
            "selected_task_is_text_only": True,
            "multimodal_not_qualified": True,
            "compaction_recovery_only_if_triggered": True,
        },
        "source_sha256": source_hashes(),
        "resolved_configs": {
            logical: {
                "path": f"configs/{logical}.json",
                "sha256": hashlib.sha256(canonical(harbor_config(logical))).hexdigest(),
            }
            for logical in MODELS
        },
    }
    manifest = dict(body)
    manifest["manifest_sha256"] = hashlib.sha256(canonical(body)).hexdigest()
    return manifest


def materialize() -> dict[str, Any]:
    manifest_path = RUN_DIR / "manifest.json"
    if manifest_path.exists():
        return json.loads(manifest_path.read_text())
    if RUN_DIR.exists():
        raise RuntimeError(f"refusing partially existing run directory: {RUN_DIR}")
    manifest = build_manifest()
    for logical in MODELS:
        write_json(RUN_DIR / "configs" / f"{logical}.json", harbor_config(logical))
    write_json(manifest_path, manifest)
    return manifest


def verify_frozen(manifest: dict[str, Any]) -> None:
    recorded = manifest["manifest_sha256"]
    body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if hashlib.sha256(canonical(body)).hexdigest() != recorded:
        raise RuntimeError("manifest hash mismatch")
    if source_hashes() != manifest["source_sha256"]:
        raise RuntimeError("relevant source changed after freeze")
    if (
        not HARBOR_BIN.is_file()
        or sha256_file(HARBOR_BIN) != manifest["runner"]["entry_sha256"]
    ):
        raise RuntimeError("pinned Harbor executable changed after freeze")
    for logical, item in manifest["resolved_configs"].items():
        path = RUN_DIR / item["path"]
        if (
            not path.is_file()
            or hashlib.sha256(canonical(json.loads(path.read_text()))).hexdigest()
            != item["sha256"]
        ):
            raise RuntimeError(f"frozen config hash mismatch: {logical}")


def preflight() -> int:
    manifest = materialize()
    verify_frozen(manifest)
    version = subprocess.run(
        [str(HARBOR_BIN), "--version"], capture_output=True, text=True, timeout=30
    )
    if version.returncode != 0 or version.stdout.strip() != HARBOR_VERSION:
        raise RuntimeError(f"Harbor version drift: {version.stdout.strip()}")
    env = harbor_base.tool_env("preflight-dummy")
    for logical, model in MODELS.items():
        config_path = RUN_DIR / "configs" / f"{logical}.json"
        frozen = json.loads(config_path.read_text())
        if (
            frozen["retry"]["max_retries"] != 0
            or frozen["timeout_multiplier"] != 1.0
            or frozen["agent_timeout_multiplier"] != 1.0
            or frozen["verifier_timeout_multiplier"] != 1.0
            or frozen["environment_build_timeout_multiplier"] != 1.0
            or frozen["agent_setup_timeout_multiplier"] != 2.0
        ):
            raise RuntimeError(f"frozen runner control drift: {logical}")
        result = subprocess.run(
            harbor_command(
                config_path, print_config=True
            ),
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Harbor config resolution failed: {logical}")
        resolved = json.loads(result.stdout)
        agent = resolved["agents"][0]
        kwargs = agent["kwargs"]
        spec = DSH_MODEL_SPECS[model]
        if (
            agent["model_name"] != f"openrouter/{model}"
            or kwargs["reasoning_effort"] != "high"
            or kwargs["openrouter_route"] != spec["route"]
            or kwargs["context_window"] != spec["context"]
            or kwargs["max_tokens"] != spec["max_output"]
            or resolved["agent_setup_timeout_multiplier"] != 2.0
        ):
            raise RuntimeError(f"resolved config drift: {logical}")
    print("Zero-cost preflight passed for five DSH TUA trials.")
    print(f"Manifest SHA   {manifest['manifest_sha256']}")
    print("No model generation was made.")
    return 0


def wait_process(process: subprocess.Popen[Any], timeout: int) -> int:
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        return 124


def write_state(state: dict[str, Any], lock: threading.Lock) -> None:
    with lock:
        path = RUN_DIR / "run-state.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
        os.replace(temporary, path)


def run_cell(
    logical: str, api_key: str, state: dict[str, Any], lock: threading.Lock
) -> int:
    state["cells"][logical] = {"status": "running"}
    write_state(state, lock)
    log_path = RUN_DIR / "logs" / f"{logical}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            harbor_command(RUN_DIR / "configs" / f"{logical}.json"),
            cwd=ROOT,
            env=harbor_base.tool_env(api_key),
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        rc = wait_process(process, 3 * 60 * 60)
    state["cells"][logical] = {"status": "finished", "return_code": rc}
    write_state(state, lock)
    return rc


def run(approved_hash: str) -> int:
    manifest = materialize()
    verify_frozen(manifest)
    if approved_hash != manifest["manifest_sha256"]:
        raise RuntimeError("approved hash does not match frozen manifest")
    if (RUN_DIR / "run-state.json").exists() or (RUN_DIR / "raw").exists():
        raise RuntimeError("refusing to reuse an already-started output directory")
    api_key = harbor_base.load_key()
    before = harbor_base.common.current_key_usage(api_key)
    write_json(RUN_DIR / "openrouter-usage-before.json", before, mode=0o600)
    state: dict[str, Any] = {
        "campaign_id": CAMPAIGN_ID,
        "manifest_sha256": approved_hash,
        "status": "running",
        "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cells": {},
    }
    lock = threading.Lock()
    write_state(state, lock)
    return_codes: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = {
            pool.submit(run_cell, logical, api_key, state, lock): logical
            for logical in MODELS
        }
        for future in as_completed(futures):
            logical = futures[future]
            try:
                return_codes[logical] = future.result()
            except Exception as exc:
                return_codes[logical] = 125
                state["cells"][logical] = {
                    "status": "launcher-error",
                    "error": f"{type(exc).__name__}: {exc}",
                }
                write_state(state, lock)
    after = harbor_base.common.current_key_usage(api_key)
    write_json(RUN_DIR / "openrouter-usage-after.json", after, mode=0o600)
    state.update(
        {
            "status": "finished",
            "finished_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "return_codes": return_codes,
            "trajectory_audit_required": True,
        }
    )
    write_state(state, lock)
    return 0 if all(value == 0 for value in return_codes.values()) else 1


def launch(approved_hash: str) -> int:
    if subprocess.run(
        ["tmux", "has-session", "-t", CAMPAIGN_ID], capture_output=True
    ).returncode == 0:
        raise RuntimeError(f"tmux session already exists: {CAMPAIGN_ID}")
    command = (
        f"exec {shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} "
        f"run --approved-manifest-sha256 {shlex.quote(approved_hash)}"
    )
    subprocess.run(
        [
            "tmux", "new-session", "-d", "-s", CAMPAIGN_ID, "-c", str(ROOT),
            "bash", "-lc", command,
        ],
        check=True,
    )
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
        print(materialize()["manifest_sha256"])
        return 0
    if args.command == "preflight":
        return preflight()
    if args.command == "run":
        return run(args.approved_manifest_sha256)
    return launch(args.approved_manifest_sha256)


if __name__ == "__main__":
    raise SystemExit(main())
