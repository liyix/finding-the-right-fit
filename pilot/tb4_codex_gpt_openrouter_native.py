#!/usr/bin/env python3
"""One-task Codex home-field GPT qualification through OpenRouter Responses.

Codex keeps its bundled GPT-5.6-Sol metadata and Responses-Lite/code-mode
request shape.  The transparent sidecar changes only the model slug and adds
the frozen OpenRouter provider route; it performs no protocol translation,
retry, timeout, response mutation, or output-token injection.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
from typing import Any

import provider_smoke as smoke
import tb4_codex_openrouter as common


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = Path(__file__).resolve()
CAMPAIGN_ID = "pilot-tb4-codex-gpt-openai-pin-native-20260902-r5"
RUN_DIR = ROOT / "runs" / CAMPAIGN_ID
HARBOR_DIR = RUN_DIR / "harbor"
TASK = "html-js-filter"
PORT = 4170
INJECTOR_LISTEN_HOST = "0.0.0.0"
IMAGE = "harness-test-provider-smoke:20260901-bridge4"
IMAGE_ID = "sha256:2f1b6015924c96a2eebbb9d6901f51b11f17009b08b83a1aa1ffa99e439a80f6"
ROUTE = {
    "only": ["openai"],
    "allow_fallbacks": False,
    "require_parameters": False,
}
INCOMING_MODEL = "gpt-6-astra"
UPSTREAM_MODEL = "openai/gpt-6-astra"
EXPECTED_ACTUAL_MODEL = "openai/gpt-6-astra-20260903"
HARBOR_PLATFORMDIRS_VERSION = "4.11.6"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def overlay() -> dict[str, Any]:
    return {"services": {"main": {"extra_hosts": ["host.docker.internal:host-gateway"]}}}


def harbor_config(*, install_only: bool = False, jobs_dir: Path | None = None) -> dict[str, Any]:
    return {
        "job_name": (
            "qualification-codex-gpt-native-install"
            if install_only
            else "tb4--codex--gpt-6-astra--rep-1"
        ),
        "jobs_dir": str(jobs_dir or HARBOR_DIR),
        "n_attempts": 1,
        "install_only": install_only,
        "timeout_multiplier": 1.0,
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
        "agents": [
            {
                "import_path": "integrations.harbor_codex:NativeGPTCodex",
                "model_name": INCOMING_MODEL,
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
                    "OPENAI_BASE_URL": f"http://host.docker.internal:{PORT}/v1",
                },
                "mcp_servers": [],
            }
        ],
        "datasets": [
            {
                "path": str(ROOT / "vendor" / "terminal-bench"),
                "task_names": [TASK],
            }
        ],
        "tasks": [],
        "artifacts": [],
        "extra_instruction_paths": [],
        "extra_instructions": [],
    }


def injector_config() -> dict[str, Any]:
    return {
        "incoming_model": INCOMING_MODEL,
        "model": UPSTREAM_MODEL,
        "provider": ROUTE,
    }


def manifest() -> dict[str, Any]:
    data: dict[str, Any] = {
        "campaign_id": CAMPAIGN_ID,
        "kind": "paid-real-benchmark-qualification-not-primary-result",
        "benchmark": {
            "name": "Terminal-Bench 4.0",
            "tag": "v4.0.0",
            "commit": common.TB4_COMMIT,
            "replicate": 1,
            "planned_trials": 1,
            "tasks": [common.task_metadata(TASK)],
        },
        "harness": {
            "name": "Codex CLI",
            "version": "0.150.1",
            "install": "npm @openai/codex@0.150.1 through Harbor built-in installer",
            "integration": "Harbor v0.22.0 built-in plus install-lock-only NativeGPTCodex subclass",
            "entry": (
                "codex exec --dangerously-bypass-approvals-and-sandbox "
                "--skip-git-repo-check --model gpt-6-astra --json "
                "--enable unified_exec -c model_reasoning_effort=high "
                "-c model_reasoning_summary=none"
            ),
            "catalog": "unmodified bundled gpt-6-astra entry",
        },
        "model_path": {
            "requested_to_codex": INCOMING_MODEL,
            "requested_to_openrouter": UPSTREAM_MODEL,
            "expected_actual_model": EXPECTED_ACTUAL_MODEL,
            "provider": "OpenRouter pinned OpenAI official endpoint",
            "base_url": "https://openrouter.ai/api/v1",
            "protocol": "OpenAI Responses end to end; no Responses-to-Chat translation",
            "classification": "provider-compatible gateway path for the native Codex×GPT pairing",
            "route": ROUTE,
            "quantization": "unset; OpenAI official endpoint",
        },
        "compatibility_layer": {
            "image": IMAGE,
            "image_id": IMAGE_ID,
            "sidecar": "integrations/openrouter_body_injector.mjs in inject mode",
            "changed_request_fields": ["model", "provider"],
            "model_change": f"{INCOMING_MODEL} -> {UPSTREAM_MODEL}",
            "provider_injection": ROUTE,
            "response_changes": [],
            "retries": 0,
            "timeout": None,
            "output_cap": None,
            "protocol_translation": False,
        },
        "controls": {
            "reasoning_effort": "high via Codex model_reasoning_effort; exact wire reasoning.effort=high required",
            "reasoning_context": "bundled Responses Lite default all_turns",
            "reasoning_summary": "none (explicit; also bundled default)",
            "context": {
                "provider_advertised": 1_050_000,
                "codex_bundled_context_window": 272_000,
                "codex_bundled_max_context_window": 872_000,
                "effective_context_window_percent": 95,
                "study_override": None,
                "policy": "preserve pinned Codex home-field defaults; audit actual compaction",
            },
            "output": {
                "provider_advertised_max": 128_000,
                "codex_configured": None,
                "required_wire_field": "max_output_tokens absent",
            },
            "verbosity": "low from bundled GPT metadata",
            "temperature": "unset/provider default",
            "seed": None,
            "max_turns": "unset/Codex default",
            "compaction": "Codex native; every event is a primary outcome",
            "retries": {"Codex_request": 4, "Codex_stream": 5, "sidecar": 0, "Harbor": 0},
            "timeouts": {
                "TB4_agent_seconds": 28_800,
                "TB4_verifier_seconds": 1_800,
                "Harbor_agent_override": None,
                "sidecar": None,
                "Codex_stream": "native",
            },
            "concurrency": 1,
            "tools": "unmodified Codex Responses-Lite/code_mode_only defaults",
            "skills": [],
            "mcp_servers": [],
            "subagents": "Codex bundled/default; no study override",
            "memory": "fresh isolated CODEX_HOME",
            "extra_prompt_or_benchmark_augmentation": False,
        },
        "sandbox": {
            "runner": "Harbor 0.22.0",
            "host_runner_dependency_lock": (
                f"platformdirs=={HARBOR_PLATFORMDIRS_VERSION}; fixes Harbor's otherwise "
                "floating transitive dependency for reproducible offline uvx startup"
            ),
            "provider": "Docker",
            "task": common.task_metadata(TASK),
            "network": "task-default Docker network plus host-gateway only for the model endpoint",
        },
        "accounting": {
            "primary": "OpenRouter generation metadata total_cost per sidecar-captured generation ID",
            "secondary": ["Codex native token events", "Harbor normalized metrics"],
            "double_count_rule": "never sum duplicate views of the same request",
            "estimated_incremental_cost_usd": "$0.50-$2.50 based on the prior GPT html-js-filter pilot; high uncertainty",
            "operator_alert_usd": 3.0,
            "hard_dollar_cap": None,
        },
        "qualification_criteria": [
            "every successful generation resolves to expected_actual_model and provider_name=OpenAI",
            "provider.only=[openai], allow_fallbacks=false, require_parameters=false on every request",
            "first and later wire requests retain Responses Lite additional_tools, high/all_turns reasoning, and no max_output_tokens",
            "all observed tool calls/results/replay close without protocol 400, hidden sidecar retry, or malformed SSE",
            "Harbor reaches the verifier and native plus normalized trajectories are preserved",
        ],
        "maximum_claim": (
            "Exact Codex 0.150.1 home-field GPT Responses-Lite path can traverse OpenRouter "
            "while strictly pinned to OpenAI on one text-only TB4 lifecycle. This does not "
            "qualify multimodal, MCP, compaction recovery, concurrency, or OpenAI-direct numerical equivalence."
        ),
        "post_run_audit": (
            "mandatory full trajectory and raw sidecar/provider audit before changing the canonical readiness claim"
        ),
        "source_sha256": {
            str(SCRIPT_PATH.relative_to(ROOT)): common.file_sha256(SCRIPT_PATH),
            "integrations/harbor_codex.py": common.file_sha256(ROOT / "integrations/harbor_codex.py"),
            "integrations/openrouter_body_injector.mjs": common.file_sha256(
                ROOT / "integrations/openrouter_body_injector.mjs"
            ),
            "pilot/codex_config_parity_check.py": common.file_sha256(
                ROOT / "pilot/codex_config_parity_check.py"
            ),
            "pilot/openrouter_body_injector_audit_check.py": common.file_sha256(
                ROOT / "pilot/openrouter_body_injector_audit_check.py"
            ),
        },
    }
    data["manifest_sha256"] = hashlib.sha256(common.canonical(data)).hexdigest()
    return data


def materialize() -> dict[str, Any]:
    data = manifest()
    manifest_path = RUN_DIR / "manifest.json"
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text())
        if existing.get("manifest_sha256") != data["manifest_sha256"]:
            raise RuntimeError("refusing to mutate an existing frozen campaign")
        return existing
    common.write_json(RUN_DIR / "host-gateway.compose.json", overlay())
    common.write_json(RUN_DIR / "configs/harbor.json", harbor_config())
    common.write_json(RUN_DIR / "configs/openrouter-inject.json", injector_config())
    common.write_json(manifest_path, data)
    return data


def harbor_env() -> dict[str, str]:
    env = common.harbor_env()
    env["HARNESS_CODEX_INSTALL_LOCK"] = str(RUN_DIR / "codex-install.lock")
    return env


def harbor_command(config: Path) -> list[str]:
    """Run the pinned Harbor with its otherwise-floating host dependency locked."""
    return [
        "uvx", "--offline", "--from", f"harbor=={common.HARBOR_VERSION}",
        "--with", f"platformdirs=={HARBOR_PLATFORMDIRS_VERSION}",
        "harbor", "run", "--config", str(config), "--yes",
    ]


def run_offline_checks() -> dict[str, Any]:
    commands = [
        [
            "docker", "run", "--rm", "--network", "none",
            "-v", f"{ROOT / 'pilot'}:/pilot:ro",
            IMAGE, "python", "/pilot/codex_config_parity_check.py", "--mode", "native",
        ],
        [
            "docker", "run", "--rm", "--network", "none",
            "-v", f"{ROOT / 'pilot'}:/pilot:ro",
            "-v", f"{ROOT / 'integrations'}:/integrations:ro",
            IMAGE, "python", "/pilot/openrouter_body_injector_audit_check.py",
            "--injector", "/integrations/openrouter_body_injector.mjs",
        ],
    ]
    outputs = []
    for command in commands:
        completed = subprocess.run(
            command, cwd=ROOT, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=5 * 60, check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError("offline check failed:\n" + completed.stdout[-6000:])
        outputs.append(hashlib.sha256(completed.stdout.encode()).hexdigest())
    return {"status": "passed", "paid_api_calls": 0, "output_sha256": outputs}


def preflight() -> int:
    data = materialize()
    offline = run_offline_checks()
    injector, injector_log, _ = start_injector()
    try:
        reachable = subprocess.run(
            [
                "docker", "run", "--rm", "--network", "bridge",
                "--add-host", "host.docker.internal:host-gateway",
                IMAGE, "python", "-c",
                (
                    "import urllib.request; "
                    f"r=urllib.request.urlopen('http://host.docker.internal:{PORT}/health', timeout=5); "
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
        if reachable.returncode != 0:
            raise RuntimeError(
                "Docker host-gateway injector health check failed:\n"
                + reachable.stdout[-3000:]
            )
    finally:
        stop_injector(injector, injector_log)
    with tempfile.TemporaryDirectory(prefix="codex-gpt-native-install-", dir="/tmp") as raw:
        temp = Path(raw)
        install = harbor_config(install_only=True, jobs_dir=temp / "harbor")
        config = temp / "install.json"
        common.write_json(config, install)
        install_env = harbor_env()
        install_env["OPENROUTER_API_KEY"] = "preflight-local-dummy"
        for attempt in range(2):
            completed = subprocess.run(
                harbor_command(config), cwd=ROOT, env=install_env, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=30 * 60, check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(
                    f"Harbor install-only pass {attempt + 1} failed:\n"
                    + completed.stdout[-6000:]
                )
    evidence = {
        "status": "passed",
        "completed_at_utc": utc_now(),
        "manifest_sha256": data["manifest_sha256"],
        "paid_api_calls": 0,
        "offline_native_request_and_injector": offline,
        "docker_host_gateway_injector_health": "passed",
        "injector_listen_host": INJECTOR_LISTEN_HOST,
        "harbor_install_only": "passed twice with the exact formal command",
        "harbor_platformdirs_version": HARBOR_PLATFORMDIRS_VERSION,
    }
    common.write_json(RUN_DIR / "preflight.json", evidence)
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0


def injector_name() -> str:
    return f"{CAMPAIGN_ID}-injector"


def wait_injector(process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Responses injector exited during startup")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=1) as response:
                if response.status == 204:
                    return
        except Exception:
            time.sleep(0.2)
    raise TimeoutError("Responses injector health check timed out")


def start_injector() -> tuple[subprocess.Popen[str], Any, Path]:
    telemetry = RUN_DIR / "provider-telemetry"
    telemetry.mkdir(parents=True, exist_ok=True)
    telemetry.chmod(0o700)
    process_log = (telemetry / "process.log").open("w", encoding="utf-8", buffering=1)
    command = [
        "docker", "run", "--rm", "--network", "host", "--name", injector_name(),
        "--user", f"{os.getuid()}:{os.getgid()}",
        "-v", f"{ROOT / 'integrations'}:/integrations:ro",
        "-v", f"{RUN_DIR / 'configs/openrouter-inject.json'}:/config.json:ro",
        "-v", f"{telemetry}:/telemetry",
        IMAGE, "node", "/integrations/openrouter_body_injector.mjs",
        "--mode", "inject", "--config", "/config.json",
        "--upstream", "https://openrouter.ai/api",
        "--log", "/telemetry/requests.jsonl", "--port", str(PORT),
        "--listen-host", INJECTOR_LISTEN_HOST,
    ]
    process = subprocess.Popen(
        command, cwd=ROOT, stdout=process_log, stderr=subprocess.STDOUT,
        text=True, start_new_session=True,
    )
    wait_injector(process)
    return process, process_log, telemetry


def stop_injector(process: subprocess.Popen[str], log: Any) -> None:
    subprocess.run(
        ["docker", "stop", "--time", "5", injector_name()],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    log.close()


def collect_provider(telemetry: Path) -> None:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return
    common.write_json(
        telemetry / "generation-records.json",
        smoke.fetch_openrouter_generation_records(telemetry, key),
        0o600,
    )


def monitor_runtime_containers(stop: threading.Event) -> None:
    """Capture exact trial container/image identities before Harbor cleanup."""
    captured: dict[str, dict[str, Any]] = {}
    output = RUN_DIR / "runtime-containers.json"
    while not stop.is_set():
        prefixes: set[str] = set()
        if HARBOR_DIR.exists():
            for job in HARBOR_DIR.iterdir():
                if not job.is_dir():
                    continue
                for trial in job.iterdir():
                    if trial.is_dir() and (trial / "config.json").exists():
                        prefixes.add(f"{trial.name.lower()}__env-")
        if prefixes:
            listed = subprocess.run(
                ["docker", "ps", "--format", "{{.ID}}\t{{.Names}}"],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
            for line in listed.stdout.splitlines():
                container_id, separator, name = line.partition("\t")
                if not separator or not any(name.lower().startswith(prefix) for prefix in prefixes):
                    continue
                if container_id in captured:
                    continue
                inspected = subprocess.run(
                    ["docker", "inspect", container_id],
                    cwd=ROOT,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
                if inspected.returncode != 0:
                    continue
                detail = json.loads(inspected.stdout)[0]
                captured[container_id] = {
                    "container_id": detail.get("Id"),
                    "container_name": detail.get("Name"),
                    "runtime_image_id": detail.get("Image"),
                    "configured_image": (detail.get("Config") or {}).get("Image"),
                    "created": detail.get("Created"),
                    "started_at": (detail.get("State") or {}).get("StartedAt"),
                    "compose_project": ((detail.get("Config") or {}).get("Labels") or {}).get(
                        "com.docker.compose.project"
                    ),
                    "compose_service": ((detail.get("Config") or {}).get("Labels") or {}).get(
                        "com.docker.compose.service"
                    ),
                }
                common.write_json(
                    output,
                    {"source": "live docker inspect", "containers": list(captured.values())},
                )
        stop.wait(1.0)


def run(approved: str) -> int:
    data = materialize()
    if approved != data["manifest_sha256"]:
        raise RuntimeError("approval hash does not match frozen manifest")
    preflight_path = RUN_DIR / "preflight.json"
    if not preflight_path.exists():
        raise RuntimeError("matching zero-cost preflight is required")
    preflight_data = json.loads(preflight_path.read_text())
    if preflight_data.get("manifest_sha256") != approved or preflight_data.get("status") != "passed":
        raise RuntimeError("preflight evidence does not match manifest")
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise RuntimeError("OPENROUTER_API_KEY is not loaded")
    output = HARBOR_DIR / harbor_config()["job_name"]
    if output.exists():
        raise RuntimeError(f"refusing to reuse output {output}")
    injector, injector_log, telemetry = start_injector()
    state = {"status": "running", "started_at_utc": utc_now(), "manifest_sha256": approved}
    common.write_json(RUN_DIR / "run-state.json", state)
    monitor_stop = threading.Event()
    monitor = threading.Thread(
        target=monitor_runtime_containers,
        args=(monitor_stop,),
        name=f"{CAMPAIGN_ID}-runtime-container-audit",
        daemon=True,
    )
    monitor.start()
    completed: subprocess.CompletedProcess[Any] | None = None
    try:
        completed = subprocess.run(
            harbor_command(RUN_DIR / "configs/harbor.json"),
            cwd=ROOT, env=harbor_env(), timeout=9 * 60 * 60, check=False,
        )
        return completed.returncode
    finally:
        monitor_stop.set()
        monitor.join(timeout=15)
        stop_injector(injector, injector_log)
        collect_provider(telemetry)
        state.update(
            {
                "status": "finished",
                "finished_at_utc": utc_now(),
                "return_code": completed.returncode if completed is not None else None,
                "trajectory_audit_required": True,
            }
        )
        common.write_json(RUN_DIR / "run-state.json", state)


def session_name() -> str:
    return CAMPAIGN_ID


def launch(approved: str) -> int:
    data = materialize()
    if approved != data["manifest_sha256"]:
        raise RuntimeError("approval hash does not match frozen manifest")
    if not (RUN_DIR / "preflight.json").exists():
        raise RuntimeError("run preflight first")
    if not (ROOT / ".env").exists():
        raise RuntimeError(".env is missing")
    inner = (
        f"set -a; source {shlex.quote(str(ROOT / '.env'))}; set +a; exec "
        + shlex.join([sys.executable, str(SCRIPT_PATH), "_controller", "--approved", approved])
    )
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session_name(), "-c", str(ROOT),
         shlex.join(["/bin/bash", "-lc", inner])],
        check=True,
    )
    common.write_json(
        RUN_DIR / "launcher.json",
        {"launched_at_utc": utc_now(), "manifest_sha256": approved, "tmux": session_name()},
    )
    print(f"launched tmux session {session_name()}")
    return 0


def controller(approved: str) -> int:
    log_path = RUN_DIR / "controller.log"
    with log_path.open("a", encoding="utf-8", buffering=1) as log:
        os.dup2(log.fileno(), sys.stdout.fileno())
        os.dup2(log.fileno(), sys.stderr.fileno())
        try:
            return run(approved)
        except BaseException:
            traceback.print_exc()
            raise


def status() -> int:
    alive = subprocess.run(
        ["tmux", "has-session", "-t", session_name()],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    ).returncode == 0
    result: dict[str, Any] = {"campaign_id": CAMPAIGN_ID, "tmux_alive": alive}
    for name in ("launcher.json", "run-state.json"):
        path = RUN_DIR / name
        if path.exists():
            result[name] = json.loads(path.read_text())
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("prepare")
    sub.add_parser("preflight")
    sub.add_parser("status")
    for name in ("run", "launch", "_controller"):
        item = sub.add_parser(name)
        item.add_argument("--approved", required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        print(materialize()["manifest_sha256"])
        return 0
    if args.command == "preflight":
        return preflight()
    if args.command == "status":
        return status()
    if args.command == "launch":
        return launch(args.approved)
    if args.command == "_controller":
        return controller(args.approved)
    return run(args.approved)


if __name__ == "__main__":
    raise SystemExit(main())
