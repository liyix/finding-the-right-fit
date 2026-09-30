#!/usr/bin/env python3
"""TUA lifecycle qualification for registered Claude Code and PI cells.

This campaign reuses Harbor 0.22.0's built-in agents and the already qualified
TB4 provider controls.  It runs two waves: Claude Code x Claude first, then the
five PI model lanes in parallel.  Qualification results never enter primary
benchmark scores.
"""

from __future__ import annotations

import argparse
import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from typing import Any
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import claude_code_final_readiness as cc
import provider_smoke as smoke
import tb4_pi_glm as pi_legacy
import tb4_pi_openrouter_readiness as pi_base


ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN_ID = "pilot-tua-claude-pi-readiness-20260902-r1"
RUN_DIR = ROOT / "runs" / CAMPAIGN_ID
HARBOR_DIR = RUN_DIR / "harbor"
TASK = "106-create-charles-ssh-user"
TUA_ROOT = ROOT / "vendor" / "tua-bench"
TUA_COMMIT = "3497fd320abcafaf4797424192c891a593fd7964"
TASK_TREE_SHA256 = "04179f8784a1b791ca8c8cf178fc8d328c1b5f726913abf25c47b6fa90f7e62d"
ASSET_SHA256 = "e043bf796f027ab758f976fc19ba1f199d709951720f62be0adb68046f45ed3a"
TASK_DIGEST = "sha256:d9d5390f5b8388bc960efba88f8b8aeffbcc8b0baa5482d14ed1cd931bdb0d82"
HARBOR_VERSION = "0.22.0"
HARBOR_COMMIT = "4407eb5"
CC_VERSION = "2.1.251"
PI_VERSION = "0.84.4"
SETUP_MULTIPLIER = 2.0
TASK_MULTIPLIER = 1.0
CELL_WATCHDOG_SECONDS = 2 * 60 * 60
CC_INJECTOR_PORT = 4510
TUA_PREFLIGHT_IMAGE = "106-create-charles-ssh-user__7yxrwpo__env-main:latest"
TUA_PREFLIGHT_IMAGE_ID = "sha256:3fb742c94d07c4c5399e4547c77fd60e3d50ccc83de25092718e96fe7144c5fe"

PI_REMOTE_DIR = pi_base.PI_REMOTE_DIR
PI_MODEL_SPECS = copy.deepcopy(pi_base.MODEL_SPECS)
PI_MODEL_SPECS["glm-5.3"] = {
    "model": "z-ai/glm-5.3",
    "expected_model": "z-ai/glm-5.3-20260816",
    "provider_name": "Z.AI",
    "route": {
        "only": ["z-ai/fp8"],
        "quantizations": ["fp8"],
        "allow_fallbacks": False,
        "require_parameters": True,
    },
    "quantization": "fp8",
    "pi_context_window": 1_048_576,
    "pi_max_tokens": 131_072,
    "endpoint_max_tokens": 131_072,
    "list_context": "1.0M",
    "list_max_out": "131.1K",
    "input": ["text", "image"],
}
# Reuse PI's existing catalog verifier against this campaign's resolved
# five-model table (the imported TB4 module contains only four entries).
pi_base.MODEL_SPECS = PI_MODEL_SPECS

# Reuse the exact registered Claude configuration and injector implementation.
CC_MODELS = {"claude-opus-5": copy.deepcopy(cc.MODELS["claude-opus-5"])}
cc.CAMPAIGN_ID = CAMPAIGN_ID
cc.RUN_DIR = RUN_DIR
cc.JOBS_DIR = HARBOR_DIR
cc.MODELS = CC_MODELS
cc.INJECTOR_BASE_PORT = CC_INJECTOR_PORT
cc.PER_MODEL_CONCURRENCY = 1


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    path.chmod(mode)


def task_metadata() -> dict[str, Any]:
    task_dir = TUA_ROOT / "tasks" / TASK
    task = tomllib.loads((task_dir / "task.toml").read_text())
    return {
        "name": TASK,
        "task_digest": TASK_DIGEST,
        "task_toml_sha256": sha256_file(task_dir / "task.toml"),
        "instruction_sha256": sha256_file(task_dir / "instruction.md"),
        "agent_timeout_seconds": task["agent"]["timeout_sec"],
        "verifier_timeout_seconds": task["verifier"]["timeout_sec"],
        "build_timeout_seconds": task["environment"]["build_timeout_sec"],
        "cpus": task["environment"]["cpus"],
        "memory_mb": task["environment"]["memory_mb"],
        "storage_mb": task["environment"]["storage_mb"],
        "gpus": task["environment"]["gpus"],
        "allow_internet": task["environment"]["allow_internet"],
        "mcp_servers": task["environment"]["mcp_servers"],
    }


def common_job(job_name: str, jobs_dir: Path) -> dict[str, Any]:
    return {
        "job_name": job_name,
        "jobs_dir": str(jobs_dir),
        "n_attempts": 1,
        "install_only": False,
        "timeout_multiplier": TASK_MULTIPLIER,
        "agent_timeout_multiplier": TASK_MULTIPLIER,
        "verifier_timeout_multiplier": TASK_MULTIPLIER,
        "environment_build_timeout_multiplier": TASK_MULTIPLIER,
        "agent_setup_timeout_multiplier": SETUP_MULTIPLIER,
        "n_concurrent_trials": 1,
        "retry": {"max_retries": 0},
        "environment": {
            "type": "docker",
            "force_build": False,
            # Keep exact campaign containers only until the controller records
            # a sanitized live inspect; cleanup follows immediately afterward.
            "delete": False,
            "cpu_enforcement_policy": "auto",
            "memory_enforcement_policy": "auto",
        },
        "datasets": [{"path": str(TUA_ROOT / "tasks"), "task_names": [TASK]}],
        "tasks": [],
        "artifacts": [],
        "extra_instruction_paths": [],
        "extra_instructions": [],
    }


def cc_job_name() -> str:
    return f"tua--claude-code--claude-opus-5--{TASK}--rep-1"


def cc_config(*, overlay_path: Path, jobs_dir: Path = HARBOR_DIR) -> dict[str, Any]:
    result = common_job(cc_job_name(), jobs_dir)
    result["environment"]["extra_docker_compose"] = [str(overlay_path)]
    result["agents"] = [cc.agent_config("claude-opus-5")]
    return result


def pi_job_name(logical: str) -> str:
    return f"tua--pi--{logical}--{TASK}--rep-1"


def pi_models_json(logical: str) -> dict[str, Any]:
    spec = PI_MODEL_SPECS[logical]
    return {
        "providers": {
            "openrouter": {
                "modelOverrides": {
                    spec["model"]: {
                        "compat": {
                            "maxTokensField": "max_tokens",
                            "openRouterRouting": spec["route"],
                        }
                    }
                }
            }
        }
    }


def pi_overlay(models_path: Path) -> dict[str, Any]:
    return {
        "services": {
            "main": {
                "tmpfs": [f"{PI_REMOTE_DIR}:rw,mode=1777"],
                "volumes": [f"{models_path.resolve()}:{PI_REMOTE_DIR}/models.json:ro"],
            }
        }
    }


def pi_config(
    logical: str, *, overlay_path: Path, jobs_dir: Path = HARBOR_DIR
) -> dict[str, Any]:
    spec = PI_MODEL_SPECS[logical]
    result = common_job(pi_job_name(logical), jobs_dir)
    result["environment"]["extra_docker_compose"] = [str(overlay_path)]
    result["agents"] = [
        {
            "name": "pi",
            "model_name": f"openrouter/{spec['model']}",
            "n_concurrent": 1,
            "skills": [],
            "resume_trajectory": False,
            "include_logs": ["**/*"],
            "kwargs": {"version": PI_VERSION, "thinking": "high"},
            "env": {
                "OPENROUTER_API_KEY": "${OPENROUTER_API_KEY}",
                "PI_CODING_AGENT_DIR": PI_REMOTE_DIR,
                "PI_OFFLINE": "1",
                "PI_SKIP_VERSION_CHECK": "1",
                "PI_TELEMETRY": "0",
            },
            "mcp_servers": [],
        }
    ]
    return result


def pi_model_manifest(logical: str) -> dict[str, Any]:
    spec = PI_MODEL_SPECS[logical]
    return {
        "requested_model_id": spec["model"],
        "expected_actual_model": spec["expected_model"],
        "provider": spec["provider_name"],
        "base_url": "https://openrouter.ai/api/v1",
        "protocol": "OpenAI Chat Completions",
        "classification": "PI-native OpenRouter provider; no protocol translation",
        "route": spec["route"],
        "quantization": spec["quantization"],
        "pi_context_window": spec["pi_context_window"],
        "pi_bundled_max_output": spec["pi_max_tokens"],
        "endpoint_max_output": spec["endpoint_max_tokens"],
        "wire_output_expected": {"field": "max_tokens", "value": spec["pi_max_tokens"]},
        "modalities": spec["input"],
    }


def install_evidence() -> dict[str, Any]:
    root = ROOT / "runs" / "pilot-tua-install-only-20260901-r1"
    result: dict[str, Any] = {}
    for harness, stamp in (("claude-code", "2026-09-01__20-10-43"), ("pi", "2026-09-01__20-10-44")):
        path = root / harness / stamp / "result.json"
        result[harness] = {
            "path": str(path.relative_to(ROOT)),
            "sha256": sha256_file(path),
            "completed": json.loads(path.read_text())["stats"]["n_completed_trials"],
            "errors": json.loads(path.read_text())["stats"]["n_errored_trials"],
        }
    return result


def manifest_body() -> dict[str, Any]:
    cc_overlay = RUN_DIR / "configs" / "claude-code.compose.json"
    pi_configs = {
        logical: pi_config(
            logical,
            overlay_path=RUN_DIR / "configs" / f"pi-{logical}.compose.json",
        )
        for logical in PI_MODEL_SPECS
    }
    task = task_metadata()
    body: dict[str, Any] = {
        "schema_version": 1,
        "campaign_id": CAMPAIGN_ID,
        "kind": "paid-text-tua-lifecycle-qualification-pilot-not-primary-score",
        "approval": {
            "required_before_launch": True,
            "target": "campaign ID plus exact manifest SHA256",
        },
        "scope": {
            "benchmark": "TUA-Bench",
            "dataset_commit": TUA_COMMIT,
            "task": task,
            "task_tree_sha256": TASK_TREE_SHA256,
            "asset_manifest_sha256": ASSET_SHA256,
            "replicate": 1,
            "planned_trials": 6,
            "primary_scores": False,
            "waves": [
                {"name": "claude-code", "cells": 1, "concurrency": 1},
                {"name": "pi", "cells": 5, "concurrency": 5, "per_provider": 1},
            ],
        },
        "runner": {
            "name": "Harbor",
            "version": HARBOR_VERSION,
            "commit": HARBOR_COMMIT,
            "entry": "uvx --offline --from harbor==0.22.0 harbor run --config <frozen-config> --yes",
            "dataset_path": str(TUA_ROOT / "tasks"),
            "outer_trial_retries": 0,
            "resolved_configs": {
                "claude-code": cc_config(overlay_path=cc_overlay),
                "pi": pi_configs,
            },
            "host_runtime_lock": sha256_file(ROOT / "uv.lock"),
        },
        "harnesses": {
            "claude-code": {
                "version": CC_VERSION,
                "package": f"@anthropic-ai/claude-code@{CC_VERSION}",
                "integration": "Harbor built-in claude-code agent",
                "entry": "claude --verbose --output-format=stream-json --permission-mode=bypassPermissions --effort high --print",
                "model": "anthropic/claude-opus-5[1m]",
                "provider_receives_model": "anthropic/claude-opus-5",
                "provider": "OpenRouter pinned to Anthropic",
                "base_url": "https://openrouter.ai/api",
                "protocol": "Anthropic Messages",
                "classification": "provider-compatible; transparent injector adds only provider routing object",
                "route": CC_MODELS["claude-opus-5"]["route"],
                "reasoning": "high via Harbor reasoning_effort and Claude --effort high",
                "context": 1_000_000,
                "output": 64_000,
                "compaction": "Claude Code native policy at fixed 1M window",
                "max_turns": "unset/native",
                "budget": "no harness dollar cap",
                "native_retries": "preserved; audit actual requests",
                "tools": "Claude Code default tool surface",
                "skills": [],
                "mcp": [],
                "subagents": "native availability; aliases and subagents use same Opus identity",
                "memory": "fresh session; resume disabled",
            },
            "pi": {
                "version": PI_VERSION,
                "package": f"@earendil-works/pi-coding-agent@{PI_VERSION}",
                "integration": "Harbor built-in pi agent",
                "entry": "pi --print --mode json --provider openrouter --model <exact-id> --thinking high",
                "models": {logical: pi_model_manifest(logical) for logical in PI_MODEL_SPECS},
                "reasoning": "high via PI --thinking high -> reasoning.effort=high",
                "context_output": "PI 0.84.4 native catalog values per model; no common numeric override",
                "compaction": "native: context - 16384 trigger, reserve 16384, keep recent 20000",
                "max_turns": "unset/native",
                "budget": "no harness dollar cap",
                "native_retries": "PI max 3 with 2/4/8 second backoff",
                "tools": "PI default read/bash/edit/write coding tools",
                "skills": [],
                "extensions": [],
                "mcp": [],
                "subagents": "none in unextended PI",
                "memory": "fresh session; resume disabled",
            },
        },
        "controls": {
            "sampling": {"temperature": "omitted", "top_p": "omitted", "seed": None},
            "prompt_augmentation": False,
            "task_deadlines": "native agent/verifier/build 2400 seconds; all multipliers 1.0",
            "agent_setup": "Harbor native 360 seconds x common multiplier 2.0 = 720 seconds",
            "network": "TUA native allow_internet=true",
            "mcp": "TUA task empty; no study MCP",
            "sandbox": "Harbor Docker; task-native 1 CPU, 2048MB, 10240MB storage, 0 GPU",
            "runtime_image_audit": "keep containers through sanitized live inspect, then remove exact campaign-labeled IDs",
            "retry": "Harbor max_retries=0; no compatibility-layer retry",
        },
        "accounting": {
            "primary": "OpenRouter settled per-generation usage/cost/route metadata",
            "secondary": "native Claude Code/PI logs and Harbor result",
            "double_count": "never sum duplicate representations of one request",
            "estimated_total_usd": {"low": 0.1, "high": 8.0},
            "hard_dollar_stop": None,
            "uncertainty": "task behavior and provider settlement lag; two waves bound simultaneous exposure",
        },
        "zero_cost_evidence": {
            "benchmark_doctor": "120/120 tasks, 73/73 assets, exact hashes passed 2026-09-02",
            "oracle_result_sha256": sha256_file(
                ROOT / "runs/pilot-tua-oracle-20260901-r2/2026-09-01__20-04-52/result.json"
            ),
            "install_only": install_evidence(),
            "cached_preflight_image": {"ref": TUA_PREFLIGHT_IMAGE, "id": TUA_PREFLIGHT_IMAGE_ID},
        },
        "post_run_audit": {
            "required": True,
            "scope": "all six trajectories and every upstream request",
            "checks": [
                "strict actual model/provider/quantization/no-fallback route",
                "wire high reasoning and exact context/output behavior",
                "complete tool-call/result and reasoning replay",
                "default tools; no skills/MCP/prompt contamination",
                "retry/429/timeout/output-cap/compaction/verifier/artifact status",
                "provider versus harness versus Harbor token/cost reconciliation",
                "sanitized live container and runtime image identity before cleanup",
            ],
        },
        "source_sha256": {
            "pilot/tua_claude_pi_readiness.py": sha256_file(Path(__file__)),
            "pilot/claude_code_final_readiness.py": sha256_file(ROOT / "pilot/claude_code_final_readiness.py"),
            "pilot/claude_openrouter_pin_smoke.py": sha256_file(ROOT / "pilot/claude_openrouter_pin_smoke.py"),
            "pilot/tb4_pi_openrouter_readiness.py": sha256_file(ROOT / "pilot/tb4_pi_openrouter_readiness.py"),
            "pilot/tb4_pi_glm.py": sha256_file(ROOT / "pilot/tb4_pi_glm.py"),
            "pilot/provider_smoke.py": sha256_file(ROOT / "pilot/provider_smoke.py"),
            "harbor-claude-code.py": "3cb00b1f60aa61390f2f3e2262bf0df68b0f663322ddf5c0341a3e455e5dc006",
            "harbor-pi.py": "2a4d99fc1f229ced58bc8ded5c9795cdce7065400212cf26c3952a5ca980e4db",
        },
    }
    return body


def manifest() -> dict[str, Any]:
    body = manifest_body()
    body["manifest_sha256"] = hashlib.sha256(canonical(body)).hexdigest()
    return body


def harbor_command(path: Path, *, print_config: bool = False) -> list[str]:
    result = [
        "uvx", "--offline", "--from", f"harbor=={HARBOR_VERSION}",
        "harbor", "run", "--config", str(path),
    ]
    result.append("--print-config" if print_config else "--yes")
    return result


def tool_env(api_key: str) -> dict[str, str]:
    env = pi_legacy.tool_env(api_key)
    env["PYTHONPATH"] = str(ROOT)
    return env


def check_port_free(port: int) -> None:
    with socket.socket() as sock:
        try:
            sock.bind(("0.0.0.0", port))
        except OSError as exc:
            raise RuntimeError(f"injector port {port} unavailable: {exc}") from exc


def host_gateway_preflight() -> None:
    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
            self.send_response(204)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    port = CC_INJECTOR_PORT + 20
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        probe = subprocess.run(
            [
                "docker", "run", "--rm", "--add-host", "host.docker.internal:host-gateway",
                TUA_PREFLIGHT_IMAGE, "python3", "-c",
                "import urllib.request; assert urllib.request.urlopen("
                f"'http://host.docker.internal:{port}/').status == 204",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if probe.returncode != 0:
            raise RuntimeError("TUA image host-gateway probe failed: " + probe.stderr[-1000:])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def validate_resolved(config: dict[str, Any], expected_agent: str, model: str) -> None:
    with tempfile.TemporaryDirectory(prefix="tua-cc-pi-config-", dir="/tmp") as raw:
        path = Path(raw) / "job.json"
        write_json(path, config)
        completed = subprocess.run(
            harbor_command(path, print_config=True),
            cwd=ROOT,
            env=tool_env("preflight-dummy"),
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    if completed.returncode != 0:
        raise RuntimeError(completed.stdout[-2000:] + completed.stderr[-2000:])
    resolved = json.loads(completed.stdout)
    agent = (resolved.get("agents") or [{}])[0]
    if agent.get("name") != expected_agent or agent.get("model_name") != model:
        raise RuntimeError(f"Harbor config drift: {expected_agent}/{model}")
    if agent.get("kwargs", {}).get("version") not in {CC_VERSION, PI_VERSION}:
        raise RuntimeError(f"Harbor version drift: {agent.get('kwargs')}")
    if (
        config["retry"]["max_retries"] != 0
        or config["agent_timeout_multiplier"] != 1.0
        or config["verifier_timeout_multiplier"] != 1.0
        or config["environment_build_timeout_multiplier"] != 1.0
        or config["agent_setup_timeout_multiplier"] != 2.0
    ):
        raise RuntimeError("TUA runner controls drift")


def preflight() -> int:
    if subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=TUA_ROOT, text=True).strip() != TUA_COMMIT:
        raise RuntimeError("TUA checkout drift")
    check_port_free(CC_INJECTOR_PORT)
    cc.injector.offline_self_test()
    host_gateway_preflight()
    with tempfile.TemporaryDirectory(prefix="tua-cc-pi-preflight-", dir="/tmp") as raw:
        temp = Path(raw)
        cc_overlay = temp / "cc.compose.json"
        write_json(cc_overlay, cc.compose_overlay())
        validate_resolved(
            cc_config(overlay_path=cc_overlay, jobs_dir=temp / "jobs"),
            "claude-code",
            CC_MODELS["claude-opus-5"]["cli_model"],
        )
        for logical, spec in PI_MODEL_SPECS.items():
            models_path = temp / logical / "models.json"
            overlay_path = temp / logical / "compose.json"
            write_json(models_path, pi_models_json(logical))
            write_json(overlay_path, pi_overlay(models_path))
            validate_resolved(
                pi_config(logical, overlay_path=overlay_path, jobs_dir=temp / "jobs"),
                "pi",
                f"openrouter/{spec['model']}",
            )
            ok, detail = pi_base.static_model_check(logical, models_path)
            if not ok:
                raise RuntimeError(f"PI model metadata drift for {logical}: {detail[-2000:]}")
    print("Zero-cost preflight passed: TUA task/assets, CC injector/gateway/config, PI five configs/catalogs")
    print(f"Prospective manifest SHA256: {manifest()['manifest_sha256']}")
    print("No model API call was made")
    return 0


def freeze(approved_hash: str) -> tuple[dict[str, Path], Path, dict[str, Any]]:
    resolved = manifest()
    if approved_hash != resolved["manifest_sha256"]:
        raise RuntimeError("approved manifest hash mismatch")
    if RUN_DIR.exists():
        raise RuntimeError(f"immutable run directory already exists: {RUN_DIR}")
    configs = RUN_DIR / "configs"
    configs.mkdir(parents=True)
    cc_overlay = configs / "claude-code.compose.json"
    cc_path = configs / "claude-code.job.json"
    write_json(cc_overlay, cc.compose_overlay(), mode=0o444)
    write_json(cc_path, cc_config(overlay_path=cc_overlay), mode=0o444)
    pi_paths: dict[str, Path] = {}
    for logical in PI_MODEL_SPECS:
        models_path = configs / f"pi-{logical}.models.json"
        overlay_path = configs / f"pi-{logical}.compose.json"
        config_path = configs / f"pi-{logical}.job.json"
        write_json(models_path, pi_models_json(logical), mode=0o444)
        write_json(overlay_path, pi_overlay(models_path), mode=0o444)
        write_json(config_path, pi_config(logical, overlay_path=overlay_path), mode=0o444)
        pi_paths[logical] = config_path
    spec_path = configs / "claude-code.injector.json"
    write_json(spec_path, cc.injector_spec("claude-opus-5"), mode=0o444)
    write_json(RUN_DIR / "manifest.json", resolved, mode=0o444)
    write_json(
        RUN_DIR / "approval.json",
        {
            "campaign_id": CAMPAIGN_ID,
            "approved_manifest_sha256": approved_hash,
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        },
        mode=0o444,
    )
    return pi_paths, cc_path, resolved


def load_frozen_after_zero_call_controller_failure(
    approved_hash: str,
) -> tuple[dict[str, Path], Path, dict[str, Any]]:
    """Resume only the already frozen campaign after its pre-call controller bug."""
    manifest_path = RUN_DIR / "manifest.json"
    approval_path = RUN_DIR / "approval.json"
    if not manifest_path.exists() or not approval_path.exists():
        raise RuntimeError("frozen recovery metadata is incomplete")
    resolved = json.loads(manifest_path.read_text())
    approval = json.loads(approval_path.read_text())
    if (
        resolved.get("manifest_sha256") != approved_hash
        or approval.get("approved_manifest_sha256") != approved_hash
    ):
        raise RuntimeError("frozen recovery approval hash mismatch")
    if (RUN_DIR / "run-state.json").exists() or (RUN_DIR / "openrouter-usage-before.json").exists():
        raise RuntimeError("refusing recovery after a paid launch may have started")
    pi_paths = {
        logical: RUN_DIR / "configs" / f"pi-{logical}.job.json"
        for logical in PI_MODEL_SPECS
    }
    cc_path = RUN_DIR / "configs" / "claude-code.job.json"
    required = [cc_path, *pi_paths.values()]
    if any(not path.exists() for path in required):
        raise RuntimeError("frozen recovery config is missing")
    write_json(
        RUN_DIR / "controller-recovery.json",
        {
            "reason": "initial controller referenced a non-exported usage helper",
            "paid_model_calls_before_failure": 0,
            "frozen_configs_changed": False,
            "approved_manifest_sha256": approved_hash,
            "approved_controller_sha256": resolved["source_sha256"][
                "pilot/tua_claude_pi_readiness.py"
            ],
            "recovery_controller_sha256": sha256_file(Path(__file__)),
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        },
        mode=0o444,
    )
    return pi_paths, cc_path, resolved


def run_harbor(label: str, config_path: Path, api_key: str) -> int:
    log_path = RUN_DIR / "logs" / f"{label}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w") as log:
        process = subprocess.Popen(
            harbor_command(config_path), cwd=ROOT, env=tool_env(api_key),
            stdout=log, stderr=subprocess.STDOUT, text=True, start_new_session=True,
        )
        try:
            return process.wait(timeout=CELL_WATCHDOG_SECONDS)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            return 124


def campaign_containers() -> list[dict[str, Any]]:
    ids = subprocess.run(
        ["docker", "ps", "-aq"], capture_output=True, text=True, check=True
    ).stdout.split()
    records: list[dict[str, Any]] = []
    for container_id in ids:
        item = json.loads(subprocess.check_output(["docker", "inspect", container_id], text=True))[0]
        labels = (item.get("Config") or {}).get("Labels") or {}
        config_files = str(labels.get("com.docker.compose.project.config_files") or "")
        working_dir = str(labels.get("com.docker.compose.project.working_dir") or "")
        if str(HARBOR_DIR) not in config_files and str(HARBOR_DIR) not in working_dir:
            continue
        image_id = str(item.get("Image") or "")
        image_meta = json.loads(subprocess.check_output(["docker", "image", "inspect", image_id], text=True))[0]
        records.append(
            {
                "container_id": item.get("Id"),
                "name": str(item.get("Name") or "").lstrip("/"),
                "created": item.get("Created"),
                "image_id": image_id,
                "image_repo_digests": image_meta.get("RepoDigests") or [],
                "state": {
                    key: (item.get("State") or {}).get(key)
                    for key in ("Status", "Running", "ExitCode", "StartedAt", "FinishedAt")
                },
                "compose": {
                    key: labels.get(key)
                    for key in (
                        "com.docker.compose.project",
                        "com.docker.compose.service",
                        "com.docker.compose.project.config_files",
                    )
                },
            }
        )
    return records


def cleanup_campaign_containers(records: list[dict[str, Any]]) -> list[str]:
    removed: list[str] = []
    for record in records:
        container_id = str(record["container_id"])
        result = subprocess.run(
            ["docker", "rm", "-f", container_id], capture_output=True, text=True, check=False
        )
        if result.returncode == 0:
            removed.append(container_id)
    return removed


def trial_results(job_name: str) -> list[tuple[Path, dict[str, Any]]]:
    result: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted((HARBOR_DIR / job_name).glob("*/result.json")):
        payload = json.loads(path.read_text())
        if payload.get("task_name"):
            result.append((path.parent, payload))
    return result


def audit_trial(
    harness: str, logical: str, job_name: str, api_key: str, return_code: int
) -> dict[str, Any]:
    trials: list[dict[str, Any]] = []
    for trial_dir, result in trial_results(job_name):
        generations = smoke.fetch_openrouter_generation_records(trial_dir, api_key)
        records = generations.get("records") or []
        text_paths = [
            path for path in trial_dir.rglob("*")
            if path.is_file() and path.suffix in {".json", ".jsonl", ".log", ".txt"}
            and path.stat().st_size <= 50 * 1024 * 1024
        ]
        combined = "\n".join(path.read_text(errors="replace") for path in text_paths)
        pi_sessions = sorted(trial_dir.glob("agent/pi/sessions/**/*.jsonl"))
        session_rows = [item for path in pi_sessions for item in smoke.parse_json_lines(path)]
        messages = [item.get("message") or {} for item in session_rows if item.get("type") == "message"]
        assistant = [item for item in messages if item.get("role") == "assistant"]
        trials.append(
            {
                "trial_dir": str(trial_dir.relative_to(ROOT)),
                "task_name": result.get("task_name"),
                "reward": (result.get("verifier_result") or {}).get("rewards"),
                "exception_info": result.get("exception_info"),
                "agent_result": result.get("agent_result"),
                "generation_ids": generations.get("generation_ids_found"),
                "generation_lookup_errors": generations.get("errors"),
                "provider_records": records,
                "provider_cost_usd": sum(float(item.get("total_cost") or 0) for item in records),
                "actual_models": sorted({str(item.get("model")) for item in records if item.get("model")}),
                "actual_providers": sorted({str(item.get("provider_name")) for item in records if item.get("provider_name")}),
                "native_session_jsonl_count": sum("sessions" in path.parts and path.suffix == ".jsonl" for path in text_paths),
                "pi_tool_calls": sum(
                    sum(isinstance(block, dict) and block.get("type") == "toolCall" for block in (msg.get("content") or []))
                    for msg in assistant
                ),
                "pi_tool_results": sum(msg.get("role") == "toolResult" for msg in messages),
                "compaction_markers": combined.lower().count("compact"),
                "output_cap_markers": combined.lower().count("max_tokens") + combined.lower().count("max-tokens"),
                "rate_limit_markers": combined.lower().count("429") + combined.lower().count("rate_limit"),
            }
        )
    return {
        "harness": harness,
        "logical_model": logical,
        "harbor_return_code": return_code,
        "trials": trials,
    }


def run(approved_hash: str) -> int:
    api_key = pi_legacy.load_key()
    if RUN_DIR.exists():
        pi_paths, cc_path, resolved = load_frozen_after_zero_call_controller_failure(
            approved_hash
        )
    else:
        pi_paths, cc_path, resolved = freeze(approved_hash)
    before = pi_legacy.common.current_key_usage(api_key)
    write_json(RUN_DIR / "openrouter-usage-before.json", before, mode=0o600)
    return_codes: dict[str, int] = {}
    injector, injector_log = cc.start_injector(
        "claude-opus-5", RUN_DIR / "configs" / "claude-code.injector.json"
    )
    try:
        return_codes["claude-code/claude-opus-5"] = run_harbor(
            "claude-code--claude-opus-5", cc_path, api_key
        )
    finally:
        cc.stop_process(injector)
        injector_log.close()
    with ThreadPoolExecutor(max_workers=len(pi_paths)) as pool:
        futures = {
            pool.submit(run_harbor, f"pi--{logical}", path, api_key): logical
            for logical, path in pi_paths.items()
        }
        for future in as_completed(futures):
            logical = futures[future]
            return_codes[f"pi/{logical}"] = future.result()
    containers = campaign_containers()
    write_json(RUN_DIR / "runtime-containers.json", containers, mode=0o444)
    removed = cleanup_campaign_containers(containers)
    after = pi_legacy.common.current_key_usage(api_key)
    write_json(RUN_DIR / "openrouter-usage-after.json", after, mode=0o600)
    cells = [
        audit_trial(
            "claude-code", "claude-opus-5", cc_job_name(), api_key,
            return_codes.get("claude-code/claude-opus-5", 125),
        )
    ]
    cells.extend(
        audit_trial("pi", logical, pi_job_name(logical), api_key, return_codes.get(f"pi/{logical}", 125))
        for logical in PI_MODEL_SPECS
    )
    write_json(
        RUN_DIR / "audit.json",
        {
            "campaign_id": CAMPAIGN_ID,
            "manifest_sha256": resolved["manifest_sha256"],
            "decision": "pending mandatory full trajectory review; never primary-score data",
            "cells": cells,
            "runtime_container_records": len(containers),
            "runtime_containers_removed": removed,
        },
        mode=0o444,
    )
    write_json(
        RUN_DIR / "run-state.json",
        {
            "campaign_id": CAMPAIGN_ID,
            "manifest_sha256": resolved["manifest_sha256"],
            "return_codes": return_codes,
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        },
        mode=0o444,
    )
    print(json.dumps(return_codes, sort_keys=True))
    print("Mandatory trajectory audit remains before accepting any B qualification")
    return 0 if all(code == 0 for code in return_codes.values()) else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("manifest")
    sub.add_parser("preflight")
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--approved-manifest-sha256", required=True)
    args = parser.parse_args()
    if args.command == "manifest":
        print(json.dumps(manifest(), indent=2, ensure_ascii=False))
        return 0
    if args.command == "preflight":
        return preflight()
    return run(args.approved_manifest_sha256)


if __name__ == "__main__":
    raise SystemExit(main())
