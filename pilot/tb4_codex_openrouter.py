#!/usr/bin/env python3
"""Frozen Harbor/TB4 Codex × five-model OpenRouter pilot.

The five jobs share one Codex compatibility model, one tool/prompt catalog, and
one set of controls.  A pinned LiteLLM Responses proxy maps only the shared
alias to the approved OpenRouter model/provider route.  ``prepare`` and
``preflight`` are zero-cost; ``run`` requires the exact manifest hash.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import traceback
import urllib.error
import urllib.request
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN_ID = "pilot-tb4-codex-five-model-20260831-r10"
RUN_DIR = ROOT / "runs" / CAMPAIGN_ID
HARBOR_DIR = RUN_DIR / "harbor"
HARBOR_VERSION = "0.22.0"
HARBOR_COMMIT = "4407eb5"
CODEX_VERSION = "0.150.1"
LITELLM_VERSION = "1.98.0"
PROXY_IMAGE = "harness-test-provider-smoke:20260829-bridge3"
PROXY_IMAGE_ID = "ef2d35a5b577"
PROXY_PORT = 4020
PROXY_URL = f"http://host.docker.internal:{PROXY_PORT}/v1"
TB4_COMMIT = "452bf305c6daa62fc59061d22133a7cbc7c1572e"
TASKS = (
    "html-js-filter",
    "photonic-waveguide-routing",
    "music-harmony",
    "bun-sourcemap-leak",
)
TASK_CHECKSUMS = {
    "html-js-filter": "f9e9f9f97cc4ed197e51c0f79218cba93a9dac01e736e1c1076d7ac91f41f1c7",
    "photonic-waveguide-routing": "6adfe82f8b7736414535fa491affb554aa91856bda9571ad54910c83b9dd12c8",
    "music-harmony": "9623bde8b8df64f044ba142496347ceb058d393e1a2319669250312e8799783f",
    "bun-sourcemap-leak": "fb420f8f5cf1222a1644119305f67e5e3478bcc3745011904168ad447b765120",
}
TARGETS: dict[str, dict[str, Any]] = {
    "glm-5.3": {
        "model": "z-ai/glm-5.3",
        "route": {
            "only": ["z-ai/fp8"],
            "quantizations": ["fp8"],
            "allow_fallbacks": False,
            "require_parameters": False,
        },
    },
    "kimi-k3": {
        "model": "moonshotai/kimi-k3",
        "route": {
            "only": ["moonshotai/mxfp4"],
            "quantizations": ["mxfp4"],
            "allow_fallbacks": False,
            "require_parameters": False,
        },
    },
    "deepseek-v4-pro": {
        "model": "deepseek/deepseek-v4-pro-0813",
        "route": {
            "only": ["deepseek"],
            "allow_fallbacks": False,
            "require_parameters": False,
        },
    },
    "gpt-6-astra": {
        "model": "openai/gpt-6-astra",
        "route": {
            "only": ["openai"],
            "allow_fallbacks": False,
            "require_parameters": False,
        },
    },
    "claude-opus-5": {
        "model": "anthropic/claude-opus-5",
        "route": {
            "only": ["anthropic"],
            "allow_fallbacks": False,
            "require_parameters": False,
        },
    },
}
CONTEXT_WINDOW = 1_000_000
COMPACT_LIMIT = 950_000
MAX_OUTPUT_TOKENS = 128_000
CONCURRENCY = 4
UV_CACHE_DIR = "/tmp/harness-test-uv-cache"
UV_TOOL_DIR = "/tmp/harness-test-uv-tools"


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    path.chmod(mode)


def task_metadata(name: str) -> dict[str, Any]:
    path = ROOT / "vendor" / "terminal-bench" / name / "task.toml"
    raw = tomllib.loads(path.read_text())
    env = raw["environment"]
    verifier_env = raw["verifier"].get("environment", {})
    keys = ("cpus", "memory_mb", "storage_mb", "gpus", "docker_image")
    return {
        "name": name,
        "harbor_checksum": TASK_CHECKSUMS[name],
        "agent_timeout_seconds": raw["agent"]["timeout_sec"],
        "verifier_timeout_seconds": raw["verifier"]["timeout_sec"],
        "agent_environment": {key: env.get(key) for key in keys},
        "verifier_environment": {key: verifier_env.get(key) for key in keys},
    }


def overlay() -> dict[str, Any]:
    return {"services": {"main": {"extra_hosts": ["host.docker.internal:host-gateway"]}}}


def codex_config() -> dict[str, Any]:
    return {
        "model_reasoning_effort": "high",
        "model_reasoning_summary": "none",
        "model_verbosity": "low",
        "model_context_window": CONTEXT_WINDOW,
        "model_auto_compact_token_limit": COMPACT_LIMIT,
        "model_auto_compact_token_limit_scope": "total",
        "agents": {
            "default_subagent_model": "openrouter-eval",
            "default_subagent_reasoning_effort": "high",
        },
    }


def job_name(logical_model: str) -> str:
    return f"tb4--codex--{logical_model}--rep-1"


def harbor_config(logical_model: str) -> dict[str, Any]:
    return {
        "job_name": job_name(logical_model),
        "jobs_dir": str(HARBOR_DIR),
        "n_attempts": 1,
        "install_only": False,
        "timeout_multiplier": 1.0,
        "n_concurrent_trials": CONCURRENCY,
        "retry": {"max_retries": 0},
        "environment": {
            "type": "docker",
            "force_build": False,
            "delete": True,
            "cpu_enforcement_policy": "auto",
            "memory_enforcement_policy": "auto",
            "extra_docker_compose": [str(RUN_DIR / "host-gateway.compose.json")],
        },
        "agents": [
            {
                "import_path": "integrations.harbor_codex:ControlledCodex",
                "model_name": "openrouter-eval",
                "n_concurrent": CONCURRENCY,
                "skills": [],
                "resume_trajectory": False,
                "extra_allowed_hosts": ["host.docker.internal"],
                "include_logs": ["**/*"],
                "kwargs": {
                    "version": CODEX_VERSION,
                    "reasoning_effort": "high",
                    "reasoning_summary": "none",
                    "config": codex_config(),
                },
                "env": {
                    "OPENAI_API_KEY": "local-proxy-not-secret",
                    "OPENAI_BASE_URL": PROXY_URL,
                },
                "mcp_servers": [],
            }
        ],
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


def proxy_config(logical_model: str) -> dict[str, Any]:
    target = TARGETS[logical_model]
    params = {
        # LiteLLM uses this prefix only to select its OpenRouter provider;
        # OpenRouter receives target["model"] after LiteLLM strips the prefix.
        "model": f"openrouter/{target['model']}",
        "api_key": "os.environ/OPENROUTER_API_KEY",
        "api_base": "https://openrouter.ai/api/v1",
        "use_chat_completions_api": False,
        "num_retries": 0,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "allowed_openai_params": ["reasoning_effort"],
        "additional_drop_params": ["parallel_tool_calls"],
        "extra_headers": {"X-OpenRouter-Metadata": "enabled"},
        "extra_body": {"provider": target["route"]},
    }
    return {
        "model_list": [
            {"model_name": "openrouter-eval", "litellm_params": params}
        ],
        "litellm_settings": {"num_retries": 0},
        "router_settings": {
            "num_retries": 0,
            # A failure in one concurrent trial must not make LiteLLM reject
            # another trial locally. Codex retains ownership of its native
            # request/stream recovery behavior.
            "disable_cooldowns": True,
        },
    }


def manifest() -> dict[str, Any]:
    data: dict[str, Any] = {
        "campaign_id": CAMPAIGN_ID,
        "kind": "paid-real-benchmark-pilot-not-primary-result",
        "benchmark": {
            "name": "Terminal-Bench 4.0",
            "tag": "v4.0.0",
            "commit": TB4_COMMIT,
            "tasks": [task_metadata(name) for name in TASKS],
            "replicate": 1,
            "planned_trials": len(TASKS) * len(TARGETS),
            "oracle_preflight": "same four tasks previously passed 4/4",
        },
        "runner": {
            "name": "Harbor",
            "version": HARBOR_VERSION,
            "commit": HARBOR_COMMIT,
            "integration": (
                "Harbor built-in Codex subclassed only to materialize the shared "
                "model catalog; no custom agent loop"
            ),
            "job_configs": {
                name: harbor_config(name) for name in TARGETS
            },
            "outer_trial_retries": 0,
        },
        "harness": {
            "name": "Codex CLI",
            "version": CODEX_VERSION,
            "install": f"npm @openai/codex@{CODEX_VERSION}",
            "entry": (
                "codex exec --dangerously-bypass-approvals-and-sandbox "
                "--skip-git-repo-check --model openrouter-eval --json "
                "--enable unified_exec -c model_reasoning_effort=high"
            ),
            "catalog_baseline": "bundled gpt-6-astra from the pinned binary",
            "catalog_changes": {
                "slug": "openrouter-eval",
                "context_window": CONTEXT_WINDOW,
                "max_context_window": CONTEXT_WINDOW,
                "effective_context_window_percent": 95,
                "use_responses_lite": False,
                "tool_mode": None,
            },
            "offline_parity_evidence": (
                "runs/qualification-codex-controls-20260831/catalog-summary.json"
            ),
        },
        "model_paths": {
            name: {
                "requested_model": target["model"],
                "provider": "OpenRouter pinned official endpoint",
                "base_url": "https://openrouter.ai/api/v1",
                "protocol": "Codex Responses -> LiteLLM Responses -> OpenRouter Responses",
                "route": target["route"],
            }
            for name, target in TARGETS.items()
        },
        "controls": {
            "reasoning_effort": "high via Codex model_reasoning_effort and forwarded reasoning.effort",
            "reasoning_summary": "none; encrypted reasoning state requested and audited",
            "context_window": CONTEXT_WINDOW,
            "compaction": {
                "threshold": COMPACT_LIMIT,
                "scope": "total",
                "policy": "Codex native compaction; all events are primary outcomes",
            },
            "max_output_tokens": (
                f"{MAX_OUTPUT_TOKENS} injected uniformly by LiteLLM because Codex has no "
                "public CLI output-cap key"
            ),
            "verbosity": "low for every arm via GPT-derived catalog metadata",
            "max_turns": "unset / Codex default",
            "temperature": "unset / provider default",
            "seed": None,
            "timeouts": (
                "TB4 task-native; Harbor agent override unset; compatibility proxy adds "
                "no request timeout; Codex request/stream handling native"
            ),
            "retries": {
                "Harbor": 0,
                "LiteLLM": 0,
                "Codex_native": "request_max_retries=4 and stream_max_retries=5; audited",
            },
            "concurrency": f"four trials within one model job; model jobs run sequentially",
            "tools": (
                "identical GPT-derived Codex defaults in all arms; LiteLLM uniformly "
                "translates custom/namespace tools and omits parallel_tool_calls"
            ),
            "skills": [],
            "mcp_servers": [],
            "memory": "fresh isolated CODEX_HOME per trial",
            "extra_prompt_or_benchmark_augmentation": False,
        },
        "compatibility_layer": {
            "image": PROXY_IMAGE,
            "image_id": PROXY_IMAGE_ID,
            "litellm_version": LITELLM_VERSION,
            "changes": [
                "openrouter-eval alias -> frozen target model",
                "inject provider.only/quantization/allow_fallbacks=false",
                "inject max output 128000",
                "translate Responses custom/namespace tool shapes uniformly",
                "drop parallel_tool_calls uniformly",
            ],
            "retries": 0,
            "cooldowns": "disabled; no cross-trial deployment health state",
            "timeouts": "none added by the compatibility proxy",
        },
        "sandbox": {
            "provider": "local Docker via Harbor",
            "resources_and_image_digests": "per benchmark.tasks entries; no override",
            "network": "task default plus host.docker.internal only for local proxy",
            "task_timeout": "per task.toml",
        },
        "accounting": {
            "primary": "OpenRouter generation records and current-key usage delta",
            "secondary": ["Codex native session token events", "Harbor aggregation", "LiteLLM logs"],
            "double_count_rule": "never sum duplicate views of the same request",
            "estimated_cost_usd": "uncertain; expected below $20, approved planning envelope $60 ($3/trial)",
            "hard_dollar_cap": None,
        },
        "known_uncertainty": [
            "This standardized OpenRouter arm is not stock Codex GPT Responses Lite; a separate native GPT baseline is needed for home-field measurement.",
            "Actual provider, quantization, reasoning continuity, retries, compactions, and token/cost disagreements require post-run trajectory audit.",
            "music-harmony may expose model/image-path incompatibility and remains a valid recorded failure.",
        ],
        "zero_cost_harbor_resolution": (
            "Harbor v0.22.0 --print-config successfully resolved the shared r1 "
            "config on 2026-08-31; its CLI then left stdout open, so r4 validates "
            "the byte-equivalent job inputs locally instead of repeating that lifecycle bug"
        ),
        "prior_attempt": {
            "campaign_id": "pilot-tb4-codex-five-model-20260831-r8",
            "outcome": (
                "four GLM trials launched; one image-incompatible request triggered "
                "Codex-native recovery and exposed LiteLLM shared cooldown, then the "
                "foreground controller disappeared while another trial was active"
            ),
            "provider_requests": "preserved in immutable r8 evidence",
            "cost_usd": "preserved in immutable r8 evidence",
        },
        "post_run_audit": "all 20 trajectories before accepting any score",
        "source_sha256": {
            "pilot/tb4_codex_openrouter.py": file_sha256(Path(__file__)),
            "integrations/harbor_codex.py": file_sha256(ROOT / "integrations" / "harbor_codex.py"),
            "pilot/codex_config_parity_check.py": file_sha256(ROOT / "pilot" / "codex_config_parity_check.py"),
        },
    }
    data["manifest_sha256"] = hashlib.sha256(canonical(data)).hexdigest()
    return data


def materialize() -> dict[str, Any]:
    data = manifest()
    if RUN_DIR.exists() and (RUN_DIR / "manifest.json").exists():
        existing = json.loads((RUN_DIR / "manifest.json").read_text())
        if existing.get("manifest_sha256") != data["manifest_sha256"]:
            raise RuntimeError("refusing to mutate an existing frozen campaign")
        return existing
    write_json(RUN_DIR / "host-gateway.compose.json", overlay())
    for name in TARGETS:
        write_json(RUN_DIR / "configs" / f"harbor-{name}.json", harbor_config(name))
        # The config contains only an environment-variable reference, never the
        # key value. It must be readable by the pinned proxy image's UID 1000.
        write_json(RUN_DIR / "configs" / f"litellm-{name}.json", proxy_config(name), 0o644)
    write_json(RUN_DIR / "manifest.json", data)
    return data


def harbor_env() -> dict[str, str]:
    env = os.environ.copy()
    env["UV_CACHE_DIR"] = UV_CACHE_DIR
    env["UV_TOOL_DIR"] = UV_TOOL_DIR
    current = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(ROOT) + (os.pathsep + current if current else "")
    return env


def harbor_command(config: Path, *, print_config: bool = False) -> list[str]:
    command = [
        "uvx", "--offline", "--from", f"harbor=={HARBOR_VERSION}",
        "harbor", "run", "--config", str(config),
    ]
    command.append("--print-config" if print_config else "--yes")
    return command


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _CooldownProbeHandler(BaseHTTPRequestHandler):
    calls = 0

    def log_message(self, _format: str, *args: Any) -> None:
        del args

    def do_POST(self) -> None:  # noqa: N802 - stdlib hook name
        type(self).calls += 1
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        if type(self).calls == 1:
            status = 404
            payload: dict[str, Any] = {
                "error": {
                    "message": "intentional qualification failure",
                    "type": "invalid_request_error",
                    "code": "unsupported_input",
                }
            }
        else:
            status = 200
            payload = {
                "id": "resp_cooldown_probe",
                "object": "response",
                "created_at": int(time.time()),
                "status": "completed",
                "model": "fake-responses-model",
                "output": [
                    {
                        "id": "msg_cooldown_probe",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": "ok", "annotations": []}
                        ],
                    }
                ],
                "parallel_tool_calls": True,
                "tools": [],
                "usage": {
                    "input_tokens": 1,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens": 1,
                    "output_tokens_details": {"reasoning_tokens": 0},
                    "total_tokens": 2,
                },
            }
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def post_probe(port: int) -> int:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/responses",
        data=json.dumps(
            {"model": "openrouter-eval", "input": "reply ok", "stream": False}
        ).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read()
            return response.status
    except urllib.error.HTTPError as exc:
        exc.read()
        return exc.code


def qualify_proxy_recovery() -> dict[str, Any]:
    """Prove no proxy retry and no cross-request cooldown without paid API calls."""
    upstream_port = free_tcp_port()
    proxy_port = free_tcp_port()
    _CooldownProbeHandler.calls = 0
    server = ThreadingHTTPServer(("127.0.0.1", upstream_port), _CooldownProbeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with tempfile.TemporaryDirectory(prefix="codex-proxy-recovery-", dir="/tmp") as raw:
        temp = Path(raw)
        config = {
            "model_list": [
                {
                    "model_name": "openrouter-eval",
                    "litellm_params": {
                        "model": "openai/fake-responses-model",
                        "api_key": "not-a-secret",
                        "api_base": f"http://127.0.0.1:{upstream_port}/v1",
                        "use_chat_completions_api": False,
                        "num_retries": 0,
                    },
                }
            ],
            "litellm_settings": {"num_retries": 0},
            "router_settings": {"num_retries": 0, "disable_cooldowns": True},
        }
        config_path = temp / "litellm.json"
        write_json(config_path, config)
        log = (temp / "litellm.log").open("w", encoding="utf-8")
        container = f"{CAMPAIGN_ID}-cooldown-probe"
        process = subprocess.Popen(
            [
                "docker", "run", "--rm", "--network", "host", "--name", container,
                "--volume", f"{config_path}:/config.json:ro", PROXY_IMAGE,
                "/opt/litellm/bin/litellm", "--config", "/config.json",
                "--host", "0.0.0.0", "--port", str(proxy_port),
            ],
            cwd=ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("qualification proxy exited during startup")
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{proxy_port}/health/liveliness", timeout=1
                    ) as response:
                        if response.status == 200:
                            break
                except Exception:
                    time.sleep(0.25)
            else:
                raise TimeoutError("qualification proxy did not become ready")
            first = post_probe(proxy_port)
            first_calls = _CooldownProbeHandler.calls
            second = post_probe(proxy_port)
            second_calls = _CooldownProbeHandler.calls
            if (first, first_calls, second, second_calls) != (404, 1, 200, 2):
                raise RuntimeError(
                    "proxy retry/cooldown qualification failed: "
                    f"first={first}/{first_calls}, second={second}/{second_calls}"
                )
            result = {
                "status": "passed",
                "paid_api_calls": 0,
                "first_client_status": first,
                "upstream_calls_after_first": first_calls,
                "second_client_status": second,
                "upstream_calls_after_second": second_calls,
                "assertion": "no proxy retry and no cross-request cooldown",
            }
        finally:
            stop_proxy(process, log)
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    return result


def preflight() -> int:
    data = materialize()
    parity = json.loads(
        (ROOT / "runs/qualification-codex-controls-20260831/catalog-summary.json").read_text()
    )
    if parity.get("status") != "passed" or parity.get("paid_api_calls") != 0:
        raise RuntimeError("offline Codex catalog parity evidence is missing")
    # Harbor v0.22.0 already resolved the byte-equivalent r1 shared config and
    # printed the expected JSON. Its CLI then left a child holding stdout open;
    # avoid repeating that lifecycle bug and verify the immutable r4 inputs.
    first_name = next(iter(TARGETS))
    reference = harbor_config(first_name)
    reference.pop("job_name")
    for name in TARGETS:
        saved = json.loads(
            (RUN_DIR / "configs" / f"harbor-{name}.json").read_text()
        )
        if saved != harbor_config(name):
            raise RuntimeError(f"saved Harbor input differs from generator: {name}")
        candidate = harbor_config(name)
        candidate.pop("job_name")
        if candidate != reference:
            raise RuntimeError(f"Harbor inputs drift across model jobs: {name}")
    # Exercise Harbor's actual console entry point and custom-agent import in a
    # real TB4 container without running an agent or verifier. This is the
    # zero-cost check that catches uvx/PYTHONPATH and post-install executable
    # resolution differences missed by schema expansion alone. ControlledCodex
    # deliberately runs the complete catalog-build command during install.
    with tempfile.TemporaryDirectory(prefix="codex-r8-install-preflight-", dir="/tmp") as raw:
        temp = Path(raw)
        install_config = harbor_config(first_name)
        install_config["job_name"] = "qualification-codex-import-r8"
        install_config["jobs_dir"] = str(temp / "harbor")
        install_config["install_only"] = True
        install_config["n_concurrent_trials"] = 1
        install_config["agents"][0]["n_concurrent"] = 1
        install_config["datasets"][0]["task_names"] = [TASKS[0]]
        config_path = temp / "install-only.json"
        write_json(config_path, install_config)
        completed = subprocess.run(
            harbor_command(config_path),
            cwd=ROOT,
            env=harbor_env(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30 * 60,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError("Harbor install-only preflight failed:\n" + completed.stdout[-6000:])
    proxy_result = qualify_proxy_recovery()
    write_json(
        RUN_DIR / "preflight.json",
        {
            "status": "passed",
            "completed_at_utc": utc_now(),
            "manifest_sha256": data["manifest_sha256"],
            "paid_api_calls": 0,
            "harbor_install_only": "passed",
            "proxy_recovery": proxy_result,
        },
    )
    print("Zero-cost preflight passed; no model API call was made")
    print(f"Prospective manifest SHA256: {data['manifest_sha256']}")
    return 0


def wait_proxy(process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("LiteLLM proxy exited during startup")
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{PROXY_PORT}/health/liveliness", timeout=1
            ) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.25)
    raise TimeoutError("LiteLLM proxy did not become ready")


def start_proxy(name: str) -> tuple[subprocess.Popen[str], Any]:
    log = (RUN_DIR / f"litellm-{name}.log").open("w", encoding="utf-8")
    container = f"{CAMPAIGN_ID}-{name}".replace(".", "-")
    config = (RUN_DIR / "configs" / f"litellm-{name}.json").resolve()
    process = subprocess.Popen(
        [
            "docker", "run", "--rm", "--network", "host", "--name", container,
            "--env", "OPENROUTER_API_KEY", "--volume", f"{config}:/config.json:ro",
            PROXY_IMAGE, "/opt/litellm/bin/litellm", "--config", "/config.json",
            "--host", "0.0.0.0", "--port", str(PROXY_PORT),
        ],
        cwd=ROOT,
        env=os.environ.copy(),
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    wait_proxy(process)
    return process, log


def stop_proxy(process: subprocess.Popen[str], log: Any) -> None:
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    log.close()


def run(approved: str) -> int:
    data = materialize()
    if approved != data["manifest_sha256"]:
        raise RuntimeError("approval hash does not match the frozen manifest")
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise RuntimeError("OPENROUTER_API_KEY is not loaded")
    state = {"manifest_sha256": approved, "jobs": {}}
    write_json(RUN_DIR / "run-state.json", state)
    overall_return_code = 0
    for name in TARGETS:
        output = HARBOR_DIR / job_name(name)
        if output.exists():
            raise RuntimeError(f"refusing to reuse Harbor output {output}")
        process, log = start_proxy(name)
        try:
            completed = subprocess.run(
                harbor_command(RUN_DIR / "configs" / f"harbor-{name}.json"),
                cwd=ROOT,
                env=harbor_env(),
                timeout=9 * 60 * 60,
                check=False,
            )
        finally:
            stop_proxy(process, log)
        state["jobs"][name] = {"return_code": completed.returncode, "output": str(output)}
        result_path = output / "result.json"
        n_errored_trials = None
        if result_path.is_file():
            result = json.loads(result_path.read_text())
            n_errored_trials = result.get("stats", {}).get("n_errored_trials")
            state["jobs"][name]["n_errored_trials"] = n_errored_trials
        write_json(RUN_DIR / "run-state.json", state)
        if completed.returncode or n_errored_trials:
            overall_return_code = completed.returncode or 1
            print(
                f"Recorded failed trials for independent model job {name}; continuing",
                file=sys.stderr,
            )
    print("All jobs finished. Trajectory/log audit is mandatory before scores are accepted.")
    return overall_return_code


def tmux_session_name() -> str:
    return CAMPAIGN_ID.replace(".", "-")


def launch(approved: str) -> int:
    data = materialize()
    if approved != data["manifest_sha256"]:
        raise RuntimeError("approval hash does not match the frozen manifest")
    preflight_path = RUN_DIR / "preflight.json"
    if not preflight_path.is_file():
        raise RuntimeError("zero-cost preflight evidence is missing")
    preflight_result = json.loads(preflight_path.read_text())
    if (
        preflight_result.get("status") != "passed"
        or preflight_result.get("manifest_sha256") != approved
        or preflight_result.get("paid_api_calls") != 0
    ):
        raise RuntimeError("zero-cost preflight evidence does not match this manifest")
    if not os.environ.get("OPENROUTER_API_KEY") and not (ROOT / ".env").is_file():
        raise RuntimeError("OPENROUTER_API_KEY is not loaded and .env is unavailable")
    session = tmux_session_name()
    exists = subprocess.run(
        ["tmux", "has-session", "-t", session],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if exists.returncode == 0:
        raise RuntimeError(f"tmux session already exists: {session}")
    inner = (
        f"set -a; source {shlex.quote(str(ROOT / '.env'))}; set +a; exec "
        + shlex.join(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "_controller",
                "--approved-manifest-sha256",
                approved,
            ]
        )
    )
    command = shlex.join(["/bin/bash", "-lc", inner])
    write_json(
        RUN_DIR / "launcher.json",
        {
            "campaign_id": CAMPAIGN_ID,
            "manifest_sha256": approved,
            "launched_at_utc": utc_now(),
            "tmux_session": session,
            "controller_command": "environment loaded from protected .env; secrets omitted",
        },
    )
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session, "-c", str(ROOT), command],
        check=True,
    )
    time.sleep(1)
    print(f"Launched persistent controller in tmux session {session}")
    print(f"Status: {sys.executable} {Path(__file__).resolve()} status")
    return 0


def controller(approved: str) -> int:
    log_path = RUN_DIR / "controller.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8", buffering=1) as log:
        os.dup2(log.fileno(), sys.stdout.fileno())
        os.dup2(log.fileno(), sys.stderr.fileno())
        state = {
            "campaign_id": CAMPAIGN_ID,
            "manifest_sha256": approved,
            "pid": os.getpid(),
            "started_at_utc": utc_now(),
            "status": "running",
        }
        write_json(RUN_DIR / "controller-state.json", state)
        return_code = 1
        try:
            return_code = run(approved)
            return return_code
        except BaseException:
            traceback.print_exc()
            raise
        finally:
            state["finished_at_utc"] = utc_now()
            state["return_code"] = return_code
            state["status"] = "completed" if return_code == 0 else "failed"
            write_json(RUN_DIR / "controller-state.json", state)


def status() -> int:
    session = tmux_session_name()
    alive = subprocess.run(
        ["tmux", "has-session", "-t", session],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0
    summary: dict[str, Any] = {"campaign_id": CAMPAIGN_ID, "tmux_alive": alive}
    for filename, key in (
        ("launcher.json", "launcher"),
        ("controller-state.json", "controller"),
        ("run-state.json", "run"),
    ):
        path = RUN_DIR / filename
        if path.is_file():
            summary[key] = json.loads(path.read_text())
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("prepare")
    sub.add_parser("preflight")
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--approved-manifest-sha256", required=True)
    launch_parser = sub.add_parser("launch")
    launch_parser.add_argument("--approved-manifest-sha256", required=True)
    controller_parser = sub.add_parser("_controller")
    controller_parser.add_argument("--approved-manifest-sha256", required=True)
    sub.add_parser("status")
    args = parser.parse_args()
    if args.command == "prepare":
        print(materialize()["manifest_sha256"])
        return 0
    if args.command == "preflight":
        return preflight()
    if args.command == "launch":
        return launch(args.approved_manifest_sha256)
    if args.command == "_controller":
        return controller(args.approved_manifest_sha256)
    if args.command == "status":
        return status()
    return run(args.approved_manifest_sha256)


if __name__ == "__main__":
    raise SystemExit(main())
