"""Single-entry campaign preparation and execution.

This module does not implement an agent loop.  It materializes immutable jobs
for the pinned Harbor agents and ALE deployers registered in experiment.yaml,
then launches only those resolved jobs after an exact manifest approval.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import importlib.util
import json
import os
import platform
import re
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
HARBOR_VERSION = "0.22.0"
HARBOR_PLATFORMDIRS_VERSION = "4.11.6"
CODEX_VERSION = "0.150.1"
# Disjoint host-only ranges; ALE uses its independent in-sandbox fixed ports.
CODEX_PORT = 24170
CLAUDE_PORT = 25170
NODE_IMAGE = (
    "node:22-bookworm-slim@"
    "sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5"
)
ALE_PREFLIGHT_SCRIPT = """
import json, sys
from ale_run.orchestration.config_loader import load_experiment
from ale_run.orchestration.factory import build_config, resolve_agent
from ale_run.orchestration.runner import Runner
spec = load_experiment(sys.argv[1])
for agent in spec.agents:
    _, config_cls = resolve_agent(agent)
    build_config(config_cls, agent.config)
print(json.dumps({"name": spec.name, "agents": len(spec.agents),
                  "units": len(Runner(spec).enumerate_units())}))
""".strip()
RUN_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{2,95}\Z")
GENERATION_ID_RE = re.compile(r"\bgen-[A-Za-z0-9_-]+\b")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def write_json(path: Path, value: Any, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    path.chmod(mode)


def write_yaml(path: Path, value: Any, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    path.chmod(mode)


def write_state(path: Path, value: Any) -> None:
    temporary = path.with_name(path.name + ".tmp")
    write_json(temporary, value)
    os.replace(temporary, path)


def load_local(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(
            f"local config missing: {path}; copy local.yaml.example to local.yaml"
        )
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("local config must be a schema_version: 1 mapping")
    return value


def by_id(config: dict[str, Any], section: str, item_id: str) -> dict[str, Any]:
    for item in config[section]:
        if item["id"] == item_id:
            return item
    raise ValueError(f"unknown {section.rstrip('s')}: {item_id}")


def route_for(model: dict[str, Any], *, require_parameters: bool) -> dict[str, Any]:
    provider = model["openrouter"]
    route: dict[str, Any] = {
        "only": provider["provider_only"],
        "allow_fallbacks": False,
        "require_parameters": require_parameters,
    }
    if provider.get("quantizations"):
        route["quantizations"] = provider["quantizations"]
    return route


def qualification_level(harness: dict[str, Any], benchmark_id: str) -> str:
    return str(harness["qualification"][benchmark_id]["level"])


def git_state() -> dict[str, Any]:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain=v1"], cwd=ROOT, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    ).stdout
    diff = subprocess.run(
        ["git", "diff", "--binary", "HEAD"], cwd=ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    ).stdout
    untracked_raw = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    ).stdout
    untracked_paths = [
        ROOT / value.decode("utf-8", errors="strict")
        for value in untracked_raw.split(b"\0") if value
    ]
    untracked_records = [
        {
            "path": str(path.relative_to(ROOT)),
            "sha256": sha256_file(path),
        }
        for path in untracked_paths if path.is_file()
    ]
    return {
        "commit": commit,
        "clean": not bool(status.strip()),
        "status_sha256": sha256_bytes(status.encode()),
        "tracked_diff_sha256": sha256_bytes(diff),
        "untracked_files": untracked_records,
        "untracked_files_sha256": sha256_bytes(canonical(untracked_records)),
    }


def machine_state() -> dict[str, Any]:
    memory_kib: int | None = None
    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        match = re.search(r"^MemTotal:\s+(\d+)\s+kB", meminfo.read_text(), re.MULTILINE)
        memory_kib = int(match.group(1)) if match else None
    docker = subprocess.run(
        ["docker", "info", "--format", "{{json .}}"], text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    docker_info: dict[str, Any] | None = None
    if docker.returncode == 0:
        try:
            value = json.loads(docker.stdout)
            docker_info = {
                "server_version": value.get("ServerVersion"),
                "driver": value.get("Driver"),
                "docker_root_dir": value.get("DockerRootDir"),
                "cpus": value.get("NCPU"),
                "memory_bytes": value.get("MemTotal"),
            }
        except json.JSONDecodeError:
            docker_info = None
    return {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "memory_kib": memory_kib,
        "docker": docker_info,
    }


def sidecar_port(run_dir: Path, unit_id: str, base: int) -> int:
    """Give concurrent Harbor units stable, non-shared loopback ports."""
    offset = int(sha256_bytes(f"{run_dir.name}:{unit_id}".encode())[:4], 16) % 1000
    return base + offset


def task_ids(config: dict[str, Any], benchmark_id: str) -> list[str]:
    if benchmark_id == "terminal-bench-4":
        root = ROOT / "vendor" / "terminal-bench"
        values = sorted(p.parent.name for p in root.glob("*/task.toml"))
    elif benchmark_id == "tua-bench":
        root = ROOT / by_id(config, "benchmarks", benchmark_id)["tasks_path"]
        values = sorted(p.parent.name for p in root.glob("*/task.toml"))
    elif benchmark_id == "ale-cli":
        runner = config["runners"]["ale"]
        benchmark = by_id(config, "benchmarks", benchmark_id)
        path = ROOT / runner["repository_path"] / benchmark["selected_tasks_path"]
        values = [line.strip() for line in path.read_text().splitlines() if line.strip()]
    else:
        raise ValueError(f"unsupported benchmark: {benchmark_id}")
    expected = int(by_id(config, "benchmarks", benchmark_id)["tasks"])
    if len(values) != expected or len(set(values)) != expected:
        raise ValueError(
            f"{benchmark_id} task set mismatch: got {len(values)}, expected {expected}"
        )
    return values


def selected_tasks(
    config: dict[str, Any], benchmark_id: str, explicit: list[str] | None
) -> list[str]:
    all_tasks = task_ids(config, benchmark_id)
    if not explicit:
        return all_tasks
    if len(set(explicit)) != len(explicit):
        raise ValueError("selected task IDs must be unique")
    unknown = sorted(set(explicit) - set(all_tasks))
    if unknown:
        raise ValueError(f"unknown {benchmark_id} tasks: {', '.join(unknown)}")
    return explicit


def ale_local_docker_tasks(config: dict[str, Any]) -> list[str]:
    """Return the frozen 99-task ALE score set supported by the local sandbox."""
    benchmark = by_id(config, "benchmarks", "ale-cli")
    runner = config["runners"]["ale"]
    local_spec = benchmark["local_docker"]
    path = ROOT / runner["repository_path"] / local_spec["selected_tasks_path"]
    values = [
        line.strip() for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    expected = int(local_spec["tasks"])
    if len(values) != expected or len(set(values)) != expected:
        raise ValueError(
            f"ALE local Docker task set mismatch: got {len(values)}, expected {expected}"
        )
    return values


def common_harbor_job(
    *, run_dir: Path, unit_id: str, benchmark_id: str, tasks: list[str],
    concurrency: int, dataset_path: Path,
) -> dict[str, Any]:
    tua = benchmark_id == "tua-bench"
    return {
        "job_name": unit_id,
        "jobs_dir": str((run_dir / "raw" / unit_id).resolve()),
        "n_attempts": 1,
        "install_only": False,
        "timeout_multiplier": 1.0,
        "agent_timeout_multiplier": 1.0,
        "verifier_timeout_multiplier": 1.0,
        "environment_build_timeout_multiplier": 1.0,
        "agent_setup_timeout_multiplier": 2.0 if tua else 1.0,
        "n_concurrent_trials": concurrency,
        "retry": {"max_retries": 0},
        "environment": {
            "type": "docker",
            "force_build": False,
            "delete": True,
            "cpu_enforcement_policy": "auto",
            "memory_enforcement_policy": "auto",
        },
        "verifier": {},
        "datasets": [{"path": str(dataset_path.resolve()), "task_names": tasks}],
        "tasks": [],
        "artifacts": [],
        "extra_instruction_paths": [],
        "extra_instructions": [],
    }


def pi_agent(
    run_dir: Path, unit_id: str, harness: dict[str, Any], model_id: str
) -> tuple[dict[str, Any], list[Path]]:
    spec = harness["model_transport"]["per_model_catalog"][model_id]
    route = {
        "only": spec["provider_only"],
        "allow_fallbacks": False,
        "require_parameters": True,
    }
    if spec.get("quantizations"):
        route["quantizations"] = spec["quantizations"]
    model_path = run_dir / "configs" / f"{unit_id}.pi-models.json"
    remote = f"/tmp/{unit_id}-pi"
    write_json(model_path, {
        "providers": {"openrouter": {"modelOverrides": {
            spec["model_id"]: {"compat": {
                "maxTokensField": "max_tokens", "openRouterRouting": route,
            }}
        }}}
    })
    overlay_path = run_dir / "configs" / f"{unit_id}.compose.json"
    write_json(overlay_path, {"services": {"main": {
        "tmpfs": [f"{remote}:rw,mode=1777"],
        "volumes": [f"{model_path.resolve()}:{remote}/models.json:ro"],
    }}})
    agent = {
        "name": "pi",
        "model_name": f"openrouter/{spec['model_id']}",
        "n_concurrent": 1,
        "skills": [],
        "resume_trajectory": False,
        "include_logs": ["**/*"],
        "kwargs": {"version": "0.84.4", "thinking": "high"},
        "env": {
            "OPENROUTER_API_KEY": "${OPENROUTER_API_KEY}",
            "PI_CODING_AGENT_DIR": remote,
            "PI_OFFLINE": "1",
            "PI_SKIP_VERSION_CHECK": "1",
            "PI_TELEMETRY": "0",
        },
        "mcp_servers": [],
    }
    return agent, [overlay_path]


def claude_agent(
    run_dir: Path, unit_id: str, harness: dict[str, Any], port: int,
) -> tuple[dict[str, Any], list[Path], dict[str, Any]]:
    identity = harness["model_transport"]["harness_model_identity"]
    upstream = harness["model_transport"]["provider_model_id"]
    route = {
        "only": harness["model_transport"]["provider_only"],
        "allow_fallbacks": False,
        "require_parameters": True,
    }
    overlay_path = run_dir / "configs" / f"{unit_id}.compose.json"
    write_json(overlay_path, {"services": {"main": {
        "extra_hosts": ["host.docker.internal:host-gateway"]
    }}})
    settings = {"env": {
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000000",
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1000000",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "64000",
    }}
    agent = {
        "name": "claude-code",
        "model_name": identity,
        "n_concurrent": 1,
        "skills": [],
        "resume_trajectory": False,
        "extra_allowed_hosts": ["host.docker.internal"],
        "include_logs": ["**/*"],
        "kwargs": {
            "version": "2.1.251", "reasoning_effort": "high", "config": settings,
        },
        "env": {
            "ANTHROPIC_AUTH_TOKEN": "${OPENROUTER_API_KEY}",
            "ANTHROPIC_BASE_URL": f"http://host.docker.internal:{port}",
            "ANTHROPIC_CUSTOM_HEADERS": "X-OpenRouter-Metadata: enabled",
            "ANTHROPIC_MODEL": identity,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": identity,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": identity,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": identity,
            "CLAUDE_CODE_SUBAGENT_MODEL": identity,
            "CLAUDE_CODE_EFFORT_LEVEL": "high",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        },
        "mcp_servers": [],
    }
    spec = {
        "cell_id": unit_id,
        "channel": "openrouter",
        "harness": "claude-code",
        "harness_version": "2.1.251",
        "logical_model": "claude-opus-5",
        "actual_model": upstream,
        "provider": "openrouter",
        "key_env": "OPENROUTER_API_KEY",
        "protocol": "anthropic-messages",
        "translated": False,
        "translation": None,
        "proxy_required": False,
        "body_injector_required": True,
        "compatibility_variant": "transparent-provider-body-injector-with-request-gate",
        "compatibility_drop_params": [],
        "base_url": "https://openrouter.ai/api",
        "anthropic_base_url_override": f"http://host.docker.internal:{port}",
        "openrouter_route": route,
    }
    return agent, [overlay_path], {
        "kind": "claude-messages", "port": port, "spec": spec,
    }


def codex_agent(
    run_dir: Path, unit_id: str, benchmark_id: str, harness: dict[str, Any],
    port: int,
) -> tuple[dict[str, Any], list[Path], dict[str, Any]]:
    overlay: dict[str, Any] = {"services": {"main": {
        "extra_hosts": ["host.docker.internal:host-gateway"]
    }}}
    if benchmark_id == "tua-bench":
        cache = ROOT / ".cache" / "codex" / f"npm-{CODEX_VERSION}"
        vendor = cache / (
            "node_modules/@openai/codex/node_modules/@openai/"
            "codex-linux-x64/vendor/x86_64-unknown-linux-musl"
        )
        entrypoint = run_dir / "configs" / f"{unit_id}.codex-entrypoint"
        entrypoint.write_text(
            f"#!/bin/sh\nexec /opt/codex-{CODEX_VERSION}/bin/codex \"$@\"\n"
        )
        entrypoint.chmod(0o755)
        overlay["services"]["main"]["volumes"] = [
            f"{entrypoint.resolve()}:/usr/local/bin/codex:ro",
            f"{vendor.resolve()}:/opt/codex-{CODEX_VERSION}:ro",
        ]
    overlay_path = run_dir / "configs" / f"{unit_id}.compose.json"
    write_json(overlay_path, overlay)
    agent = {
        "import_path": "integrations.harbor_codex:NativeGPTCodex",
        "model_name": "gpt-6-astra",
        "n_concurrent": 1,
        "skills": [],
        "resume_trajectory": False,
        "extra_allowed_hosts": ["host.docker.internal"],
        "include_logs": ["**/*"],
        "kwargs": {
            "version": CODEX_VERSION,
            "reasoning_effort": "high",
            "reasoning_summary": "none",
        },
        "env": {
            "OPENAI_API_KEY": "${OPENROUTER_API_KEY}",
            "OPENAI_BASE_URL": f"http://host.docker.internal:{port}/v1",
        },
        "mcp_servers": [],
    }
    spec = {
        "incoming_model": harness["model_transport"]["requested_model"],
        "model": harness["model_transport"]["forwarded_model"],
        "provider": {
            "only": harness["model_transport"]["provider_only"],
            "allow_fallbacks": False,
            "require_parameters": False,
        },
    }
    return agent, [overlay_path], {
        "kind": "codex-responses", "port": port, "spec": spec,
    }


def openhands_agent(model: dict[str, Any]) -> dict[str, Any]:
    provider = model["openrouter"]
    kwargs: dict[str, Any] = {
        "version": "1.44.1",
        "reasoning_effort": "high",
        "max_input_tokens": 1_000_000,
        "max_output_tokens": 128_000,
        "api_mode": "auto",
        "capability_overrides": provider.get("openhands_capability_overrides", {}),
        "openrouter_provider": route_for(model, require_parameters=True),
        "load_skills": False,
        "max_iterations": 500,
        "temperature": None,
    }
    if provider.get("openhands_inline_image_urls"):
        kwargs["inline_image_urls"] = True
    return {
        "import_path": "integrations.harbor:ControlledOpenHandsSDK",
        "model_name": f"openrouter/{provider['model_id']}",
        "n_concurrent": 1,
        "skills": [],
        "resume_trajectory": False,
        "include_logs": ["**/*"],
        "kwargs": kwargs,
        "mcp_servers": [],
    }


def dsh_agent(harness: dict[str, Any], model_id: str) -> dict[str, Any]:
    route_spec = harness["model_transport"]["strict_routes"][model_id]
    route = {
        "only": route_spec["provider_only"],
        "allow_fallbacks": False,
        "require_parameters": True,
    }
    if route_spec.get("quantizations"):
        route["quantizations"] = route_spec["quantizations"]
    kwargs: dict[str, Any] = {
        "version": "0.1.1-rc.2",
        "adapter": "pi-ai",
        "model_api": "openai-completions",
        "context_window": harness["controls"]["context"]["per_model_tokens"][model_id],
        "max_tokens": harness["controls"]["output"]["per_model_tokens"][model_id],
        "input_modalities": route_spec["input_modalities"],
        "reasoning_effort": "high",
        "compat": {
            "supportsDeveloperRole": False,
            "supportsReasoningEffort": True,
            "maxTokensField": "max_tokens",
            "thinkingFormat": "openrouter",
        },
        "openrouter_route": route,
        "permission_mode": "danger-full-access",
    }
    if model_id == "claude-opus-5":
        kwargs["compat"]["cacheControlFormat"] = "anthropic"
        kwargs["cache_retention"] = "short"
    return {
        "name": "integrations.harbor_deepseek:DeepSeekHarness",
        "model_name": f"openrouter/{route_spec['model']}",
        "n_concurrent": 1,
        "skills": [],
        "resume_trajectory": False,
        "include_logs": ["**/*"],
        "kwargs": kwargs,
        "env": {"OPENROUTER_API_KEY": "${OPENROUTER_API_KEY}"},
        "mcp_servers": [],
    }


def build_harbor_unit(
    config: dict[str, Any], run_dir: Path, harness: dict[str, Any],
    model: dict[str, Any], benchmark_id: str, tasks: list[str], concurrency: int,
) -> dict[str, Any]:
    unit_id = f"{benchmark_id}--{harness['id']}--{model['id']}"
    dataset = (
        ROOT / "vendor" / "terminal-bench"
        if benchmark_id == "terminal-bench-4"
        else ROOT / by_id(config, "benchmarks", benchmark_id)["tasks_path"]
    )
    job = common_harbor_job(
        run_dir=run_dir, unit_id=unit_id, benchmark_id=benchmark_id,
        tasks=tasks, concurrency=concurrency, dataset_path=dataset,
    )
    overlays: list[Path] = []
    sidecar: dict[str, Any] | None = None
    if harness["id"] == "pi":
        agent, overlays = pi_agent(run_dir, unit_id, harness, model["id"])
    elif harness["id"] == "claude-code":
        agent, overlays, sidecar = claude_agent(
            run_dir, unit_id, harness,
            sidecar_port(run_dir, unit_id, CLAUDE_PORT),
        )
    elif harness["id"] == "codex":
        agent, overlays, sidecar = codex_agent(
            run_dir, unit_id, benchmark_id, harness,
            sidecar_port(run_dir, unit_id, CODEX_PORT),
        )
    elif harness["id"] == "openhands":
        agent = openhands_agent(model)
    elif harness["id"] == "deepseek-harness":
        agent = dsh_agent(harness, model["id"])
    else:
        raise ValueError(f"unsupported harness: {harness['id']}")
    job["agents"] = [agent]
    if overlays:
        job["environment"]["extra_docker_compose"] = [str(p.resolve()) for p in overlays]
    config_path = run_dir / "configs" / f"{unit_id}.harbor.json"
    write_json(config_path, job)
    sidecar_path: Path | None = None
    if sidecar:
        sidecar_path = run_dir / "configs" / f"{unit_id}.sidecar.json"
        write_json(sidecar_path, sidecar["spec"], mode=0o600)
        sidecar = {**sidecar, "spec_path": str(sidecar_path.resolve())}
        sidecar.pop("spec", None)
    return {
        "id": unit_id,
        "runner": "harbor",
        "benchmark": benchmark_id,
        "harness": harness["id"],
        "model": model["id"],
        "task_count": len(tasks),
        "task_ids_sha256": sha256_bytes("\n".join(tasks).encode()),
        "config": str(config_path.resolve()),
        "config_sha256": sha256_file(config_path),
        "output": str((run_dir / "raw" / unit_id).resolve()),
        "sidecar": sidecar,
        "expected_route": {
            "model": model["openrouter"]["model_id"],
            "provider_only": model["openrouter"]["provider_only"],
            "allow_fallbacks": False,
        },
    }


def ale_agent_payload(
    harness: dict[str, Any], model: dict[str, Any]
) -> dict[str, Any]:
    harness_id = harness["id"]
    slug = model["openrouter"]["model_id"]
    base: dict[str, Any] = {
        "class": harness["runners"]["ale"]["deployer"],
        "id": f"{harness_id}--{model['id']}",
        "model": slug,
        "executor": "sandbox",
    }
    if harness_id == "pi":
        base["config"] = {
            "provider": "openrouter", "api_key": None,
            "cli_version": "0.84.4", "thinking": "high",
        }
    elif harness_id == "claude-code":
        base["model"] = "anthropic/claude-opus-5[1m]"
        base["config"] = {
            "provider": "openrouter", "api_key": None,
            "cli_version": "@anthropic-ai/claude-code@2.1.251",
            "effort_level": "high", "max_thinking_tokens": None,
            "context_tokens": 1_000_000, "max_output_tokens": 64_000,
            "max_turns": -1, "max_budget_usd": None,
            "dangerously_skip_permissions": True, "otel_enabled": True,
        }
    elif harness_id == "codex":
        base["model"] = "gpt-6-astra"
        base["config"] = {
            "provider": "openrouter", "base_url": "http://127.0.0.1:4170/v1",
            "reasoning_effort": "high", "codex_version": "0.150.1",
            "patched_binary_url": "", "patched_binary_url_windows": "",
            "fork_version": "0.150.1", "model_catalog_path": "",
            "model_catalog_content": "", "feature_overrides": {},
            "otel_enabled": True, "injector_port": 4170,
            "injector_upstream": "https://openrouter.ai/api",
            "upstream_model": "openai/gpt-6-astra",
            "provider_only": ["openai"], "allow_fallbacks": False,
            "require_parameters": False,
        }
    elif harness_id == "openhands":
        base["config"] = {
            "provider": "openrouter", "base_url": "https://openrouter.ai/api/v1",
            "sdk_version": "1.44.1", "tools_version": "1.44.1",
            "reasoning_effort": "high", "max_input_tokens": 1_000_000,
            "max_output_tokens": 128_000, "api_mode": "auto",
            "max_iterations": 500, "temperature": None, "top_p": None,
            "seed": None, "load_skills": False, "condenser": None,
            "mcp_policy": "ale-cua-only",
        }
    elif harness_id == "deepseek-harness":
        base["config"] = {
            "provider": "openrouter", "api_key": None,
            "cli_version": "0.1.1-rc.2", "reasoning_effort": "high",
            "permission_mode": "danger-full-access",
            "base_url": "http://127.0.0.1:4010/v1",
            "injector_upstream": "https://openrouter.ai/api",
            "injector_port": 4010, "mcp_policy": "ale-cua-only",
        }
    else:
        raise ValueError(f"unsupported ALE harness: {harness_id}")
    return base


def build_ale_unit(
    config: dict[str, Any], local: dict[str, Any], run_dir: Path,
    source_dir: Path, harness: dict[str, Any], model: dict[str, Any],
    tasks: list[str], concurrency: int, *, purpose: str,
) -> dict[str, Any]:
    unit_id = f"ale-cli--{harness['id']}--{model['id']}"
    configs = run_dir / "configs"
    agent_path = configs / f"{unit_id}.agent.yaml"
    write_yaml(agent_path, ale_agent_payload(harness, model))
    task_path = configs / f"{unit_id}.tasks.txt"
    task_path.write_text("\n".join(tasks) + "\n", encoding="utf-8")
    task_path.chmod(0o644)

    ale_local = local.get("ale") or {}
    if ale_local.get("mode") != "local-docker-99":
        raise ValueError(
            "ALE requires local.yaml ale.mode: local-docker-99; the study score set "
            "excludes the six tasks unsupported by this sandbox"
        )
    ale_repo = ROOT / config["runners"]["ale"]["repository_path"]
    local_spec = by_id(config, "benchmarks", "ale-cli")["local_docker"]
    environment = {
        "snapshots": {"cpu-free-ubuntu": {
            "provider": "docker", "image": "ale-ubuntu22-docker",
            "docker": {
                "image_ref": local_spec["image"], "shm_size": "2g",
                "resolution": [1024, 768], "privileged": False,
                "enable_dind": False,
            },
        }},
        "task_data_source": f"local:{ale_repo / 'task-data'}",
        "output_path": "local",
    }
    environment_kind = "study-local-docker-99"
    environment_path = configs / f"{unit_id}.environment.yaml"
    write_yaml(environment_path, environment)
    experiment = {
        "name": unit_id,
        "secret_file": os.path.relpath(ROOT / ".env", configs),
        "agent": str(agent_path.resolve()),
        "environment": str(environment_path.resolve()),
        "tasks": str(task_path.resolve()),
        "output": {"root": str((run_dir / "raw" / unit_id).resolve())},
        "concurrency": concurrency,
        "wall_time_s": None,
        "auto_resume": False,
        "max_attempts": 1,
        "cleanup_mode": "delete",
        "prompt_suffix": "",
    }
    experiment_path = configs / f"{unit_id}.ale.yaml"
    write_yaml(experiment_path, experiment)
    return {
        "id": unit_id,
        "runner": "ale",
        "benchmark": "ale-cli",
        "harness": harness["id"],
        "model": model["id"],
        "task_count": len(tasks),
        "task_ids_sha256": sha256_bytes("\n".join(tasks).encode()),
        "config": str(experiment_path.resolve()),
        "config_sha256": sha256_file(experiment_path),
        "environment_kind": environment_kind,
        "environment_sha256": sha256_file(environment_path),
        "source": str(source_dir.resolve()),
        "output": str((run_dir / "raw" / unit_id).resolve()),
        "sidecar": None,
        "expected_route": {
            "model": model["openrouter"]["model_id"],
            "provider_only": model["openrouter"]["provider_only"],
            "allow_fallbacks": False,
        },
    }


def source_hashes() -> dict[str, str]:
    paths = [
        ROOT / "experiment.py", ROOT / "experiment.yaml", ROOT / "pyproject.toml",
        ROOT / "uv.lock", ROOT / "integrations" / "campaign.py",
        ROOT / "integrations" / "harbor.py",
        ROOT / "integrations" / "harbor_codex.py",
        ROOT / "integrations" / "harbor_deepseek.py",
        ROOT / "integrations" / "openrouter_body_injector.mjs",
        ROOT / "integrations" / "ale.py",
        ROOT / "integrations" / "openhands_reasoning_details_patch.py",
        ROOT / "integrations" / "patches" / "dsh-0.1.1-rc.2-headless-standard.patch",
        ROOT / "pilot" / "claude_openrouter_pin_smoke.py",
        ROOT / "pilot" / "provider_smoke.py",
        ROOT / "pilot" / "provider_tool_smoke.py",
    ]
    return {str(path.relative_to(ROOT)): sha256_file(path) for path in paths}


def prepare_campaign(
    config: dict[str, Any], *, local_path: Path, run_id: str,
    harness_id: str, model_ids: list[str] | None,
    benchmark_ids: list[str] | None, explicit_tasks: list[str] | None,
    purpose: str, concurrency: int | None, unit_concurrency: int | None,
    qualification_gate: bool,
) -> dict[str, Any]:
    if not RUN_ID_RE.fullmatch(run_id):
        raise ValueError("run ID must be 3-96 lowercase letters/digits/._- characters")
    run_dir = ROOT / "runs" / run_id
    if run_dir.exists():
        raise FileExistsError(f"refusing to overwrite run directory: {run_dir}")
    local = load_local(local_path)
    harness = by_id(config, "harnesses", harness_id)
    if (
        purpose == "formal" and config["campaign"].get("status") != "ready"
        and not qualification_gate
    ):
        raise ValueError(
            "formal campaign is not centrally released: experiment.yaml "
            f"campaign.status={config['campaign'].get('status')!r}"
        )
    allowed_models = harness["study_models"]
    chosen_models = model_ids or allowed_models
    if chosen_models == ["all"]:
        chosen_models = allowed_models
    if len(set(chosen_models)) != len(chosen_models):
        raise ValueError("model selection must be unique")
    outside = sorted(set(chosen_models) - set(allowed_models))
    if outside:
        raise ValueError(
            f"{harness_id} models outside registered matrix: {', '.join(outside)}"
        )
    chosen_benchmarks = benchmark_ids or [b["id"] for b in config["benchmarks"]]
    if chosen_benchmarks == ["all"]:
        chosen_benchmarks = [b["id"] for b in config["benchmarks"]]
    if len(set(chosen_benchmarks)) != len(chosen_benchmarks):
        raise ValueError("benchmark selection must be unique")
    for benchmark_id in chosen_benchmarks:
        by_id(config, "benchmarks", benchmark_id)
    if explicit_tasks and len(chosen_benchmarks) != 1:
        raise ValueError("--task can only be used with one benchmark")

    ale_local = local.get("ale") or {}
    if (
        "ale-cli" in chosen_benchmarks
        and ale_local.get("mode") != "local-docker-99"
    ):
        raise ValueError("ALE requires local.yaml ale.mode: local-docker-99")
    concurrency = concurrency or int((local.get("concurrency") or {}).get("trials", 4))
    unit_concurrency = unit_concurrency or int(
        (local.get("concurrency") or {}).get("units", 1)
    )
    if concurrency < 1 or unit_concurrency < 1:
        raise ValueError("concurrency values must be positive")

    task_map: dict[str, list[str]] = {}
    for benchmark_id in chosen_benchmarks:
        if benchmark_id == "ale-cli":
            available = ale_local_docker_tasks(config)
            unknown = sorted(set(explicit_tasks or []) - set(available))
            if unknown:
                raise ValueError(
                    "ALE local Docker cannot run selected tasks: " + ", ".join(unknown)
                )
            task_map[benchmark_id] = explicit_tasks or available
        else:
            task_map[benchmark_id] = selected_tasks(
                config, benchmark_id, explicit_tasks
            )
    gate_paths: list[str] = []
    for benchmark_id in chosen_benchmarks:
        level = qualification_level(harness, benchmark_id)
        if level == "B":
            continue
        path = f"{benchmark_id}--{harness_id}"
        if not (
            qualification_gate and purpose == "formal" and path == "ale-cli--pi"
            and explicit_tasks
        ):
            raise ValueError(
                f"{path} is level {level}; use a pre-registered qualification gate "
                "before primary-result preparation"
            )
        gate_paths.append(path)

    run_dir.mkdir(parents=True)
    (run_dir / "configs").mkdir()
    (run_dir / "logs").mkdir()
    models = [by_id(config, "models", model_id) for model_id in chosen_models]
    source_dir: Path | None = None
    source_manifest: dict[str, Any] | None = None
    if "ale-cli" in chosen_benchmarks:
        from integrations.ale import prepare_agent_source

        source_dir = run_dir / "source"
        source_manifest = prepare_agent_source(config, source_dir, [harness_id])

    units: list[dict[str, Any]] = []
    try:
        for benchmark_id in chosen_benchmarks:
            for model in models:
                if benchmark_id == "ale-cli":
                    assert source_dir is not None
                    unit = build_ale_unit(
                        config, local, run_dir, source_dir, harness, model,
                        task_map[benchmark_id], concurrency, purpose=purpose,
                    )
                else:
                    unit = build_harbor_unit(
                        config, run_dir, harness, model, benchmark_id,
                        task_map[benchmark_id], concurrency,
                    )
                units.append(unit)
    except Exception:
        # The directory contains no run output and is intentionally left as
        # diagnostic evidence; a corrected preparation must use a new run ID.
        raise

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "purpose": purpose,
        "prepared_at_utc": utc_now(),
        "approval": {"required": True, "approved_sha256": None},
        "study_config_sha256": sha256_bytes(canonical(config)),
        "local_config": {
            "path": str(local_path.resolve()),
            "sha256": sha256_file(local_path),
        },
        "git": git_state(),
        "machine": machine_state(),
        "source_sha256": source_hashes(),
        "selection": {
            "harness": harness_id,
            "models": chosen_models,
            "benchmarks": chosen_benchmarks,
            "replicate": config["campaign"]["repetitions"],
        },
        "controls": {
            "trial_concurrency": concurrency,
            "unit_concurrency": unit_concurrency,
            "reasoning_effort": "high",
            "harbor_outer_retries": 0,
            "ale_max_attempts": 1,
            "provider": "openrouter-strict-no-fallback",
        },
        "resolved_contract": {
            "harness": {
                "id": harness["id"], "version": harness["version"],
                "install": harness["install"], "runners": harness["runners"],
                "model_transport": harness["model_transport"],
                "controls": harness["controls"],
            },
            "models": {
                model["id"]: model["openrouter"] for model in models
            },
            "benchmarks": {
                benchmark_id: by_id(config, "benchmarks", benchmark_id)
                for benchmark_id in chosen_benchmarks
            },
        },
        "qualification": {
            "gate_paths": gate_paths,
            "primary_eligible_before_audit": not bool(gate_paths),
            "rule": "gate output becomes eligible only after the frozen structural audit passes",
        },
        "task_sets": {
            key: {
                "count": len(value),
                "sha256": sha256_bytes("\n".join(value).encode()),
            }
            for key, value in task_map.items()
        },
        "ale_derived_source": source_manifest,
        "units": units,
        "expected_trials": sum(unit["task_count"] for unit in units),
        "cost_source": "OpenRouter settled per-generation records",
        "post_run_trajectory_audit_required": True,
    }
    manifest["manifest_sha256"] = sha256_bytes(canonical(manifest))
    manifest_path = run_dir / "manifest.json"
    write_json(manifest_path, manifest, mode=0o444)
    write_state(run_dir / "state.json", {
        "run_id": run_id, "status": "prepared",
        "manifest_sha256": manifest["manifest_sha256"], "units": {},
    })
    return manifest


def verify_manifest(path: Path) -> dict[str, Any]:
    manifest = json.loads(path.read_text())
    recorded = manifest.pop("manifest_sha256", None)
    actual = sha256_bytes(canonical(manifest))
    manifest["manifest_sha256"] = recorded
    if recorded != actual:
        raise ValueError(f"manifest hash mismatch: recorded={recorded}, actual={actual}")
    for unit in manifest["units"]:
        config_path = Path(unit["config"])
        if sha256_file(config_path) != unit["config_sha256"]:
            raise ValueError(f"unit config drift: {unit['id']}")
    for relative, expected in manifest["source_sha256"].items():
        path_to_source = ROOT / relative
        if sha256_file(path_to_source) != expected:
            raise ValueError(f"source drift after preparation: {relative}")
    return manifest


def harbor_command(config_path: Path, *, print_config: bool = False) -> list[str]:
    return [
        "uvx", "--offline", "--from", f"harbor=={HARBOR_VERSION}",
        "--with", f"platformdirs=={HARBOR_PLATFORMDIRS_VERSION}",
        "harbor", "run", "--config", str(config_path),
        "--print-config" if print_config else "--yes",
    ]


def host_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["UV_CACHE_DIR"] = str(ROOT / ".cache" / "uv")
    env["UV_TOOL_DIR"] = str(ROOT / ".cache" / "uv-tools")
    return env


def preflight_campaign(manifest_path: Path) -> dict[str, Any]:
    manifest = verify_manifest(manifest_path)
    sidecar_ports = [
        int(unit["sidecar"]["port"]) for unit in manifest["units"]
        if unit.get("sidecar")
    ]
    if len(sidecar_ports) != len(set(sidecar_ports)):
        raise RuntimeError("resolved sidecar ports collide inside this campaign")
    def check_unit(unit: dict[str, Any]) -> dict[str, Any]:
        env = host_env()
        env["OPENROUTER_API_KEY"] = "preflight-placeholder"
        if unit["runner"] == "harbor":
            command = harbor_command(Path(unit["config"]), print_config=True)
            cwd = ROOT
            result_kind = "harbor-print-config"
            cleanup = None
        else:
            cleanup = tempfile.TemporaryDirectory(prefix="ale-preflight-", dir="/tmp")
            sanitized_path = Path(cleanup.name) / "experiment.yaml"
            sanitized = yaml.safe_load(Path(unit["config"]).read_text())
            sanitized.pop("secret_file", None)
            write_yaml(sanitized_path, sanitized)
            ale_repo = ROOT / "vendor" / "agents-last-exam"
            env["PYTHONPATH"] = os.pathsep.join([unit["source"], str(ale_repo)])
            command = [
                sys.executable, "-P", "-c", ALE_PREFLIGHT_SCRIPT,
                str(sanitized_path),
            ]
            cwd = ale_repo
            result_kind = "ale-loader-and-agent-config"
        process = subprocess.Popen(
            command, cwd=cwd, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        timed_out_after_output = False
        try:
            output, _ = process.communicate(timeout=4)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                output, _ = process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                output, _ = process.communicate()
            timed_out_after_output = True
        if unit["runner"] == "harbor":
            try:
                resolved = json.loads(output)
                resolved_ok = (
                    resolved.get("job_name") == unit["id"]
                    and len(resolved.get("agents") or []) == 1
                    and len((resolved.get("datasets") or [{}])[0].get("task_names") or [])
                        == unit["task_count"]
                )
            except (json.JSONDecodeError, AttributeError, IndexError):
                resolved_ok = False
        else:
            try:
                resolved = json.loads(output)
                resolved_ok = (
                    resolved.get("name") == unit["id"]
                    and resolved.get("agents") == 1
                    and resolved.get("units") == unit["task_count"]
                )
            except (json.JSONDecodeError, AttributeError):
                resolved_ok = False
        return_code = process.returncode if process.returncode is not None else 255
        passed_unit = resolved_ok and (return_code == 0 or timed_out_after_output)
        result = {
            "unit": unit["id"], "kind": result_kind,
            "return_code": return_code,
            "resolved_config_valid": resolved_ok,
            "terminated_after_valid_print_config": timed_out_after_output and resolved_ok,
            "passed": passed_unit,
            "output_sha256": sha256_bytes(output.encode()),
            "tail": output[-2000:] if not passed_unit else None,
        }
        if cleanup is not None:
            cleanup.cleanup()
        return result

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(8, max(1, len(manifest["units"])))
    ) as pool:
        results = list(pool.map(check_unit, manifest["units"]))
    passed = all(item["passed"] for item in results)
    evidence = {
        "status": "passed" if passed else "failed",
        "manifest_sha256": manifest["manifest_sha256"],
        "paid_api_calls": 0,
        "sidecar_ports_unique": True,
        "unit_checks": results,
        "completed_at_utc": utc_now(),
    }
    path = manifest_path.parent / "preflight.json"
    if path.exists():
        raise FileExistsError(f"preflight already exists: {path}")
    write_json(path, evidence, mode=0o444)
    if not passed:
        raise RuntimeError("one or more runner configuration checks failed")
    return evidence


def wait_port(process: subprocess.Popen[Any], port: int) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"sidecar exited during startup on port {port}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.2)
    raise TimeoutError(f"sidecar health timeout on port {port}")


def start_sidecar(
    unit: dict[str, Any], run_dir: Path
) -> tuple[subprocess.Popen[Any], Any, str | None] | None:
    spec = unit.get("sidecar")
    if not spec:
        return None
    log_path = run_dir / "logs" / f"{unit['id']}.sidecar-process.log"
    handle = log_path.open("w", encoding="utf-8", buffering=1)
    port = int(spec["port"])
    container_name: str | None = None
    if spec["kind"] == "claude-messages":
        command = [
            sys.executable, str(ROOT / "pilot" / "claude_openrouter_pin_smoke.py"),
            "_proxy", "--spec", spec["spec_path"], "--host", "0.0.0.0",
            "--port", str(port), "--upstream", "https://openrouter.ai/api",
            "--log-path", str(run_dir / "logs" / f"{unit['id']}.injector.jsonl"),
            "--max-in-flight", "2",
        ]
    elif spec["kind"] == "codex-responses":
        container_name = re.sub(r"[^a-z0-9_.-]", "-", f"{run_dir.name}-{unit['id']}-injector")
        telemetry = run_dir / "logs" / f"{unit['id']}.injector"
        telemetry.mkdir(parents=True, exist_ok=True)
        command = [
            "docker", "run", "--rm", "--network", "host", "--name", container_name,
            "--user", f"{os.getuid()}:{os.getgid()}",
            "-v", f"{ROOT / 'integrations'}:/integrations:ro",
            "-v", f"{spec['spec_path']}:/config.json:ro",
            "-v", f"{telemetry}:/telemetry", NODE_IMAGE,
            "node", "/integrations/openrouter_body_injector.mjs", "--mode", "inject",
            "--config", "/config.json", "--upstream", "https://openrouter.ai/api",
            "--log", "/telemetry/requests.jsonl", "--port", str(port),
            "--listen-host", "0.0.0.0",
        ]
    else:
        handle.close()
        raise ValueError(f"unknown sidecar: {spec['kind']}")
    process = subprocess.Popen(
        command, cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT,
        text=True, start_new_session=True,
    )
    try:
        wait_port(process, port)
    except Exception:
        stop_sidecar(process, handle, container_name)
        raise
    return process, handle, container_name


def stop_sidecar(process: subprocess.Popen[Any], handle: Any, container: str | None) -> None:
    if container:
        subprocess.run(
            ["docker", "stop", "--time", "5", container],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
    elif process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
    handle.close()


def execute_unit(unit: dict[str, Any], run_dir: Path) -> int:
    output = Path(unit["output"])
    if output.exists():
        raise FileExistsError(f"refusing to reuse unit output: {output}")
    log_path = run_dir / "logs" / f"{unit['id']}.log"
    sidecar = start_sidecar(unit, run_dir)
    env = host_env()
    env["HARNESS_CODEX_INSTALL_LOCK"] = str(run_dir / "codex-install.lock")
    if unit["runner"] == "harbor":
        command = harbor_command(Path(unit["config"]))
        cwd = ROOT
    else:
        ale_repo = ROOT / "vendor" / "agents-last-exam"
        env["PYTHONPATH"] = os.pathsep.join([unit["source"], str(ale_repo)])
        command = [
            sys.executable, "-P", "-m", "ale_run", "run", unit["config"],
            "--disable-resume",
        ]
        cwd = ale_repo
    try:
        with log_path.open("w", encoding="utf-8", buffering=1) as log:
            completed = subprocess.run(
                command, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT,
                text=True, check=False,
            )
        return completed.returncode
    finally:
        if sidecar:
            stop_sidecar(*sidecar)


def _read_state(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def execute_campaign(manifest_path: Path, approved_sha256: str) -> int:
    manifest = verify_manifest(manifest_path)
    if approved_sha256 != manifest["manifest_sha256"]:
        raise ValueError("approved SHA256 does not match the frozen manifest")
    preflight_path = manifest_path.parent / "preflight.json"
    if not preflight_path.is_file():
        raise ValueError("matching zero-cost preflight.json is required")
    preflight = json.loads(preflight_path.read_text())
    if (
        preflight.get("status") != "passed"
        or preflight.get("manifest_sha256") != approved_sha256
    ):
        raise ValueError("preflight is not passed for this exact manifest")
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise ValueError("OPENROUTER_API_KEY is not loaded")
    run_dir = manifest_path.parent
    state_path = run_dir / "state.json"
    state = _read_state(state_path)
    if state.get("status") != "prepared":
        raise ValueError(f"run state must be prepared, got {state.get('status')}")
    state.update({"status": "running", "started_at_utc": utc_now()})
    write_state(state_path, state)
    lock = threading.Lock()

    def worker(unit: dict[str, Any]) -> tuple[str, int]:
        with lock:
            state["units"][unit["id"]] = {
                "status": "running", "started_at_utc": utc_now()
            }
            write_state(state_path, state)
        try:
            code = execute_unit(unit, run_dir)
        except BaseException as exc:
            code = 255
            error = f"{type(exc).__name__}: {exc}"
        else:
            error = None
        with lock:
            state["units"][unit["id"]] = {
                "status": "finished" if code == 0 else "failed",
                "return_code": code, "finished_at_utc": utc_now(), "error": error,
            }
            write_state(state_path, state)
        return unit["id"], code

    results: list[tuple[str, int]] = []
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=int(manifest["controls"]["unit_concurrency"])
    ) as pool:
        futures = [pool.submit(worker, unit) for unit in manifest["units"]]
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
    failed = [unit_id for unit_id, code in results if code != 0]
    state.update({
        "status": "finished" if not failed else "finished-with-failures",
        "finished_at_utc": utc_now(), "failed_units": sorted(failed),
        "trajectory_audit_required": True,
    })
    write_state(state_path, state)
    return 0 if not failed else 2


def launch_campaign(manifest_path: Path, approved_sha256: str) -> str:
    manifest = verify_manifest(manifest_path)
    if approved_sha256 != manifest["manifest_sha256"]:
        raise ValueError("approved SHA256 does not match the frozen manifest")
    session = re.sub(r"[^a-zA-Z0-9_.-]", "-", f"eval-{manifest['run_id']}")[:120]
    exists = subprocess.run(
        ["tmux", "has-session", "-t", session],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    ).returncode == 0
    if exists:
        raise ValueError(f"tmux session already exists: {session}")
    inner = (
        f"set -a; source {shlex.quote(str(ROOT / '.env'))}; set +a; exec "
        + shlex.join([
            sys.executable, str(ROOT / "experiment.py"), "_execute",
            "--manifest", str(manifest_path.resolve()),
            "--approved-manifest-sha256", approved_sha256,
        ])
    )
    subprocess.run([
        "tmux", "new-session", "-d", "-s", session, "-c", str(ROOT),
        "/bin/bash", "-lc", inner,
    ], check=True)
    write_json(manifest_path.parent / "launcher.json", {
        "launched_at_utc": utc_now(), "tmux": session,
        "manifest_sha256": approved_sha256,
    }, mode=0o444)
    return session


def result_files(run_dir: Path) -> list[Path]:
    raw = run_dir / "raw"
    if not raw.exists():
        return []
    results: list[Path] = []
    for path in raw.rglob("result.json"):
        try:
            value = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(value, dict) and "task_name" in value:
            results.append(path)
    for path in raw.rglob("run.json"):
        try:
            value = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(value, dict) and "task" in value and "status" in value:
            results.append(path)
    return sorted(results)


def campaign_status(manifest_path: Path) -> dict[str, Any]:
    manifest = verify_manifest(manifest_path)
    run_dir = manifest_path.parent
    state = _read_state(run_dir / "state.json")
    launcher_path = run_dir / "launcher.json"
    launcher = json.loads(launcher_path.read_text()) if launcher_path.exists() else None
    alive = False
    if launcher and launcher.get("tmux"):
        alive = subprocess.run(
            ["tmux", "has-session", "-t", launcher["tmux"]],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        ).returncode == 0
    results = result_files(run_dir)
    return {
        "run_id": manifest["run_id"], "state": state.get("status"),
        "tmux_alive": alive, "finished_units": sum(
            item.get("status") in {"finished", "failed"}
            for item in state.get("units", {}).values()
        ),
        "total_units": len(manifest["units"]),
        "result_files": len(results), "expected_trials": manifest["expected_trials"],
        "trajectory_audit_required": state.get("trajectory_audit_required", True),
    }


def _generation_ids_in(roots: list[Path]) -> list[str]:
    found: set[str] = set()
    for root in roots:
        if not root.exists():
            continue
        paths = [root] if root.is_file() else root.rglob("*")
        for path in paths:
            if not path.is_file() or path.suffix not in {".json", ".jsonl", ".log"}:
                continue
            with path.open("r", encoding="utf-8", errors="replace") as source:
                carry = ""
                while chunk := source.read(4 * 1024 * 1024):
                    value = carry + chunk
                    found.update(GENERATION_ID_RE.findall(value))
                    carry = value[-128:]
    return sorted(found)


def _generation_ids_for_unit(run_dir: Path, unit: dict[str, Any]) -> list[str]:
    roots = [Path(unit["output"])]
    logs = run_dir / "logs"
    if logs.is_dir():
        roots.extend(path for path in logs.glob(f"{unit['id']}*") if path.is_file())
        roots.extend(path for path in logs.glob(f"{unit['id']}*") if path.is_dir())
    return _generation_ids_in(roots)


def _route_matches(expected: list[str], actual: str | None) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", (actual or "").lower())
    aliases = {
        "anthropic": "anthropic",
        "openai": "openai",
        "moonshotaimxfp4": "moonshot",
        "zaifp8": "zai",
        "deepseek": "deepseek",
    }
    tokens = [
        aliases.get(re.sub(r"[^a-z0-9]", "", item.lower())) for item in expected
    ]
    return any(token is not None and token in normalized for token in tokens)


def _provider_records(ids: list[str], api_key: str) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    for generation_id in ids:
        request = urllib.request.Request(
            "https://openrouter.ai/api/v1/generation?"
            + urllib.parse.urlencode({"id": generation_id}),
            headers={"Authorization": f"Bearer {api_key}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read())
            if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
                records.append(payload["data"])
            else:
                errors.append(f"{generation_id}: malformed response")
        except Exception as exc:
            errors.append(f"{generation_id}: {type(exc).__name__}")
    return records, errors


def audit_campaign(manifest_path: Path, *, fetch_provider: bool) -> dict[str, Any]:
    manifest = verify_manifest(manifest_path)
    run_dir = manifest_path.parent
    state = _read_state(run_dir / "state.json")
    if state.get("status") not in {"finished", "finished-with-failures"}:
        raise ValueError(
            f"campaign must finish before audit; current state is {state.get('status')}"
        )
    if (run_dir / "audit.json").exists():
        raise FileExistsError("audit.json already exists; raw evidence is immutable")
    results: list[dict[str, Any]] = []
    exception_count = 0
    reward_count = 0
    for path in result_files(run_dir):
        try:
            value = json.loads(path.read_text())
        except Exception:
            results.append({"path": str(path.relative_to(run_dir)), "parse_error": True})
            continue
        if path.name == "run.json":
            termination = value.get("termination") or {}
            exception = termination.get("error") if value.get("status") != "completed" else None
            rewards = value.get("score")
            task_value = value.get("task") or {}
            task_name = task_value.get("path") or task_value.get("slug")
        else:
            exception = value.get("exception_info")
            rewards = (value.get("verifier_result") or {}).get("rewards")
            task_name = value.get("task_name")
        exception_count += exception is not None
        if isinstance(rewards, dict):
            positive_reward = any(float(v or 0) > 0 for v in rewards.values())
        elif isinstance(rewards, (int, float)):
            positive_reward = float(rewards) > 0
        elif isinstance(rewards, list):
            positive_reward = any(
                isinstance(value, (int, float)) and float(value) > 0
                for value in rewards
            )
        else:
            positive_reward = False
        reward_count += positive_reward
        results.append({
            "path": str(path.relative_to(run_dir)),
            "task_name": task_name, "exception": exception,
            "rewards": rewards,
        })
    unit_ids = {
        unit["id"]: _generation_ids_for_unit(run_dir, unit)
        for unit in manifest["units"]
    }
    ids = sorted({generation_id for values in unit_ids.values() for generation_id in values})
    records: list[dict[str, Any]] = []
    provider_errors: list[str] = []
    if fetch_provider:
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise ValueError("OPENROUTER_API_KEY must be loaded for provider audit")
        records, provider_errors = _provider_records(ids, key)
    records_by_id = {str(record.get("id")): record for record in records}
    route_checks: list[dict[str, Any]] = []
    for unit in manifest["units"]:
        unit_records = [
            records_by_id[generation_id] for generation_id in unit_ids[unit["id"]]
            if generation_id in records_by_id
        ]
        expected_route = unit["expected_route"]
        provider_ok = bool(unit_records) and all(
            _route_matches(expected_route["provider_only"], record.get("provider_name"))
            for record in unit_records
        )
        route_checks.append({
            "unit": unit["id"],
            "expected": expected_route,
            "generation_ids": len(unit_ids[unit["id"]]),
            "provider_records": len(unit_records),
            "actual_models": sorted({str(r.get("model")) for r in unit_records if r.get("model")}),
            "actual_providers": sorted({str(r.get("provider_name")) for r in unit_records if r.get("provider_name")}),
            "strict_provider_match": provider_ok if fetch_provider else None,
        })
    marker_patterns = {
        "rate_limit": [r"\b429\b", r"rate[_ -]?limit"],
        "timeout": [r"timed out", r"timeouterror", r"timeout exceeded"],
        "compaction": [
            r'"iscompactsummary"\s*:\s*true', r"auto[- ]compaction triggered",
            r'"type"\s*:\s*"compaction"',
        ],
        "output_limit": [
            r'"finish_reason"\s*:\s*"length"',
            r'"stop_reason"\s*:\s*"max_tokens"',
            r"outputtokenexceedederror",
        ],
    }
    marker_counts = {name: 0 for name in marker_patterns}
    for root in (run_dir / "logs", run_dir / "raw"):
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in {".log", ".jsonl"}:
                continue
            with path.open("r", encoding="utf-8", errors="replace") as source:
                carry = ""
                while chunk := source.read(4 * 1024 * 1024):
                    value = carry + chunk.lower()
                    boundary = len(carry)
                    for name, patterns in marker_patterns.items():
                        marker_counts[name] += sum(
                            match.end() > boundary
                            for pattern in patterns for match in re.finditer(pattern, value)
                        )
                    carry = value[-128:]

    summary = {
        "schema_version": 1,
        "run_id": manifest["run_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "audited_at_utc": utc_now(),
        "completion": {
            "expected_trials": manifest["expected_trials"],
            "result_files": len(results), "exceptions": exception_count,
            "positive_reward_results": reward_count,
        },
        "provider": {
            "generation_ids": len(ids), "records": len(records),
            "lookup_errors": provider_errors,
            "actual_models": sorted({str(r.get("model")) for r in records if r.get("model")}),
            "actual_providers": sorted({str(r.get("provider_name")) for r in records if r.get("provider_name")}),
            "cost_usd": sum(float(r.get("total_cost") or 0) for r in records),
            "prompt_tokens": sum(int(r.get("native_tokens_prompt") or 0) for r in records),
            "cached_prompt_tokens": sum(int(r.get("native_tokens_cached") or 0) for r in records),
            "completion_tokens": sum(int(r.get("native_tokens_completion") or 0) for r in records),
            "reasoning_tokens": sum(int(r.get("native_tokens_reasoning") or 0) for r in records),
            "unit_routes": route_checks,
        },
        "markers": marker_counts,
        "qualification_gate": manifest["qualification"],
        "results": results,
        "decision": "manual-trajectory-review-required",
        "required_next": (
            "sample successes, task failures, infrastructure failures and cost/runtime outliers; "
            "then record accept/rerun/exclude without modifying raw outputs"
        ),
    }
    write_json(run_dir / "audit.json", summary, mode=0o444)
    return summary


def setup_environment(
    config: dict[str, Any], *, harness_id: str, execute: bool
) -> list[list[str]]:
    harness = by_id(config, "harnesses", harness_id)
    ale = config["runners"]["ale"]
    tua = by_id(config, "benchmarks", "tua-bench")
    vendor = ROOT / "vendor"
    ale_path = ROOT / ale["repository_path"]
    tua_path = ROOT / tua["repository_path"]
    tb_path = ROOT / by_id(config, "benchmarks", "terminal-bench-4")["tasks_path"]
    commands: list[list[str]] = [
        ["uv", "sync", "--frozen"],
        [
            "uvx", "--from", f"harbor=={HARBOR_VERSION}",
            "--with", f"platformdirs=={HARBOR_PLATFORMDIRS_VERSION}",
            "harbor", "--help",
        ],
        [
            "uvx", "--from", f"harbor=={HARBOR_VERSION}",
            "--with", f"platformdirs=={HARBOR_PLATFORMDIRS_VERSION}",
            "harbor", "datasets", "download", "terminal-bench@4.0.0",
            "--output-dir", str(vendor), "--export",
        ],
        ["git", "clone", "https://github.com/facebookresearch/TUA-Bench", str(tua_path)],
        ["git", "-C", str(tua_path), "checkout", "--detach", tua["commit"]],
        [
            "uv", "run", "--directory", str(tua_path), "--frozen", "setup-env",
        ],
        [
            "git", "clone", "https://github.com/rdi-berkeley/agents-last-exam",
            str(ale_path),
        ],
        ["git", "-C", str(ale_path), "checkout", "--detach", ale["commit"]],
    ]
    if harness_id == "codex":
        commands.extend([
            ["docker", "pull", NODE_IMAGE],
            [
                "npm", "install", "--prefix",
                str(ROOT / ".cache" / "codex" / f"npm-{CODEX_VERSION}"),
                f"@openai/codex@{CODEX_VERSION}", "--ignore-scripts",
            ],
        ])
    if execute:
        env = host_env()
        vendor.mkdir(parents=True, exist_ok=True)
        subprocess.run(commands[0], cwd=ROOT, env=env, check=True)

        def harbor_probe(command: list[str]) -> bool:
            process = subprocess.Popen(
                command, cwd=ROOT, env=env, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                output, _ = process.communicate(timeout=4)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    output, _ = process.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    output, _ = process.communicate()
            return "Usage: harbor" in output or process.returncode == 0

        offline_harbor = [commands[1][0], "--offline", *commands[1][1:]]
        if not harbor_probe(offline_harbor) and not harbor_probe(commands[1]):
            raise ValueError("could not install or start pinned harbor==0.22.0")
        if not tb_path.is_dir():
            subprocess.run(commands[2], cwd=ROOT, env=env, check=True)
        if not tua_path.is_dir():
            subprocess.run(commands[3], cwd=ROOT, env=env, check=True)
            subprocess.run(commands[4], cwd=ROOT, env=env, check=True)
        actual_tua = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=tua_path, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout.strip()
        if actual_tua != tua["commit"]:
            raise ValueError(
                f"existing TUA checkout drift: {actual_tua}; expected {tua['commit']}"
            )
        setup_module_path = tua_path / "repo_env" / "setup_env.py"
        spec = importlib.util.spec_from_file_location("_campaign_tua_setup", setup_module_path)
        if spec is None or spec.loader is None:
            raise ValueError(f"cannot import TUA setup module: {setup_module_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        targets = module.downloaded_data_targets()
        if len(targets) != int(tua["setup"]["asset_targets"]):
            raise ValueError("TUA setup target list drifted")
        if any(not path.is_file() for path in targets):
            tua_env = env.copy()
            setup_cache = tua_path / ".cache" / "setup-env"
            uv_cache = tua_path / ".cache" / "uv"
            setup_cache.mkdir(parents=True, exist_ok=True)
            uv_cache.mkdir(parents=True, exist_ok=True)
            tua_env.update({
                "TMPDIR": str(setup_cache), "UV_CACHE_DIR": str(uv_cache),
                "UV_LINK_MODE": "copy",
            })
            subprocess.run(commands[5], cwd=ROOT, env=tua_env, check=True)
        if not ale_path.is_dir():
            subprocess.run(commands[6], cwd=ROOT, env=env, check=True)
            subprocess.run(commands[7], cwd=ROOT, env=env, check=True)
        actual_ale = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ale_path, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout.strip()
        if actual_ale != ale["commit"]:
            raise ValueError(
                f"existing ALE checkout drift: {actual_ale}; expected {ale['commit']}"
            )
        if harness_id == "codex":
            image_present = subprocess.run(
                ["docker", "image", "inspect", NODE_IMAGE],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            ).returncode == 0
            if not image_present:
                subprocess.run(commands[8], cwd=ROOT, env=env, check=True)
            codex_cache = ROOT / ".cache" / "codex" / f"npm-{CODEX_VERSION}"
            codex_binary = codex_cache / (
                "node_modules/@openai/codex/node_modules/@openai/"
                "codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex"
            )
            if not codex_binary.is_file():
                subprocess.run(commands[9], cwd=ROOT, env=env, check=True)
    return commands
