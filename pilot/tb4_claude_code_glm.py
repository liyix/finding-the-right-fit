#!/usr/bin/env python3
"""Harbor/TB4 pilot for Claude Code and four OpenRouter-pinned models.

This is a pilot runner, not a replacement Harbor adapter. Harbor's built-in
Claude Code integration remains the agent implementation. A transparent local
Anthropic Messages proxy adds only the frozen OpenRouter ``provider`` object.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import claude_openrouter_pin_smoke as injector
import provider_smoke as smoke
import provider_tool_smoke as tool_smoke


ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN_ID = "pilot-tb4-claude-code-four-model-20260831-r3"
PREPARED_AT_UTC = "2026-08-31T12:00:00Z"
RUN_DIR = ROOT / "runs" / CAMPAIGN_ID
HARBOR_JOB_NAME = "terminal-bench-4--claude-code--four-model--rep-1"
HARBOR_JOBS_DIR = RUN_DIR / "harbor"
HARBOR_JOB_DIR = HARBOR_JOBS_DIR / HARBOR_JOB_NAME
INJECTOR_BASE_PORT = 4110
UPSTREAM_URL = "https://openrouter.ai/api"
TASKS = (
    "html-js-filter",
    "bun-sourcemap-leak",
)
TASK_CHECKSUMS = {
    "html-js-filter": "f9e9f9f97cc4ed197e51c0f79218cba93a9dac01e736e1c1076d7ac91f41f1c7",
    "photonic-waveguide-routing": "6adfe82f8b7736414535fa491affb554aa91856bda9571ad54910c83b9dd12c8",
    "music-harmony": "9623bde8b8df64f044ba142496347ceb058d393e1a2319669250312e8799783f",
    "bun-sourcemap-leak": "fb420f8f5cf1222a1644119305f67e5e3478bcc3745011904168ad447b765120",
}
TB4_COMMIT = "452bf305c6daa62fc59061d22133a7cbc7c1572e"
CLAUDE_CODE_VERSION = "2.1.251"
HARBOR_VERSION = "0.22.0"
MODELS: dict[str, dict[str, Any]] = {
    "claude-opus-5": {
        "request_model": "anthropic/claude-opus-5",
        "cli_model": "anthropic/claude-opus-5[1m]",
        "actual_model": "anthropic/claude-opus-5-20260723",
        "provider": "Anthropic",
        "provider_tag": "anthropic",
        "quantization": "unknown",
        "context_length": 1_000_000,
        "max_completion_tokens": 128_000,
        "route": {
            "only": ["anthropic"],
            "allow_fallbacks": False,
            "require_parameters": True,
        },
    },
    "gpt-6-astra": {
        "request_model": "openai/gpt-6-astra",
        "cli_model": "openai/gpt-6-astra",
        "actual_model": "openai/gpt-6-astra-20260903",
        "provider": "OpenAI",
        "provider_tag": "openai",
        "quantization": "unknown",
        "context_length": 1_050_000,
        "max_completion_tokens": 128_000,
        "route": {
            "only": ["openai"],
            "allow_fallbacks": False,
            "require_parameters": False,
        },
    },
    "glm-5.3": {
        "request_model": "z-ai/glm-5.3",
        "cli_model": "z-ai/glm-5.3",
        "actual_model": "z-ai/glm-5.3-20260816",
        "provider": "Z.AI",
        "provider_tag": "z-ai/fp8",
        "quantization": "fp8",
        "context_length": 1_048_576,
        "max_completion_tokens": 131_072,
        "route": {
            "only": ["z-ai/fp8"],
            "quantizations": ["fp8"],
            "allow_fallbacks": False,
            "require_parameters": False,
        },
    },
    "kimi-k3": {
        "request_model": "moonshotai/kimi-k3",
        "cli_model": "moonshotai/kimi-k3",
        "actual_model": "moonshotai/kimi-k3-20260715",
        "provider": "Moonshot AI",
        "provider_tag": "moonshotai/mxfp4",
        "quantization": "mxfp4",
        "context_length": 1_048_576,
        "max_completion_tokens": 943_718,
        "route": {
            "only": ["moonshotai/mxfp4"],
            "quantizations": ["mxfp4"],
            "allow_fallbacks": False,
            "require_parameters": False,
        },
    },
}
N_CONCURRENT = 4
PER_MODEL_CONCURRENCY = 1
CONTEXT_TOKENS = 1_000_000
AUTO_COMPACT_TOKENS = 1_000_000
WHOLE_RUN_TIMEOUT_SECONDS = 18 * 60 * 60
EXPECTED_COST_USD = "$5-$30; long-task uncertainty is high"
ACCEPTED_EXPOSURE_USD = 40.0
UV_CACHE_DIR = "/tmp/harness-test-uv-cache"
UV_TOOL_DIR = "/tmp/harness-test-uv-tools"


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    for child in sorted(item for item in path.rglob("*") if item.is_file()):
        relative = child.relative_to(path).as_posix().encode()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(bytes.fromhex(sha256_file(child)))
    return digest.hexdigest()


def write_json(path: Path, value: Any, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    path.chmod(mode)


def load_task_metadata(task_name: str) -> dict[str, Any]:
    task_dir = ROOT / "vendor" / "terminal-bench" / task_name
    config = tomllib.loads((task_dir / "task.toml").read_text())
    agent = config["agent"]
    environment = config["environment"]
    verifier = config["verifier"]
    verifier_environment = verifier.get("environment", {})
    return {
        "task_name": task_name,
        "harbor_task_checksum": TASK_CHECKSUMS[task_name],
        "materialized_tree_sha256": tree_sha256(task_dir),
        "category": config.get("metadata", {}).get("category"),
        "expert_time_estimate_hours": config.get("metadata", {}).get(
            "expert_time_estimate_hours"
        ),
        "agent_timeout_seconds": agent["timeout_sec"],
        "verifier_timeout_seconds": verifier["timeout_sec"],
        "agent_environment": {
            key: environment.get(key)
            for key in ("cpus", "memory_mb", "storage_mb", "gpus", "docker_image")
        },
        "verifier_environment": {
            key: verifier_environment.get(key)
            for key in ("cpus", "memory_mb", "storage_mb", "gpus", "docker_image")
        },
    }


def injector_port(logical_model: str) -> int:
    return INJECTOR_BASE_PORT + list(MODELS).index(logical_model)


def container_injector_url(logical_model: str) -> str:
    return f"http://host.docker.internal:{injector_port(logical_model)}"


def injector_spec(logical_model: str) -> dict[str, Any]:
    model = MODELS[logical_model]
    spec = {
        "cell_id": f"{CAMPAIGN_ID}--{logical_model}",
        "channel": "openrouter",
        "harness": "claude-code",
        "harness_version": CLAUDE_CODE_VERSION,
        "logical_model": logical_model,
        "actual_model": model["request_model"],
        "provider": "openrouter",
        "key_env": "OPENROUTER_API_KEY",
        "protocol": "anthropic-messages",
        "translated": False,
        "translation": None,
        "proxy_required": False,
        "body_injector_required": True,
        "compatibility_variant": "transparent-provider-body-injector",
        "compatibility_drop_params": [],
        "base_url": UPSTREAM_URL,
        "anthropic_base_url_override": container_injector_url(logical_model),
        "openrouter_route": model["route"],
    }
    injector.validate_spec(spec)
    return spec


def compose_overlay() -> dict[str, Any]:
    return {
        "services": {
            "main": {
                "extra_hosts": ["host.docker.internal:host-gateway"],
            }
        }
    }


def agent_config(logical_model: str) -> dict[str, Any]:
    model = MODELS[logical_model]
    alias_model = model["cli_model"]
    return {
        "name": "claude-code",
        "model_name": alias_model,
        "n_concurrent": PER_MODEL_CONCURRENCY,
        "skills": [],
        "resume_trajectory": False,
        "extra_allowed_hosts": ["host.docker.internal"],
        "include_logs": ["**/*"],
        "kwargs": {
            "version": CLAUDE_CODE_VERSION,
            "reasoning_effort": "high",
        },
        "env": {
            "ANTHROPIC_AUTH_TOKEN": "${OPENROUTER_API_KEY}",
            "ANTHROPIC_BASE_URL": container_injector_url(logical_model),
            "ANTHROPIC_CUSTOM_HEADERS": "X-OpenRouter-Metadata: enabled",
            "ANTHROPIC_MODEL": alias_model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": alias_model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": alias_model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": alias_model,
            "CLAUDE_CODE_SUBAGENT_MODEL": alias_model,
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS": str(CONTEXT_TOKENS),
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(AUTO_COMPACT_TOKENS),
            "CLAUDE_CODE_EFFORT_LEVEL": "high",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        },
        "mcp_servers": [],
    }


def harbor_config(*, overlay_path: Path) -> dict[str, Any]:
    return {
        "job_name": HARBOR_JOB_NAME,
        "jobs_dir": str(HARBOR_JOBS_DIR),
        "n_attempts": 1,
        "install_only": False,
        "timeout_multiplier": 1.0,
        "n_concurrent_trials": N_CONCURRENT,
        "retry": {"max_retries": 0},
        "environment": {
            "type": "docker",
            "force_build": False,
            "delete": True,
            "cpu_enforcement_policy": "auto",
            "memory_enforcement_policy": "auto",
            "extra_docker_compose": [str(overlay_path)],
        },
        "verifier": {},
        "agents": [agent_config(name) for name in MODELS],
        "datasets": [
            {
                "path": str(ROOT / "vendor" / "terminal-bench"),
                "task_names": list(TASKS),
            }
        ],
        "tasks": [],
        "artifacts": [],
        "extra_instruction_paths": [],
        "extra_instructions": [],
    }


def build_manifest() -> dict[str, Any]:
    overlay_path = RUN_DIR / "host-gateway.compose.json"
    resolved_harbor_config = harbor_config(overlay_path=overlay_path)
    manifest: dict[str, Any] = {
        "campaign_id": CAMPAIGN_ID,
        "kind": "paid-real-benchmark-pilot-not-for-primary-scores",
        "prepared_at_utc": PREPARED_AT_UTC,
        "scope": {
            "benchmark": "Terminal-Bench 4.0",
            "dataset_tag": "v4.0.0",
            "dataset_commit": TB4_COMMIT,
            "local_materialization": str(ROOT / "vendor" / "terminal-bench"),
            "local_materialization_note": (
                "semantic task files matched the official v4.0.0 tag; Harbor package "
                "materialization adds only fixed agent/verifier docker_image entries and "
                "one omitted .gitignore"
            ),
            "tasks": [load_task_metadata(name) for name in TASKS],
            "replicate": 1,
            "planned_trials": len(TASKS) * len(MODELS),
            "n_attempts": 1,
            "concurrency": {
                "global": N_CONCURRENT,
                "per_model": PER_MODEL_CONCURRENCY,
                "policy": "four model arms run concurrently; tasks are sequential within an arm",
            },
            "oracle_preflight": {
                "job": "pilot-tb4-oracle-preflight-20260830",
                "result": "4/4 reward=1; 0 exceptions",
                "runtime_seconds": 202,
            },
        },
        "runner": {
            "name": "Harbor",
            "version": HARBOR_VERSION,
            "package": f"harbor=={HARBOR_VERSION}",
            "source": "PyPI official package / https://github.com/harbor-framework/harbor",
            "commit": "4407eb5",
            "entry_command": (
                "uvx --from harbor==0.22.0 harbor run --config <frozen-job-config> --yes"
            ),
            "integration": "Harbor built-in claude-code agent",
            "resolved_job_config": resolved_harbor_config,
        },
        "repository": {
            "git_head": "ba4fb2e28a3705d0ae0a07ae67cf50f6fa273e3a",
            "working_tree": (
                "dirty shared workspace; exact source hashes below and immutable run manifest "
                "are authoritative; unrelated user/agent changes are not included in the path"
            ),
        },
        "harness": {
            "name": "Claude Code",
            "version": CLAUDE_CODE_VERSION,
            "install": f"official bootstrap pinned to {CLAUDE_CODE_VERSION}",
            "install_only_preflight": (
                "passed in the html-js-filter TB4 image; Harbor agent_info reported 2.1.251"
            ),
            "entry": (
                "claude --verbose --output-format=stream-json --permission-mode="
                "bypassPermissions --effort high --print"
            ),
            "launch_behavior": "Harbor built-in; no custom agent adapter",
        },
        "model_paths": {
            name: {
                "logical_model": name,
                "requested_model_id": model["request_model"],
                "claude_code_cli_model": model["cli_model"],
                "expected_actual_model_permaslug": model["actual_model"],
                "provider": f"OpenRouter pinned to {model['provider']}",
                "provider_endpoint_tag": model["provider_tag"],
                "quantization": model["quantization"],
                "endpoint_snapshot": {
                    "checked_at_utc": "2026-08-31T07:00:00Z",
                    "status": "active in the last exact-control qualification",
                    "provider_name": model["provider"],
                    "context_length": model["context_length"],
                    "max_completion_tokens": model["max_completion_tokens"],
                },
                "upstream_base_url": UPSTREAM_URL,
                "upstream_endpoint": "https://openrouter.ai/api/v1/messages",
                "protocol": "Anthropic Messages with raw SSE",
                "classification": "provider-compatible plus transparent body injection",
                "translated": False,
                "route": model["route"],
                "route_note": (
                    "require_parameters=true only for Anthropic and false for cross-family "
                    "endpoints; provider.only, declared quantization, and fallback=false remain strict"
                ),
                "terminal_loop_evidence": (
                    "passed the exact 1M/high route and tool loop in Claude Code controls; "
                    "the prior 64K output override is not reused in this campaign"
                ),
            }
            for name, model in MODELS.items()
        },
        "controls": {
            "reasoning_effort": "high via Claude Code --effort high",
            "thinking_budget": "Claude Code/provider adaptive default; MAX_THINKING_TOKENS unset",
            "context_window": (
                "1,000,000 study control; Claude uses official [1m] selector and all paths set "
                "CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000"
            ),
            "compaction": (
                "CLAUDE_CODE_AUTO_COMPACT_WINDOW=1000000; Claude Code native policy and "
                "recovery behavior retained and audited"
            ),
            "output_budget": (
                "Claude Code native behavior; CLAUDE_CODE_MAX_OUTPUT_TOKENS is unset and no "
                "study output cap is imposed. The endpoint-advertised maxima remain metadata "
                "only; exact request fields, stop reasons, and cap hits are audited per arm."
            ),
            "max_turns": "unset / Claude Code default",
            "budget_management": {
                "upstream_default": "no per-session dollar cap",
                "harness_override": "none; Claude Code default retained",
                "campaign_safety": "monitor and report only; no dollar-triggered process kill",
                "accepted_exposure_usd": ACCEPTED_EXPOSURE_USD,
                "warning": "OpenRouter settlement is delayed and the key may have unrelated concurrent use",
            },
            "task_timeout": "task-native agent timeout 28800 seconds",
            "request_timeout": (
                "injector connect timeout 30 seconds; no total/sock-read timeout after "
                "the upstream stream is established"
            ),
            "retries": {
                "harbor_trial": 0,
                "injector": 0,
                "claude_code_native": "upstream default; every upstream attempt logged",
            },
            "temperature": "omitted by Claude Code; fixed provider default",
            "top_p": "omitted by Claude Code; fixed provider default",
            "seed": None,
            "tools": "Claude Code installed defaults; no allow/disallow override",
            "skills": "no project/Harbor skills; Claude Code built-in catalog remains default",
            "mcp_servers": [],
            "subagents": (
                "Claude Code default availability; each arm pins main/default/subagent routing "
                "to the same model, and Claude retains the [1m] selector"
            ),
            "memory": "isolated per-trial CLAUDE_CONFIG_DIR; no prior user history",
            "prompt_files": [],
            "benchmark_specific_augmentation": False,
            "extra_instructions": [],
        },
        "sandbox": {
            "provider": "local Docker via Harbor",
            "host": {"cpus": 128, "memory_bytes": 1081331798016},
            "per_task_resources_and_images": "see scope.tasks",
            "resource_overrides": None,
            "network_policy": (
                "task-default Docker outbound network; host.docker.internal added only to reach "
                "the local compatibility proxy"
            ),
            "permission_mode": (
                "bypassPermissions inside the benchmark container, set by Harbor; Docker is the "
                "outer sandbox"
            ),
        },
        "compatibility_layer": {
            "listeners": {
                name: container_injector_url(name) for name in MODELS
            },
            "request_change": "add exactly one top-level provider object",
            "unchanged": (
                "model, messages, system, tools, thinking, metadata, headers, response status, "
                "response body, and SSE chunk bytes"
            ),
            "retries": 0,
            "prompt_or_response_logging": False,
            "compose_overlay": compose_overlay(),
        },
        "accounting": {
            "primary": (
                "OpenRouter per-generation records after settlement; current-key delta is diagnostic only"
            ),
            "secondary": [
                "Claude Code native stream-json/modelUsage",
                "Harbor source-qualified AgentResult aggregation",
                "transparent injector one-record-per-upstream-attempt audit",
            ],
            "litellm": "not present on this path",
            "double_count_rule": "the same HTTP request is never summed across sources",
            "tokens": "input, cached input, output, and reasoning preserved separately when available",
            "estimated_provider_cost_usd": (
                EXPECTED_COST_USD
            ),
            "safety_bound": (
                f"accepted exposure ${ACCEPTED_EXPOSURE_USD:.2f}; no in-process dollar kill, "
                "because delayed shared-key accounting previously killed valid verifier work"
            ),
            "shared_key_note": (
                "other paid campaigns may overlap, so account-level before/after delta is not "
                "attributable and must not be used as this campaign's cost"
            ),
        },
        "launch_gate": {
            "condition": (
                "source hashes, manifest hash, ports, and key presence must pass; shared-key overlap is allowed"
            ),
            "reason": (
                "user requested fast pilot execution while Codex/OpenHands/DSH pilots remain active"
            ),
            "known_overlapping_campaigns_at_freeze": [
                "pilot-tb4-codex-five-model-20260831-r10",
                "pilot-tb4-openhands-openrouter-four-model-20260831-r1",
                "pilot-tb4-dsh-openrouter-other-models-20260831-r1"
            ],
            "interpretation_limit": (
                "429, latency, and concurrency observations cannot qualify an isolated formal "
                "concurrency setting; classify shared-load rate limits as infrastructure failures"
            ),
            "check": "read-only process/port/source/manifest audit immediately before launch",
            "owner": "root operator",
        },
        "post_run_audit": {
            "required": True,
            "scope": "all eight pilot trajectories",
            "checks": [
                "actual model/provider/endpoint and request retry count",
                "tool and thinking continuity across every turn",
                "unexpected skills, MCP, subagents, or prompt injection",
                "compaction/truncation/budget-stop/loop/timeout events",
                "provider 429s, verifier errors, malformed or missing artifacts",
                "provider versus Claude Code versus Harbor token/cost disagreement",
            ],
        },
        "source_sha256": {
            "pilot/tb4_claude_code_glm.py": sha256_file(Path(__file__)),
            "pilot/claude_openrouter_pin_smoke.py": sha256_file(
                ROOT / "pilot" / "claude_openrouter_pin_smoke.py"
            ),
            "pilot/provider_smoke.py": sha256_file(
                ROOT / "pilot" / "provider_smoke.py"
            ),
        },
    }
    manifest["manifest_sha256"] = hashlib.sha256(canonical_json(manifest)).hexdigest()
    return manifest


def harbor_command(config_path: Path, *, print_config: bool = False) -> list[str]:
    command = [
        "uvx",
        "--from",
        f"harbor=={HARBOR_VERSION}",
        "harbor",
        "run",
        "--config",
        str(config_path),
    ]
    if print_config:
        command.append("--print-config")
    else:
        command.append("--yes")
    return command


def tool_env(api_key: str | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["UV_CACHE_DIR"] = UV_CACHE_DIR
    env["UV_TOOL_DIR"] = UV_TOOL_DIR
    if api_key is not None:
        env["OPENROUTER_API_KEY"] = api_key
    return env


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        self.send_response(204)
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        return


def docker_host_gateway_preflight() -> None:
    port = INJECTOR_BASE_PORT + len(MODELS) + 1
    server = ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        probe = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--add-host",
                "host.docker.internal:host-gateway",
                "harborframework/terminal-bench:html-js-filter-environment-84a8dc9541acfe09@sha256:72aefd5ceb952e93e1fbc6e14787b9b91975adc218912b73ab3fb86e335fdcbf",
                "python",
                "-c",
                (
                    "import urllib.request; "
                    f"assert urllib.request.urlopen('http://host.docker.internal:{port}/').status == 204"
                ),
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if probe.returncode != 0:
            raise RuntimeError("Docker host-gateway probe failed: " + probe.stdout + probe.stderr)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def preflight() -> int:
    manifest = build_manifest()
    injector.offline_self_test()
    docker_host_gateway_preflight()
    with tempfile.TemporaryDirectory(prefix="tb4-claude-four-preflight-", dir="/tmp") as raw:
        directory = Path(raw)
        overlay_path = directory / "host-gateway.compose.json"
        config_path = directory / "job-config.json"
        write_json(overlay_path, compose_overlay())
        write_json(config_path, harbor_config(overlay_path=overlay_path))
        completed = subprocess.run(
            harbor_command(config_path, print_config=True),
            cwd=ROOT,
            env=tool_env("preflight-not-a-secret"),
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if completed.returncode != 0:
            print(completed.stdout[-4000:] + completed.stderr[-4000:], file=sys.stderr)
            return 2
        try:
            resolved = json.loads(completed.stdout)
        except json.JSONDecodeError:
            print("Harbor --print-config did not return JSON", file=sys.stderr)
            print(completed.stdout[-4000:], file=sys.stderr)
            return 2
        # Harbor omits fields equal to schema defaults from --print-config.
        if resolved.get("n_concurrent_trials", 4) != N_CONCURRENT:
            raise RuntimeError(
                "Harbor resolved the wrong concurrency: "
                + json.dumps(resolved, sort_keys=True)[:4000]
            )
        agents = resolved.get("agents") or []
        if len(agents) != len(MODELS) or any(
            agent.get("kwargs", {}).get("version") != CLAUDE_CODE_VERSION
            for agent in agents
        ):
            raise RuntimeError("Harbor resolved the wrong Claude Code agent set/version")
        by_model = {agent.get("model_name"): agent for agent in agents}
        for logical_model, model in MODELS.items():
            agent = by_model.get(model["cli_model"])
            if not agent:
                raise RuntimeError(f"Harbor lost model arm {logical_model}")
            expected_env = agent_config(logical_model)["env"]
            actual_env = agent.get("env") or {}
            controls = {
                key: value
                for key, value in expected_env.items()
                if key != "ANTHROPIC_AUTH_TOKEN"
            }
            changed = {
                key: {"expected": value, "actual": actual_env.get(key)}
                for key, value in controls.items()
                if actual_env.get(key) not in {value, "****"}
            }
            if changed:
                raise RuntimeError(
                    f"Harbor changed environment controls for {logical_model}: "
                    + json.dumps(changed, sort_keys=True)
                )
            injector_spec(logical_model)
    print("Zero-cost preflight passed: four injectors, Docker host gateway, Harbor config")
    print(f"Prospective manifest SHA256: {manifest['manifest_sha256']}")
    print("No model API call was made")
    return 0


def current_key_usage(api_key: str) -> dict[str, Any]:
    request = urllib.request.Request(
        "https://openrouter.ai/api/v1/auth/key",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.loads(response.read()).get("data", {})
    return {
        key: data.get(key)
        for key in (
            "usage",
            "usage_daily",
            "usage_weekly",
            "usage_monthly",
            "limit",
            "limit_remaining",
            "is_free_tier",
        )
    }


def run_harbor(args: list[str], *, env: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """Run Harbor without a dollar-triggered kill; retain one outer wall timeout."""
    started = time.monotonic()
    stop_reason = "harbor-exited"
    with (RUN_DIR / "harbor-console.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            args,
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            return_code = process.wait(timeout=WHOLE_RUN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            stop_reason = "whole-run-timeout"
            return_code = 124
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        if return_code is None:
            return_code = process.returncode
    return return_code, {
        "source": "runner-wall-clock-only",
        "dollar_stop": None,
        "accepted_exposure_usd": ACCEPTED_EXPOSURE_USD,
        "whole_run_timeout_seconds": WHOLE_RUN_TIMEOUT_SECONDS,
        "stop_reason": stop_reason,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "warning": "Provider cost is audited after settlement and does not kill verifier work.",
    }


def start_injector(
    logical_model: str, spec_path: Path
) -> tuple[subprocess.Popen[str], Any]:
    port = injector_port(logical_model)
    process_log = (RUN_DIR / f"provider-injector-{logical_model}.process.log").open(
        "w", encoding="utf-8"
    )
    process = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "pilot" / "claude_openrouter_pin_smoke.py"),
            "_proxy",
            "--spec",
            str(spec_path),
            "--host",
            "0.0.0.0",
            "--port",
            str(port),
            "--upstream",
            UPSTREAM_URL,
            "--log-path",
            str(RUN_DIR / f"provider-injector-{logical_model}.jsonl"),
        ],
        cwd=ROOT,
        stdout=process_log,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        injector.wait_for_port(process, port)
    except Exception:
        stop_process(process)
        process_log.close()
        raise
    return process, process_log


def stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def find_trial_results() -> list[tuple[Path, dict[str, Any]]]:
    results: list[tuple[Path, dict[str, Any]]] = []
    if not HARBOR_JOB_DIR.exists():
        return results
    for path in sorted(HARBOR_JOB_DIR.glob("*/result.json")):
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("task_name"):
            results.append((path.parent, payload))
    return results


def post_run_audit(api_key: str, *, harbor_return_code: int) -> dict[str, Any]:
    injector_by_model = {
        name: smoke.parse_json_lines(RUN_DIR / f"provider-injector-{name}.jsonl")
        for name in MODELS
    }
    trials: list[dict[str, Any]] = []
    for trial_dir, result in find_trial_results():
        result_model = (
            ((result.get("agent_info") or {}).get("model_info") or {}).get("name")
        )
        logical_model = next(
            (
                name
                for name, model in MODELS.items()
                if result_model in {model["cli_model"], model["request_model"]}
            ),
            None,
        )
        generation = smoke.fetch_openrouter_generation_records(trial_dir, api_key)
        text_paths = [
            path
            for path in trial_dir.rglob("*")
            if path.is_file()
            and path.suffix in {".json", ".jsonl", ".log"}
            and path.stat().st_size <= 50 * 1024 * 1024
        ]
        combined = "\n".join(path.read_text(errors="replace") for path in text_paths)
        records = generation.get("records") or []
        trials.append(
            {
                "trial_name": result.get("trial_name"),
                "task_name": result.get("task_name"),
                "logical_model": logical_model,
                "claude_code_model": result_model,
                "task_checksum": result.get("task_checksum"),
                "reward": (result.get("verifier_result") or {}).get("rewards"),
                "exception_info": result.get("exception_info"),
                "agent_result": result.get("agent_result"),
                "generation_ids": generation.get("generation_ids_found"),
                "generation_lookup_errors": generation.get("errors"),
                "provider_records": records,
                "provider_cost_usd": sum(
                    float(item.get("total_cost") or item.get("usage") or 0) for item in records
                ),
                "actual_models": sorted(
                    {str(item.get("model")) for item in records if item.get("model")}
                ),
                "actual_providers": sorted(
                    {str(item.get("provider_name")) for item in records if item.get("provider_name")}
                ),
                "native_session_jsonl_count": sum(
                    path.suffix == ".jsonl" and "sessions" in path.parts for path in text_paths
                ),
                "compaction_string_occurrences": combined.lower().count("compact"),
                "rate_limit_string_occurrences": combined.lower().count("429")
                + combined.lower().count("rate_limit"),
                "budget_stop_seen": any(
                    marker in combined.lower()
                    for marker in (
                        "error_max_budget_usd",
                        "max budget exceeded",
                        "maximum budget reached",
                    )
                ),
            }
        )
    return {
        "campaign_id": CAMPAIGN_ID,
        "manifest_sha256": build_manifest()["manifest_sha256"],
        "harbor_return_code": harbor_return_code,
        "decision": "pending operator review; pilot data must not enter primary scores",
        "audit_scope": "all produced pilot trials",
        "counts": {
            "planned_trials": len(TASKS) * len(MODELS),
            "result_trials": len(trials),
            "exceptions": sum(item["exception_info"] is not None for item in trials),
        },
        "injectors": {
            name: {
                "all_request_count": len(records),
                "messages_request_count": len(
                    messages := [item for item in records if item.get("path") == "/v1/messages"]
                ),
                "all_messages_changed_only_provider": bool(messages)
                and all(
                    item.get("changed_fields") == ["provider"]
                    and item.get("semantic_parity_except_provider") is True
                    for item in messages
                ),
                "retry_count": sum(int(item.get("retry_count", 0)) for item in records),
                "upstream_statuses": [item.get("upstream_status") for item in messages],
            }
            for name, records in injector_by_model.items()
        },
        "trials": trials,
        "required_manual_checks": [
            "inspect every native Claude Code JSONL plus ATIF trajectory",
            "classify compaction markers structurally rather than by string count",
            "check tool/thinking continuity, loops, unexpected skills/MCP/subagents, and verifier logs",
            "refresh delayed OpenRouter generation records and reconcile key delta before acceptance",
        ],
    }


def run_approved(approved_manifest_sha256: str) -> int:
    if RUN_DIR.exists():
        print(f"ERROR immutable run directory already exists: {RUN_DIR}", file=sys.stderr)
        return 2
    manifest = build_manifest()
    if approved_manifest_sha256 != manifest["manifest_sha256"]:
        print(
            "ERROR approval hash mismatch; expected " + manifest["manifest_sha256"],
            file=sys.stderr,
        )
        return 2
    api_key = smoke.load_env_value(ROOT / ".env", "OPENROUTER_API_KEY")
    if not api_key:
        print("ERROR OPENROUTER_API_KEY is missing from .env", file=sys.stderr)
        return 2

    RUN_DIR.mkdir(parents=True)
    overlay_path = RUN_DIR / "host-gateway.compose.json"
    config_path = RUN_DIR / "harbor-job-config.json"
    write_json(RUN_DIR / "manifest.json", manifest, mode=0o444)
    write_json(
        RUN_DIR / "approval.json",
        {
            "campaign_id": CAMPAIGN_ID,
            "approved_manifest_sha256": approved_manifest_sha256,
            "recorded_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        mode=0o444,
    )
    write_json(overlay_path, compose_overlay(), mode=0o444)
    write_json(config_path, harbor_config(overlay_path=overlay_path), mode=0o444)
    spec_paths: dict[str, Path] = {}
    for logical_model in MODELS:
        spec_path = RUN_DIR / f"injector-spec-{logical_model}.json"
        write_json(spec_path, injector_spec(logical_model), mode=0o444)
        spec_paths[logical_model] = spec_path

    before = current_key_usage(api_key)
    write_json(RUN_DIR / "openrouter-key-usage-before.json", before, mode=0o444)
    processes: list[tuple[subprocess.Popen[str], Any]] = []
    harbor_return_code = 125
    try:
        for logical_model, spec_path in spec_paths.items():
            processes.append(start_injector(logical_model, spec_path))
        harbor_return_code, budget_monitor = run_harbor(
            harbor_command(config_path),
            env=tool_env(api_key),
        )
        write_json(RUN_DIR / "budget-monitor.json", budget_monitor, mode=0o444)
    finally:
        for process, process_log in reversed(processes):
            stop_process(process)
            process_log.close()

    after = current_key_usage(api_key)
    key_usage = {"source": "openrouter-current-key", "before": before, "after": after}
    if isinstance(before.get("usage"), (int, float)) and isinstance(
        after.get("usage"), (int, float)
    ):
        key_usage["campaign_usage_delta_usd"] = after["usage"] - before["usage"]
    write_json(RUN_DIR / "openrouter-key-usage.json", key_usage, mode=0o444)
    write_json(
        RUN_DIR / "audit.json",
        post_run_audit(api_key, harbor_return_code=harbor_return_code),
        mode=0o444,
    )
    print(f"Harbor return code: {harbor_return_code}")
    print(f"Raw job: {HARBOR_JOB_DIR}")
    print(f"Audit: {RUN_DIR / 'audit.json'}")
    print("Mandatory trajectory audit remains before accepting the pilot")
    return harbor_return_code


def print_manifest() -> int:
    manifest = build_manifest()
    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("manifest", help="print the prospective immutable manifest")
    commands.add_parser("preflight", help="zero-cost integration preflight")
    run = commands.add_parser("run", help="launch after explicit hash approval")
    run.add_argument("--approved-manifest-sha256", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "manifest":
        return print_manifest()
    if args.command == "preflight":
        return preflight()
    if args.command == "run":
        return run_approved(args.approved_manifest_sha256)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
