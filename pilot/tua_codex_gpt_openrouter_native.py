#!/usr/bin/env python3
"""One-task TUA-Bench lifecycle qualification for stock Codex × GPT."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

import tb4_codex_gpt_openrouter_native as base


BASE_RUN_OFFLINE_CHECKS = base.run_offline_checks


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = Path(__file__).resolve()
CAMPAIGN_ID = "pilot-tua-codex-gpt-openai-pin-native-20260901-r5"
RUN_DIR = ROOT / "runs" / CAMPAIGN_ID
HARBOR_DIR = RUN_DIR / "harbor"
TASK = "106-create-charles-ssh-user"
BENCHMARK_ROOT = ROOT / "vendor" / "tua-bench"
TUA_COMMIT = "3497fd320abcafaf4797424192c891a593fd7964"
HARBOR_TASK_DIGEST = "sha256:d9d5390f5b8388bc960efba88f8b8aeffbcc8b0baa5482d14ed1cd931bdb0d82"
CODEX_VERSION = "0.150.1"
CODEX_CACHE_PREFIX = ROOT / ".cache" / "codex" / f"npm-{CODEX_VERSION}"
CODEX_VENDOR_DIR = (
    CODEX_CACHE_PREFIX
    / "node_modules/@openai/codex/node_modules/@openai/codex-linux-x64/vendor/"
    "x86_64-unknown-linux-musl"
)
CODEX_BINARY = CODEX_VENDOR_DIR / "bin/codex"
CODEX_CODE_MODE_HOST = CODEX_VENDOR_DIR / "bin/codex-code-mode-host"
CODEX_CONTAINER_ROOT = f"/opt/codex-{CODEX_VERSION}"


def _ensure_codex_cache() -> dict[str, str]:
    """Materialize the exact stock npm binary outside benchmark containers."""
    if not CODEX_BINARY.exists() or not CODEX_CODE_MODE_HOST.exists():
        CODEX_CACHE_PREFIX.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            [
                "npm", "install", "--prefix", str(CODEX_CACHE_PREFIX),
                f"@openai/codex@{CODEX_VERSION}", "--ignore-scripts",
                "--no-audit", "--no-fund",
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10 * 60,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError("Codex binary cache install failed:\n" + completed.stdout[-6000:])
    version = subprocess.run(
        [str(CODEX_BINARY), "--version"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
        check=False,
    )
    if version.returncode != 0 or f"codex-cli {CODEX_VERSION}" not in version.stdout:
        raise RuntimeError("cached Codex binary does not resolve to the frozen version")
    entrypoint = RUN_DIR / "codex-entrypoint"
    content = f"#!/bin/sh\nexec {CODEX_CONTAINER_ROOT}/bin/codex \"$@\"\n"
    if entrypoint.exists() and entrypoint.read_text() != content:
        raise RuntimeError("refusing to mutate the frozen Codex entrypoint")
    entrypoint.parent.mkdir(parents=True, exist_ok=True)
    if not entrypoint.exists():
        entrypoint.write_text(content)
    entrypoint.chmod(0o755)
    return {
        "version": CODEX_VERSION,
        "binary_sha256": base.common.file_sha256(CODEX_BINARY),
        "code_mode_host_sha256": base.common.file_sha256(CODEX_CODE_MODE_HOST),
        "entrypoint_sha256": base.common.file_sha256(entrypoint),
    }


def overlay() -> dict[str, Any]:
    return {
        "services": {
            "main": {
                "extra_hosts": ["host.docker.internal:host-gateway"],
                "volumes": [
                    f"{RUN_DIR / 'codex-entrypoint'}:/usr/local/bin/codex:ro",
                    f"{CODEX_VENDOR_DIR}:{CODEX_CONTAINER_ROOT}:ro",
                ],
            }
        }
    }


def run_offline_checks() -> dict[str, Any]:
    evidence = BASE_RUN_OFFLINE_CHECKS()
    process, process_log, _ = base.start_injector()
    try:
        completed = subprocess.run(
            [
                "docker", "run", "--rm", "--network", "bridge",
                "--add-host", "host.docker.internal:host-gateway",
                base.IMAGE,
                "python", "-c",
                (
                    "import urllib.request; "
                    f"r=urllib.request.urlopen('http://host.docker.internal:{base.PORT}/health', timeout=5); "
                    "assert r.status == 204"
                ),
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=60,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError("Docker host-gateway health check failed:\n" + completed.stdout[-3000:])
    finally:
        base.stop_injector(process, process_log)
    return {
        **evidence,
        "docker_host_gateway_health": "passed",
        "listen_host": base.INJECTOR_LISTEN_HOST,
    }


def _tree_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        digest.update(item.relative_to(path).as_posix().encode())
        digest.update(b"\0")
        digest.update(item.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def harbor_config(*, install_only: bool = False, jobs_dir: Path | None = None) -> dict[str, Any]:
    return {
        "job_name": (
            "qualification-tua-codex-gpt-native-install"
            if install_only else "tua--codex--gpt-6-astra--rep-1"
        ),
        "jobs_dir": str(jobs_dir or HARBOR_DIR),
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
            "extra_docker_compose": [str(RUN_DIR / "host-gateway.compose.json")],
        },
        "agents": [{
            "import_path": "integrations.harbor_codex:NativeGPTCodex",
            "model_name": base.INCOMING_MODEL,
            "n_concurrent": 1,
            "skills": [],
            "resume_trajectory": False,
            "extra_allowed_hosts": ["host.docker.internal"],
            "include_logs": ["**/*"],
            "kwargs": {
                "version": "0.150.1",
                "reasoning_effort": "high",
                "reasoning_summary": "none",
            },
            "env": {
                "OPENAI_API_KEY": "${OPENROUTER_API_KEY}",
                "OPENAI_BASE_URL": f"http://host.docker.internal:{base.PORT}/v1",
            },
            "mcp_servers": [],
        }],
        "datasets": [{
            "path": str(BENCHMARK_ROOT / "tasks"),
            "task_names": [TASK],
        }],
        "tasks": [],
        "artifacts": [],
        "extra_instruction_paths": [],
        "extra_instructions": [],
    }


def manifest() -> dict[str, Any]:
    binary = _ensure_codex_cache()
    task_dir = BENCHMARK_ROOT / "tasks" / TASK
    task_toml = task_dir / "task.toml"
    instruction = task_dir / "instruction.md"
    data: dict[str, Any] = {
        "campaign_id": CAMPAIGN_ID,
        "kind": "paid-real-benchmark-qualification-not-primary-result",
        "benchmark": {
            "name": "TUA-Bench",
            "commit": TUA_COMMIT,
            "task": TASK,
            "harbor_task_digest": HARBOR_TASK_DIGEST,
            "task_toml_sha256": base.common.file_sha256(task_toml),
            "instruction_sha256": base.common.file_sha256(instruction),
            "task_tree_sha256": _tree_sha256(task_dir),
            "replicate": 1,
            "planned_trials": 1,
        },
        "runner": {"name": "Harbor", "version": "0.22.0", "commit": "4407eb5"},
        "harness": {
            "name": "Codex CLI",
            "version": "0.150.1",
            "install": "npm @openai/codex@0.150.1 through Harbor built-in installer",
            "integration": "Harbor built-in plus install-lock-only NativeGPTCodex subclass",
            "entry": (
                "codex exec --dangerously-bypass-approvals-and-sandbox "
                "--skip-git-repo-check --model gpt-6-astra --json "
                "--enable unified_exec -c model_reasoning_effort=high "
                "-c model_reasoning_summary=none"
            ),
            "catalog": "unmodified bundled gpt-6-astra entry",
        },
        "model_path": {
            "requested_to_codex": base.INCOMING_MODEL,
            "requested_to_openrouter": base.UPSTREAM_MODEL,
            "expected_actual_model": base.EXPECTED_ACTUAL_MODEL,
            "provider": "OpenRouter pinned OpenAI official endpoint",
            "base_url": "https://openrouter.ai/api/v1",
            "protocol": "OpenAI Responses end to end; no protocol translation",
            "classification": "provider-compatible gateway for native Codex×GPT",
            "route": base.ROUTE,
            "quantization": "unset; OpenAI official endpoint",
        },
        "compatibility_layer": {
            "image": base.IMAGE,
            "image_id": base.IMAGE_ID,
            "sidecar": "integrations/openrouter_body_injector.mjs in inject mode",
            "changed_request_fields": ["model", "provider"],
            "response_changes": [],
            "retries": 0,
            "timeout": None,
            "output_cap": None,
            "protocol_translation": False,
            "listen_scope": (
                "0.0.0.0 only for the controller lifetime so the task Docker "
                "host-gateway can reach the sidecar; the sidecar does not inject credentials"
            ),
            "install_acceleration": {
                "kind": "read-only mount of the exact stock npm executable bundle",
                "container_root": CODEX_CONTAINER_ROOT,
                "behavior_change": False,
                **binary,
            },
        },
        "controls": {
            "reasoning_effort": "high; exact wire reasoning.effort=high required",
            "reasoning_context": "bundled Responses Lite default all_turns",
            "reasoning_summary": "none",
            "context": {
                "provider_advertised": 1_050_000,
                "codex_bundled_context_window": 272_000,
                "codex_bundled_max_context_window": 872_000,
                "effective_context_window_percent": 95,
                "study_override": None,
            },
            "output": {
                "provider_advertised_max": 128_000,
                "codex_configured": None,
                "required_wire_field": "max_output_tokens absent",
            },
            "temperature": "unset/provider default",
            "seed": None,
            "max_turns": "unset/Codex default",
            "compaction": "Codex native; every event is a primary outcome",
            "retries": {"Codex_request": 4, "Codex_stream": 5, "sidecar": 0, "Harbor": 0},
            "timeouts_seconds": {
                "task": 2400,
                "verifier": 2400,
                "environment_build": 2400,
                "agent_setup": 720,
                "Harbor_agent_override": None,
                "sidecar": None,
                "Codex_stream": "native",
            },
            "concurrency": 1,
            "tools": "unmodified Codex Responses-Lite/code_mode_only defaults",
            "skills": [],
            "mcp_servers": [],
            "extra_prompt_or_benchmark_augmentation": False,
        },
        "sandbox": {
            "provider": "Harbor Docker",
            "task_build_context_digest": HARBOR_TASK_DIGEST,
            "runtime_image_id_policy": (
                "Harbor builds/resolves from the frozen task digest; capture the exact "
                "runtime Docker image ID from the trial lock/log after launch"
            ),
            "cpus": 1,
            "memory_mb": 2048,
            "storage_mb": 10240,
            "gpus": 0,
            "network": "task-native allow_internet=true plus host-gateway for model endpoint",
        },
        "accounting": {
            "primary": "OpenRouter generation metadata total_cost per captured generation ID",
            "secondary": ["Codex native token events", "Harbor normalized metrics"],
            "double_count_rule": "never sum duplicate views of one request",
            "estimated_incremental_cost_usd": "$0.50-$3.00; high uncertainty",
            "operator_alert_usd": 3.0,
            "hard_dollar_cap": None,
        },
        "qualification_criteria": [
            "Harbor reaches the TUA verifier and preserves native plus normalized trajectories",
            "every generation resolves to OpenAI and the expected actual model",
            "every request carries the frozen provider route and high reasoning",
            "tool calls/results/replay close without protocol errors or hidden sidecar retries",
            "wire max_output_tokens remains absent and compaction/output behavior is audited",
        ],
        "maximum_claim": (
            "Exact Codex 0.150.1 × GPT-5.6-Sol route completes one TUA Harbor lifecycle; "
            "this is not a formal score or concurrency/compaction qualification."
        ),
        "post_run_audit": "mandatory full trajectory and provider audit",
        "source_sha256": {
            str(SCRIPT_PATH.relative_to(ROOT)): base.common.file_sha256(SCRIPT_PATH),
            "pilot/tb4_codex_gpt_openrouter_native.py": base.common.file_sha256(
                ROOT / "pilot/tb4_codex_gpt_openrouter_native.py"
            ),
            "integrations/harbor_codex.py": base.common.file_sha256(
                ROOT / "integrations/harbor_codex.py"
            ),
            "integrations/openrouter_body_injector.mjs": base.common.file_sha256(
                ROOT / "integrations/openrouter_body_injector.mjs"
            ),
        },
    }
    data["manifest_sha256"] = hashlib.sha256(base.common.canonical(data)).hexdigest()
    return data


# Reuse the already-audited native Codex controller while replacing only the
# campaign, benchmark config, and manifest resolved above.
base.CAMPAIGN_ID = CAMPAIGN_ID
base.RUN_DIR = RUN_DIR
base.HARBOR_DIR = HARBOR_DIR
base.TASK = TASK
base.SCRIPT_PATH = SCRIPT_PATH
base.INJECTOR_LISTEN_HOST = "0.0.0.0"
base.overlay = overlay
base.run_offline_checks = run_offline_checks
base.harbor_config = harbor_config
base.manifest = manifest


if __name__ == "__main__":
    raise SystemExit(base.main())
