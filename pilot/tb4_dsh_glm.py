#!/usr/bin/env python3
"""One-task TB4 qualification: DSH standard -> OpenRouter-pinned GLM-5.3."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

import provider_smoke as smoke
import tb4_claude_code_glm as common


ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN_ID = "pilot-tb4-dsh-glm-b-qualification-20260831-r1"
PREPARED_AT_UTC = "2026-08-31T06:33:07Z"
RUN_DIR = ROOT / "runs" / CAMPAIGN_ID
HARBOR_JOB_NAME = "terminal-bench-4--deepseek-harness--glm-5.3--rep-1"
HARBOR_JOBS_DIR = RUN_DIR / "harbor"
HARBOR_JOB_DIR = HARBOR_JOBS_DIR / HARBOR_JOB_NAME
TASKS = ("bun-sourcemap-leak",)
HARBOR_VERSION = "0.22.0"
DSH_VERSION = "0.1.1-rc.2"
MODEL = "openrouter/z-ai/glm-5.3"
EXPECTED_MODEL = "z-ai/glm-5.3-20260816"
ROUTE = {
    "only": ["z-ai/fp8"],
    "quantizations": ["fp8"],
    "allow_fallbacks": False,
    "require_parameters": True,
}
N_CONCURRENT = 1
WHOLE_RUN_TIMEOUT_SECONDS = 9 * 60 * 60
UV_CACHE_DIR = "/tmp/harness-test-uv-cache"
UV_TOOL_DIR = "/tmp/harness-test-uv-tools"


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    path.chmod(mode)


def harbor_config(*, task_names: tuple[str, ...] = TASKS, install_only: bool = False) -> dict[str, Any]:
    return {
        "job_name": HARBOR_JOB_NAME + ("--install-only" if install_only else ""),
        "jobs_dir": str(
            HARBOR_JOBS_DIR if not install_only else Path("/tmp/dsh-install-only")
        ),
        "n_attempts": 1,
        "install_only": install_only,
        "timeout_multiplier": 1.0,
        "n_concurrent_trials": 1 if install_only else N_CONCURRENT,
        "retry": {"max_retries": 0},
        "environment": {
            "type": "docker",
            "force_build": False,
            "delete": True,
            "cpu_enforcement_policy": "auto",
            "memory_enforcement_policy": "auto",
        },
        "verifier": {},
        "agents": [
            {
                "name": "integrations.harbor_deepseek:DeepSeekHarness",
                "model_name": MODEL,
                "n_concurrent": 1 if install_only else N_CONCURRENT,
                "skills": [],
                "resume_trajectory": False,
                "include_logs": ["**/*"],
                "kwargs": {
                    "version": DSH_VERSION,
                    "adapter": "pi-ai",
                    "model_api": "openai-completions",
                    "context_window": 1_048_576,
                    "max_tokens": 131_072,
                    "reasoning_effort": "high",
                    "compat": {
                        "supportsDeveloperRole": False,
                        "maxTokensField": "max_tokens",
                        "thinkingFormat": "openrouter",
                    },
                    "openrouter_route": ROUTE,
                    "permission_mode": "danger-full-access",
                },
                "env": {"OPENROUTER_API_KEY": "${OPENROUTER_API_KEY}"},
                "mcp_servers": [],
            }
        ],
        "datasets": [
            {
                "path": str(ROOT / "vendor" / "terminal-bench"),
                "task_names": list(task_names),
            }
        ],
        "tasks": [],
        "artifacts": [],
        "extra_instruction_paths": [],
        "extra_instructions": [],
    }


def build_manifest() -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "campaign_id": CAMPAIGN_ID,
        "kind": "paid-real-benchmark-pilot-not-for-primary-scores",
        "prepared_at_utc": PREPARED_AT_UTC,
        "scope": {
            "benchmark": "Terminal-Bench 4.0",
            "dataset_tag": "v4.0.0",
            "dataset_commit": common.TB4_COMMIT,
            "tasks": [common.load_task_metadata(name) for name in TASKS],
            "replicate": 1,
            "planned_trials": len(TASKS),
            "n_attempts": 1,
            "concurrency": N_CONCURRENT,
        },
        "runner": {
            "name": "Harbor",
            "version": HARBOR_VERSION,
            "commit": "4407eb5",
            "entry": "uvx --offline --from harbor==0.22.0 harbor run --config <frozen-config> --yes",
            "integration": "custom thin installed-agent adapter",
            "resolved_job_config": harbor_config(),
        },
        "harness": {
            "name": "DeepSeek Harness",
            "version": DSH_VERSION,
            "source": "npm @deepseek-ai/dsh@0.1.1-rc.2",
            "upstream_commit": "b150a551b8d465e31e418e1b2eaf5e79bbb7d28e",
            "entry": "dsh --profile headless --patch <resolved> <task-instruction>",
            "profile": "headless one-shot",
            "agent_preset": "upstream shipped standard",
            "standard_mount_patch_sha256": "b072cb7582dcb1c6baa948f4a292d748eed75f349e878842c50791908a801622",
            "tools": "26 tools from upstream standard preset; no deletions or additions",
            "log_lifecycle_compatibility": (
                "fresh DSH_HOME is outside Harbor's log scrub tree; a 1-second and "
                "exit-time chmod guard changes only log readability so Harbor can scrub "
                "and preserve root-created session files"
            ),
        },
        "model_path": {
            "requested_model_id": "z-ai/glm-5.3",
            "expected_actual_model": EXPECTED_MODEL,
            "gateway": "OpenRouter pinned to Z.AI",
            "base_url": "https://openrouter.ai/api/v1",
            "protocol": "OpenAI Chat Completions with SSE",
            "classification": "provider-compatible; no protocol translation",
            "route": ROUTE,
            "endpoint_snapshot": {
                "checked_at_utc": PREPARED_AT_UTC,
                "provider_name": "Z.AI",
                "tag": "z-ai/fp8",
                "quantization": "fp8",
                "status": 0,
                "context_length": 1_048_576,
                "max_completion_tokens": 131_072,
                "supported": ["reasoning_effort", "max_tokens", "tools", "tool_choice"],
            },
            "compatibility": {
                "body_injector": (
                    "localhost Node stream proxy adds only provider; no protocol, prompt, "
                    "tool, thinking, response, retry, or timeout changes; it records "
                    "X-Generation-Id when upstream headers arrive"
                ),
                "body_injector_sha256": sha256_file(
                    ROOT / "integrations" / "openrouter_body_injector.mjs"
                ),
                "max_tokens_field": "name-only maxTokensField=max_tokens",
                "thinking_format": "OpenRouter reasoning.effort=high",
            },
        },
        "controls": {
            "reasoning_effort": "high via DSH pi-ai route -> OpenRouter reasoning.effort=high",
            "thinking_token_limit": "unset; model/provider high behavior",
            "context_window": 1_048_576,
            "output_budget": (
                "131072 is the GLM-5.3 model/endpoint maximum reported by Z.AI, OpenRouter, "
                "and the newer pi-ai catalog; it is declared explicitly because GLM-5.3 is "
                "absent from DSH rc.2's 2026-07-25 pi-ai catalog"
            ),
            "compaction": {
                "backend": "upstream standard compaction-basic",
                "auto": True,
                "threshold_ratio": 0.8,
                "retain_ratio": 0.16,
                "summary_max_tokens": 8192,
                "compaction_retries": 1,
                "overflow_retries": 1,
                "summary_model": "same routed model by default",
            },
            "max_turns": "unset; upstream standard agent loop",
            "task_timeout": "task-native agent timeout 28800 seconds",
            "harbor_agent_timeout_override": None,
            "timeouts": {
                "pi_ai_sdk_request": "unset; OpenAI SDK default 600 seconds",
                "dsh_stream_idle_seconds": 300,
                "tool": "upstream standard tool defaults",
                "verifier": "task-native; see scope.tasks",
                "injector": None,
                "campaign_operator_safety_seconds": WHOLE_RUN_TIMEOUT_SECONDS,
            },
            "retries": {
                "harbor_trial": 0,
                "dsh_provider": (
                    "normal default: max 5 retries for empty/rate-limit/server/timeout/transport; "
                    "500ms-10s exponential backoff, 10% jitter"
                ),
                "injector": 0,
            },
            "concurrency": N_CONCURRENT,
            "temperature": "omitted; provider default",
            "top_p": "omitted; provider default",
            "seed": None,
            "skills": "upstream standard skill mechanism present; fresh DSH_HOME has no user/project skills",
            "mcp_servers": [],
            "subagents": "upstream standard subagent/workflow tools available",
            "memory": "fresh per-trial DSH_HOME; resume disabled",
            "benchmark_specific_augmentation": False,
            "auxiliary_calls": "DSH default session-title LLM call retained and billed",
        },
        "sandbox": {
            "provider": "local Docker via Harbor",
            "per_task_resources_and_images": "see scope.tasks",
            "network_policy": "task-default Docker outbound network",
            "dsh_inner_permission": "danger-full-access",
            "safety_boundary": "Harbor task container",
            "reason": "nested Landlock/bubblewrap unavailable in ordinary Harbor Docker",
        },
        "accounting": {
            "primary": (
                "OpenRouter settled generation records for IDs captured at response headers; "
                "current-key delta is only a cross-check"
            ),
            "secondary": ["DSH native session usage", "Harbor AgentResult"],
            "title_call": "included in OpenRouter total; DSH native session usage omits it",
            "litellm": "not present",
            "double_count_rule": "same call never summed across sources",
            "estimated_provider_cost_usd": "$0.25-$3 total, advisory estimate only; no dollar hard-stop guard",
        },
        "post_run_audit": {
            "required": True,
            "scope": "the complete trajectory and every injector request",
            "checks": [
                "standard preset and 26-tool catalog",
                "actual model/provider/quantization and fallback",
                "main/title request counts, DSH retries, 429 and timeout layer",
                "tool and reasoning continuity, compaction and loops",
                "skills/MCP/prompt contamination and modality failures",
                "provider versus DSH versus Harbor token/cost disagreement",
            ],
        },
        "source_sha256": {
            "pilot/tb4_dsh_glm.py": sha256_file(Path(__file__)),
            "integrations/harbor_deepseek.py": sha256_file(
                ROOT / "integrations" / "harbor_deepseek.py"
            ),
            "integrations/openrouter_body_injector.mjs": sha256_file(
                ROOT / "integrations" / "openrouter_body_injector.mjs"
            ),
        },
    }
    manifest["manifest_sha256"] = hashlib.sha256(canonical_json(manifest)).hexdigest()
    return manifest


def harbor_command(config_path: Path, *, print_config: bool = False) -> list[str]:
    command = [
        "uvx",
        "--offline",
        "--from",
        f"harbor=={HARBOR_VERSION}",
        "harbor",
        "run",
        "--config",
        str(config_path),
        "--print-config" if print_config else "--yes",
    ]
    return command


def tool_env(api_key: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "UV_CACHE_DIR": UV_CACHE_DIR,
            "UV_TOOL_DIR": UV_TOOL_DIR,
            "OPENROUTER_API_KEY": api_key,
            "PYTHONPATH": os.pathsep.join(
                filter(None, (str(ROOT), env.get("PYTHONPATH")))
            ),
        }
    )
    return env


def load_key() -> str:
    value = dotenv_values(ROOT / ".env").get("OPENROUTER_API_KEY")
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("OPENROUTER_API_KEY is missing")
    return value.strip()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def injector_self_test(temp: Path) -> None:
    received: dict[str, Any] = {}
    response_bytes = (
        b'data: {"id":"gen-self-test","choices":[{"delta":{"content":"ok"}}]}\n\n'
        b'data: [DONE]\n\n'
    )

    class FakeUpstream(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            size = int(self.headers.get("content-length", "0"))
            received["path"] = self.path
            received["body"] = json.loads(self.rfile.read(size))
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("x-generation-id", "gen-self-test")
            self.send_header("x-request-id", "req-self-test")
            self.end_headers()
            self.wfile.write(response_bytes)

        def log_message(self, format: str, *args: Any) -> None:
            return

    fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
    thread = threading.Thread(target=fake.serve_forever, daemon=True)
    thread.start()
    injector_port = free_port()
    config_path = temp / "injector-config.json"
    log_path = temp / "injector.jsonl"
    write_json(config_path, {"model": "z-ai/glm-5.3", "provider": ROUTE}, mode=0o600)
    process = subprocess.Popen(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "host",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "-v",
            f"{ROOT / 'integrations'}:/integrations:ro",
            "-v",
            f"{temp}:/self-test",
            "harness-provider-smoke:20260829-r1",
            "node",
            "/integrations/openrouter_body_injector.mjs",
            "--config",
            "/self-test/injector-config.json",
            "--upstream",
            f"http://127.0.0.1:{fake.server_port}/api",
            "--log",
            "/self-test/injector.jsonl",
            "--port",
            str(injector_port),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{injector_port}/health", timeout=1
                ).close()
                break
            except Exception:
                if process.poll() is not None:
                    raise RuntimeError(process.stdout.read() if process.stdout else "")
                time.sleep(0.1)
        else:
            raise RuntimeError("injector self-test did not become healthy")
        original = {
            "model": "z-ai/glm-5.3",
            "messages": [{"role": "user", "content": "self-test-prompt-sentinel"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "bash",
                        "parameters": {"type": "object"},
                    },
                }
            ],
            "reasoning": {"effort": "high"},
            "max_tokens": 131_072,
            "stream": True,
        }
        request = urllib.request.Request(
            f"http://127.0.0.1:{injector_port}/v1/chat/completions",
            data=canonical_json(original),
            headers={
                "authorization": "Bearer self-test-secret-sentinel",
                "content-type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.read() != response_bytes:
                raise RuntimeError("injector did not preserve the SSE response bytes")
        forwarded = received["body"]
        if forwarded.pop("provider") != ROUTE or forwarded != original:
            raise RuntimeError("injector changed fields other than provider")
        if received["path"] != "/api/v1/chat/completions":
            raise RuntimeError("injector forwarded to the wrong OpenRouter-style path")
        log_deadline = time.monotonic() + 2
        records: list[dict[str, Any]] = []
        while time.monotonic() < log_deadline:
            if log_path.exists():
                records = smoke.parse_json_lines(log_path)
                if {item.get("event") for item in records} >= {
                    "upstream_headers",
                    "request_end",
                }:
                    break
            time.sleep(0.01)
        logs = log_path.read_text()
        if "self-test-prompt-sentinel" in logs or "self-test-secret-sentinel" in logs:
            raise RuntimeError("injector log leaked prompt or credential content")
        header_records = [item for item in records if item.get("event") == "upstream_headers"]
        end_records = [item for item in records if item.get("event") == "request_end"]
        if len(header_records) != 1 or len(end_records) != 1:
            raise RuntimeError("injector did not log both upstream headers and request end")
        if header_records[0].get("openrouter_generation_id") != "gen-self-test":
            raise RuntimeError("injector did not preserve the OpenRouter generation id")
        record = end_records[0]
        if record.get("changed_fields") != ["provider"]:
            raise RuntimeError("injector parity log is incomplete")
        if record.get("injector_retry_count") != 0 or record.get("injector_timeout_ms") is not None:
            raise RuntimeError("injector added a retry or timeout")
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        fake.shutdown()
        fake.server_close()


def log_permissions_self_test(temp: Path) -> None:
    """Reproduce root-created DSH logs and verify host-side scrub readability."""
    mounted = temp / "logs"
    mounted.mkdir(mode=0o777)
    probe = subprocess.run(
        [
            "uvx",
            "--offline",
            "--from",
            f"harbor=={HARBOR_VERSION}",
            "python",
            "-c",
            (
                "from integrations.harbor_deepseek import "
                "log_permissions_guard_shell; print(log_permissions_guard_shell())"
            ),
        ],
        cwd=ROOT,
        env=tool_env("preflight-dummy"),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if probe.returncode != 0:
        raise RuntimeError("could not load DSH log guard: " + probe.stdout + probe.stderr)
    log_guard = probe.stdout.strip() + " "
    command = (
        "set -e; "
        "mkdir -p /logs/agent/deepseek-harness/sessions/test-session "
        "/tmp/harbor-dsh-home/profiles/node_modules/private-package; "
        "chmod 700 /logs/agent/deepseek-harness/sessions "
        "/logs/agent/deepseek-harness/sessions/test-session "
        "/tmp/harbor-dsh-home/profiles/node_modules/private-package; "
        f"{log_guard}"
        "printf '%s\\n' '{\"type\":\"session\"}' > "
        "/logs/agent/deepseek-harness/sessions/test-session/session.jsonl; "
        "printf '%s\\n' '{\"event\":\"request_end\"}' > "
        "/logs/agent/deepseek-harness/openrouter-injector.jsonl; "
        "chmod 600 /logs/agent/deepseek-harness/sessions/test-session/session.jsonl "
        "/logs/agent/deepseek-harness/openrouter-injector.jsonl; "
        "sleep 1.2"
    )
    completed = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--userns=host",
            "--user",
            "0:0",
            "--entrypoint",
            "sh",
            "-v",
            f"{mounted}:/logs/agent/deepseek-harness",
            "harness-provider-smoke:20260829-r1",
            "-lc",
            command,
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "DSH log-permissions self-test container failed: "
            + completed.stdout
            + completed.stderr
        )
    try:
        for path in mounted.rglob("*"):
            path.is_file()
            if path.is_file():
                path.read_bytes()
        if (mounted / "home").exists():
            raise RuntimeError("DSH_HOME unexpectedly entered the Harbor log tree")
    finally:
        # Restore ownership only on this disposable fixture so
        # TemporaryDirectory can remove it after the host-readability check.
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--userns=host",
                "--user",
                "0:0",
                "--entrypoint",
                "sh",
                "-v",
                f"{mounted}:/fixture",
                "harness-provider-smoke:20260829-r1",
                "-lc",
                f"chown -R {os.getuid()}:{os.getgid()} /fixture",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )


def preflight() -> int:
    manifest = build_manifest()
    with tempfile.TemporaryDirectory(prefix="tb4-dsh-glm-", dir="/tmp") as raw:
        temp = Path(raw)
        log_permissions_self_test(temp)
        injector_self_test(temp)
        config_path = temp / "job.json"
        write_json(config_path, harbor_config())
        completed = subprocess.run(
            harbor_command(config_path, print_config=True),
            cwd=ROOT,
            env=tool_env("preflight-dummy"),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if completed.returncode != 0:
            print(completed.stdout[-4000:] + completed.stderr[-4000:], file=sys.stderr)
            return 2
        resolved = json.loads(completed.stdout)
        agent = (resolved.get("agents") or [{}])[0]
        kwargs = agent.get("kwargs") or {}
        if agent.get("name") != "integrations.harbor_deepseek:DeepSeekHarness":
            raise RuntimeError("Harbor resolved the wrong agent")
        if kwargs.get("reasoning_effort") != "high" or kwargs.get("openrouter_route") != ROUTE:
            raise RuntimeError("Harbor did not preserve DSH high/strict routing")

        install_path = temp / "install.json"
        install_config = harbor_config(task_names=(TASKS[0],), install_only=True)
        install_config["jobs_dir"] = str(temp / "install-only")
        write_json(
            install_path,
            install_config,
        )
        installed = subprocess.run(
            harbor_command(install_path),
            cwd=ROOT,
            env=tool_env("preflight-dummy"),
            capture_output=True,
            text=True,
            timeout=1200,
            check=False,
        )
        if installed.returncode != 0:
            print(installed.stdout[-6000:] + installed.stderr[-6000:], file=sys.stderr)
            raise RuntimeError("Harbor DSH install-only preflight failed")
    print(
        "Zero-cost preflight passed: host-readable DSH logs with HOME outside the "
        "scrub tree, provider-only injector parity/generation-id/no-retry/no-timeout, "
        "Harbor custom adapter import/install, and frozen config"
    )
    print(f"Prospective manifest SHA256: {manifest['manifest_sha256']}")
    print("No model generation was made")
    return 0


def freeze(approved_sha256: str) -> tuple[Path, dict[str, Any]]:
    manifest = build_manifest()
    if approved_sha256 != manifest["manifest_sha256"]:
        raise RuntimeError("approved manifest hash does not match resolved configuration")
    if RUN_DIR.exists():
        raise RuntimeError(f"immutable run directory already exists: {RUN_DIR}")
    RUN_DIR.mkdir(parents=True)
    config_path = RUN_DIR / "harbor-job.json"
    write_json(config_path, harbor_config())
    write_json(RUN_DIR / "manifest.json", manifest)
    return config_path, manifest


def find_trial_results() -> list[tuple[Path, dict[str, Any]]]:
    found: list[tuple[Path, dict[str, Any]]] = []
    if not HARBOR_JOB_DIR.exists():
        return found
    for path in sorted(HARBOR_JOB_DIR.glob("*/result.json")):
        try:
            result = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if result.get("task_name"):
            found.append((path.parent, result))
    return found


def audit(api_key: str, *, return_code: int) -> dict[str, Any]:
    trials: list[dict[str, Any]] = []
    for trial_dir, result in find_trial_results():
        generations = smoke.fetch_openrouter_generation_records(trial_dir, api_key)
        records = generations.get("records") or []
        session_paths = sorted(
            trial_dir.glob("agent/deepseek-harness/sessions/**/session.jsonl")
        )
        injector_paths = sorted(
            trial_dir.glob("agent/deepseek-harness/openrouter-injector.jsonl")
        )
        session_text = "\n".join(
            path.read_text(errors="replace") for path in session_paths
        )
        session_records = [
            record
            for path in session_paths
            for record in smoke.parse_json_lines(path)
        ]
        retry_records = [
            item for item in session_records if item.get("type") == "llm/retry"
        ]
        injector_records = [
            record
            for path in injector_paths
            for record in smoke.parse_json_lines(path)
        ]
        injector_end_records = [
            item for item in injector_records if item.get("event") in (None, "request_end")
        ]
        injector_header_records = [
            item for item in injector_records if item.get("event") == "upstream_headers"
        ]
        trials.append(
            {
                "trial_name": result.get("trial_name"),
                "task_name": result.get("task_name"),
                "reward": (result.get("verifier_result") or {}).get("rewards"),
                "exception_info": result.get("exception_info"),
                "agent_result": result.get("agent_result"),
                "generation_ids": generations.get("generation_ids_found"),
                "generation_lookup_errors": generations.get("errors"),
                "provider_cost_usd": sum(
                    float(item.get("total_cost") or item.get("usage") or 0)
                    for item in records
                ),
                "actual_models": sorted(
                    {str(item.get("model")) for item in records if item.get("model")}
                ),
                "actual_providers": sorted(
                    {
                        str(item.get("provider_name"))
                        for item in records
                        if item.get("provider_name")
                    }
                ),
                "dsh_session_count": len(session_paths),
                "standard_session_headers": session_text.count('"agentPreset":"standard"')
                + session_text.count('"agentPreset": "standard"'),
                "compaction_events": session_text.count('"type":"compaction/start"')
                + session_text.count('"type": "compaction/start"'),
                "dsh_retry_events": len(retry_records),
                "dsh_rate_limit_retry_events": sum(
                    "rate_limit" in canonical_json(item).decode(errors="replace").lower()
                    or '"429"' in canonical_json(item).decode(errors="replace")
                    for item in retry_records
                ),
                "injector_requests": len(injector_end_records),
                "injector_header_records": len(injector_header_records),
                "injector_generation_ids": sorted(
                    {
                        str(item.get("openrouter_generation_id"))
                        for item in injector_records
                        if item.get("openrouter_generation_id")
                    }
                ),
                "injector_non_provider_changes": sum(
                    item.get("changed_fields") not in (["provider"], [])
                    for item in injector_end_records
                ),
                "injector_retries": sum(
                    int(item.get("injector_retry_count") or 0)
                    for item in injector_end_records
                ),
                "injector_errors": sum(
                    bool(item.get("error")) for item in injector_end_records
                ),
            }
        )
    return {
        "campaign_id": CAMPAIGN_ID,
        "manifest_sha256": build_manifest()["manifest_sha256"],
        "harbor_return_code": return_code,
        "decision": "pending full trajectory review; never primary-score data",
        "counts": {
            "planned": len(TASKS),
            "results": len(trials),
            "exceptions": sum(item["exception_info"] is not None for item in trials),
        },
        "trials": trials,
    }


def run(approved_sha256: str) -> int:
    api_key = load_key()
    config_path, manifest = freeze(approved_sha256)
    before = common.current_key_usage(api_key)
    write_json(RUN_DIR / "openrouter-usage-before.json", before, mode=0o600)
    with (RUN_DIR / "harbor-console.log").open("w") as log:
        process = subprocess.Popen(
            harbor_command(config_path),
            cwd=ROOT,
            env=tool_env(api_key),
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            return_code = process.wait(timeout=WHOLE_RUN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            return_code = 124
    after = common.current_key_usage(api_key)
    write_json(RUN_DIR / "openrouter-usage-after.json", after, mode=0o600)
    write_json(RUN_DIR / "audit.json", audit(api_key, return_code=return_code))
    write_json(
        RUN_DIR / "run-status.json",
        {
            "campaign_id": CAMPAIGN_ID,
            "manifest_sha256": manifest["manifest_sha256"],
            "harbor_return_code": return_code,
            "completed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )
    return return_code


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight")
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--approved-manifest-sha256", required=True)
    args = parser.parse_args()
    if args.command == "preflight":
        return preflight()
    return run(args.approved_manifest_sha256)


if __name__ == "__main__":
    raise SystemExit(main())
