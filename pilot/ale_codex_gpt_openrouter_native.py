#!/usr/bin/env python3
"""ALE lifecycle/tool qualification for stock Codex × GPT through OpenRouter.

The first campaign deliberately uses ALE's data-free ``demo/tool_smoke`` task:
it tests the exact sandbox deployer, Codex native tools, the CUA MCP bridge,
provider pin, Responses streaming, replay, telemetry, and verifier lifecycle.
It does not promote the path to the registered 105-task benchmark slice.
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
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiment import load_config  # noqa: E402
from integrations.ale import _tree_sha256 as overlay_tree_sha256  # noqa: E402
from integrations.ale import prepare_agent_source  # noqa: E402

SCRIPT_PATH = Path(__file__).resolve()
VENDOR = ROOT / "vendor" / "agents-last-exam"
ALE_COMMIT = "0b6465b13c85b5a0a017d4c88bcf979a519a5e1f"
CAMPAIGN_ID = "pilot-ale-codex-gpt-openai-pin-tool-smoke-20260901-r6"
RUN_DIR = ROOT / "runs" / CAMPAIGN_ID
SOURCE_DIR = RUN_DIR / "ale-source"
CONFIG_DIR = RUN_DIR / "configs"
OUTPUT_DIR = RUN_DIR / "ale-output"
TASK = "demo/tool_smoke"
DOCKER_IMAGE = "agentslastexam/ale-ubuntu22-docker:latest"
UPSTREAM_MODEL = "openai/gpt-6-astra"
EXPECTED_ACTUAL_MODEL = "openai/gpt-6-astra-20260903"
ROUTE = {"only": ["openai"], "allow_fallbacks": False, "require_parameters": False}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        digest.update(item.relative_to(path).as_posix().encode())
        digest.update(b"\0")
        digest.update(item.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def write_json(path: Path, value: Any, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(mode)


def write_yaml(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")


def source_payload() -> dict[str, str]:
    return {
        "upstream_commit": ALE_COMMIT,
        "upstream_status": subprocess.check_output(
            ["git", "status", "--short"], cwd=VENDOR, text=True
        ).strip(),
        "adapter_config_sha256": sha256_file(
            ROOT / "integrations/ale_agents/study_codex/config.py"
        ),
        "adapter_deployer_sha256": sha256_file(
            ROOT / "integrations/ale_agents/study_codex/deployer.py"
        ),
        "adapter_injector_sha256": sha256_file(
            ROOT / "integrations/ale_agents/study_codex/injector.py"
        ),
        "launcher_sha256": sha256_file(SCRIPT_PATH),
    }


def materialize_source() -> None:
    marker = SOURCE_DIR / "derived-source-manifest.json"
    if marker.exists():
        existing = json.loads(marker.read_text(encoding="utf-8"))
        overlay = existing.get("overlays", {}).get("codex", {})
        expected_overlay = overlay_tree_sha256(
            ROOT / "integrations/ale_agents/study_codex"
        )
        if existing.get("harnesses") != ["codex"] or overlay.get("source_sha256") != expected_overlay:
            raise RuntimeError("refusing to mutate the frozen derived ALE source")
        return
    if SOURCE_DIR.exists():
        raise RuntimeError(f"partial derived source already exists: {SOURCE_DIR}")
    prepare_agent_source(load_config(ROOT / "experiment.yaml"), SOURCE_DIR, ["codex"])


def resolved_configs() -> dict[str, dict[str, Any]]:
    agent = {
        "class": "ale_run.agents.study_codex.deployer.StudyCodexDeployer",
        "id": "codex--gpt-6-astra--openai-pin",
        "executor": "sandbox",
        "model": "gpt-6-astra",
        "config": {
            "provider": "openrouter",
            "base_url": "http://127.0.0.1:4170/v1",
            "reasoning_effort": "high",
            "codex_version": "0.150.1",
            "patched_binary_url": "",
            "patched_binary_url_windows": "",
            "fork_version": "0.150.1",
            "model_catalog_path": "",
            "model_catalog_content": "",
            "feature_overrides": {},
            "otel_enabled": True,
            "injector_port": 4170,
            "injector_upstream": "https://openrouter.ai/api",
            "upstream_model": UPSTREAM_MODEL,
            "provider_only": ["openai"],
            "allow_fallbacks": False,
            "require_parameters": False,
        },
    }
    environment = {
        "snapshots": {
            "cpu-free-ubuntu": {
                "provider": "docker",
                "image": "ale-ubuntu22-docker",
                "docker": {"shm_size": "2g", "resolution": [1024, 768]},
            }
        },
        "task_data_source": "baked_in_sandbox",
        "output_path": "local",
    }
    tasks = [{"path": TASK, "variants": [0]}]
    experiment = {
        "name": "ale--codex--gpt-6-astra--tool-smoke--rep-1",
        "agents": [str(CONFIG_DIR / "agent.yaml")],
        "environment": str(CONFIG_DIR / "environment.yaml"),
        "tasks": str(CONFIG_DIR / "tasks.yaml"),
        "output": {"root": str(OUTPUT_DIR)},
        "concurrency": 1,
        "wall_time_s": 1800,
        "auto_resume": False,
        "max_attempts": 1,
        "cleanup_mode": "delete",
        "prompt_suffix": "",
    }
    return {"agent": agent, "environment": environment, "tasks": tasks, "experiment": experiment}


def manifest_body(configs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    task_dir = VENDOR / "tasks" / TASK
    return {
        "campaign_id": CAMPAIGN_ID,
        "kind": "paid-ALE-lifecycle-tool-qualification-not-primary-result",
        "benchmark": {
            "name": "Agents' Last Exam / ALE runner",
            "commit": ALE_COMMIT,
            "task": TASK,
            "registered_105_task_slice": False,
            "reason": "data-free upstream tool-smoke first qualifies the exact stock-Codex CUA/MCP lifecycle",
            "task_main_sha256": sha256_file(task_dir / "main.py"),
            "task_card_sha256": sha256_file(task_dir / "task_card.json"),
            "replicate": 1,
            "planned_trials": 1,
        },
        "runner": {"name": "ALE official", "commit": ALE_COMMIT},
        "harness": {
            "name": "Codex CLI",
            "version": "0.150.1",
            "install": "npm install -g --force @openai/codex@0.150.1 in the ALE sandbox",
            "integration": (
                "custom stock-Codex deployer subclass retaining ALE's upstream sandbox executor, "
                "CUA MCP bridge, OTEL collector, parser, artifact gather, and verifier lifecycle"
            ),
            "entry": (
                "codex exec --model gpt-6-astra --json "
                "--dangerously-bypass-approvals-and-sandbox"
            ),
            "catalog": "unmodified bundled gpt-6-astra entry",
        },
        "model_path": {
            "requested_to_codex": "gpt-6-astra",
            "requested_to_openrouter": UPSTREAM_MODEL,
            "expected_actual_model": EXPECTED_ACTUAL_MODEL,
            "provider": "OpenRouter pinned OpenAI official endpoint",
            "base_url": "https://openrouter.ai/api/v1",
            "protocol": "Responses end to end; no protocol translation",
            "classification": "provider-compatible gateway for native Codex×GPT",
            "route": ROUTE,
            "quantization": "unset; OpenAI official endpoint",
        },
        "compatibility_layer": {
            "location": "inside each ALE sandbox",
            "changed_request_fields": ["model", "provider"],
            "response_changes": [],
            "retries": 0,
            "timeout": None,
            "output_cap": None,
            "protocol_translation": False,
        },
        "controls": {
            "reasoning_effort": "high via model_reasoning_effort; exact wire value required",
            "reasoning_context": "Codex Responses Lite default all_turns",
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
            "retries": {"Codex_request": 4, "Codex_stream": 5, "injector": 0, "ALE": 0},
            "timeouts_seconds": {
                "ALE_task_wall": 1800,
                "harness_stream": "Codex native",
                "tool": "ALE/CUA native",
                "verifier": "task native",
            },
            "concurrency": 1,
            "tools": "stock Codex native tools plus unmodified ALE CUA MCP server",
            "skills": "Codex defaults; no project skills or study skill injection",
            "mcp_servers": ["ALE upstream cua stdio bridge"],
            "subagents": "Codex bundled/default; no override",
            "memory": "fresh sandbox home/work directory",
            "extra_prompt_or_benchmark_augmentation": False,
        },
        "sandbox": {
            "provider": "ALE local Docker",
            "image": DOCKER_IMAGE,
            "snapshot": "cpu-free-ubuntu",
            "vcpus": 4,
            "memory_gb": 16,
            "disk_gb": 200,
            "network": "ALE image default; injector egresses only as part of normal model access",
        },
        "accounting": {
            "primary": "OpenRouter generation metadata for injector-captured generation IDs",
            "secondary": ["Codex turn usage", "ALE normalized trajectory/OTEL"],
            "double_count_rule": "never sum duplicate views of one request",
            "estimated_incremental_cost_usd": "$0.50-$3.00; high uncertainty",
            "operator_alert_usd": 3.0,
            "hard_dollar_cap": None,
        },
        "qualification_criteria": [
            "ALE completes provision, setup, stock Codex install, launch, artifact gather, and verifier",
            "Codex sees and successfully exercises native and CUA MCP tools",
            "every generation is pinned to OpenAI and the expected actual model",
            "provider route and reasoning=high appear on every request; max_output_tokens stays absent",
            "tool results and reasoning replay close without protocol 400 or hidden injector retry",
        ],
        "maximum_claim": (
            "Stock Codex 0.150.1 × GPT-5.6-Sol can complete ALE's exact Linux sandbox/CUA/MCP "
            "lifecycle on the data-free tool-smoke task. A registered 105-task-slice task and all "
            "non-Docker-compatible tasks remain separately unqualified."
        ),
        "post_run_audit": "mandatory full native/ALE/injector/provider trajectory audit",
        "derived_source": {
            "path": str(SOURCE_DIR.relative_to(ROOT)),
            "tree_sha256": tree_sha256(SOURCE_DIR),
            **source_payload(),
        },
        "config_sha256": {
            key: hashlib.sha256(canonical(value)).hexdigest() for key, value in configs.items()
        },
    }


def materialize() -> dict[str, Any]:
    materialize_source()
    configs = resolved_configs()
    manifest_path = RUN_DIR / "manifest.json"
    body = manifest_body(configs)
    manifest = {**body, "manifest_sha256": hashlib.sha256(canonical(body)).hexdigest()}
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        if existing.get("manifest_sha256") != manifest["manifest_sha256"]:
            raise RuntimeError("refusing to mutate an existing frozen campaign")
        return existing
    write_yaml(CONFIG_DIR / "agent.yaml", configs["agent"])
    write_yaml(CONFIG_DIR / "environment.yaml", configs["environment"])
    write_yaml(CONFIG_DIR / "tasks.yaml", configs["tasks"])
    write_yaml(CONFIG_DIR / "experiment.yaml", configs["experiment"])
    write_json(manifest_path, manifest)
    return manifest


def ale_command(*extra: str) -> list[str]:
    return [
        "uv", "run", "--no-project", "--isolated",
        "--with", "openenv-core==0.3.0",
        "--with", "cua-bench==0.2.7",
        "--with", "pydantic>=2.0",
        "--with", "httpx>=0.27",
        "--with", "anyio>=4",
        "--with", "requests>=2.28",
        "--with", "huggingface-hub>=0.28,<2",
        "--with", "pyyaml>=6",
        "python", "-m", "ale_run", "run", str(CONFIG_DIR / "experiment.yaml"),
        *extra,
    ]


def ale_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SOURCE_DIR)
    return env


def injector_loopback_check() -> dict[str, Any]:
    received: list[dict[str, Any]] = []

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            body = self.rfile.read(int(self.headers.get("content-length", "0")))
            received.append(json.loads(body))
            payload = b'{"ok":true}'
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.send_header("x-generation-id", "loopback-generation")
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with tempfile.TemporaryDirectory(prefix="ale-codex-injector-", dir="/tmp") as raw:
        tmp = Path(raw)
        write_json(tmp / "route.json", {"model": UPSTREAM_MODEL, "provider": ROUTE})
        port = server.server_address[1] + 1
        process = subprocess.Popen(
            [
                sys.executable, str(
                    ROOT / "integrations/ale_agents/study_codex/injector.py"
                ),
                "--config", str(tmp / "route.json"), "--log", str(tmp / "audit.jsonl"),
                "--port", str(port), "--upstream", f"http://127.0.0.1:{server.server_address[1]}",
            ], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:
                        if response.status == 204:
                            break
                except Exception:
                    time.sleep(0.1)
            else:
                raise RuntimeError("loopback injector did not become healthy")
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/responses",
                data=json.dumps({
                    "model": "gpt-6-astra", "reasoning": {"effort": "high"},
                    "input": [{"type": "message"}],
                    "tools": [{"type": "function", "name": "shell"}],
                }).encode(),
                headers={"content-type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                if response.status != 200:
                    raise RuntimeError(f"loopback response status {response.status}")
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            server.shutdown()
            server.server_close()
        if len(received) != 1:
            raise RuntimeError(f"expected one upstream request, got {len(received)}")
        observed = received[0]
        if observed.get("model") != UPSTREAM_MODEL or observed.get("provider") != ROUTE:
            raise RuntimeError("injector did not apply the exact frozen model/provider route")
        if observed.get("reasoning") != {"effort": "high"} or "max_output_tokens" in observed:
            raise RuntimeError("injector changed a fairness-relevant request control")
        return {"status": "passed", "external_api_calls": 0}


def preflight() -> int:
    manifest = materialize()
    loopback = injector_loopback_check()
    completed = subprocess.run(
        ale_command("--dry-run", "--disable-resume"), cwd=VENDOR,
        env=ale_env(), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        timeout=10 * 60, check=False,
    )
    if completed.returncode != 0 or "units (1):" not in completed.stdout:
        raise RuntimeError("ALE structural dry-run failed:\n" + completed.stdout[-6000:])
    image = subprocess.run(
        ["docker", "image", "inspect", DOCKER_IMAGE],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    status = "passed" if image.returncode == 0 else "blocked_missing_image"
    evidence = {
        "status": status,
        "completed_at_utc": utc_now(),
        "manifest_sha256": manifest["manifest_sha256"],
        "paid_api_calls": 0,
        "derived_source_import_and_one_unit_dry_run": "passed",
        "injector_loopback": loopback,
        "docker_image": DOCKER_IMAGE,
        "docker_image_present": image.returncode == 0,
        "next_action": (
            "paid pilot may be approved"
            if image.returncode == 0 else
            "obtain the upstream ~42GB-compressed ALE Docker image before paid launch"
        ),
    }
    write_json(RUN_DIR / "preflight.json", evidence)
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0 if status == "passed" else 3


def run(approved: str) -> int:
    manifest = materialize()
    if approved != manifest["manifest_sha256"]:
        raise RuntimeError("approval hash does not match the frozen manifest")
    preflight_path = RUN_DIR / "preflight.json"
    if not preflight_path.exists():
        raise RuntimeError("matching zero-cost preflight is required")
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    if preflight.get("status") != "passed" or preflight.get("manifest_sha256") != approved:
        raise RuntimeError("preflight has not passed for this exact manifest")
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise RuntimeError("OPENROUTER_API_KEY is not loaded")
    if OUTPUT_DIR.exists():
        raise RuntimeError(f"refusing to reuse output {OUTPUT_DIR}")
    state = {"status": "running", "started_at_utc": utc_now(), "manifest_sha256": approved}
    write_json(RUN_DIR / "run-state.json", state)
    completed: subprocess.CompletedProcess[Any] | None = None
    try:
        completed = subprocess.run(
            ale_command("--disable-resume"), cwd=VENDOR, env=ale_env(),
            timeout=2 * 60 * 60, check=False,
        )
        return completed.returncode
    finally:
        state.update({
            "status": "finished", "finished_at_utc": utc_now(),
            "return_code": completed.returncode if completed is not None else None,
            "trajectory_audit_required": True,
        })
        write_json(RUN_DIR / "run-state.json", state)


def session_name() -> str:
    return CAMPAIGN_ID


def launch(approved: str) -> int:
    manifest = materialize()
    if approved != manifest["manifest_sha256"]:
        raise RuntimeError("approval hash does not match the frozen manifest")
    if not (ROOT / ".env").exists():
        raise RuntimeError(".env is missing")
    inner = (
        f"set -a; source {shlex.quote(str(ROOT / '.env'))}; set +a; exec "
        + shlex.join([sys.executable, str(SCRIPT_PATH), "_controller", "--approved", approved])
    )
    subprocess.run([
        "tmux", "new-session", "-d", "-s", session_name(), "-c", str(ROOT),
        shlex.join(["/bin/bash", "-lc", inner]),
    ], check=True)
    write_json(RUN_DIR / "launcher.json", {
        "launched_at_utc": utc_now(), "manifest_sha256": approved, "tmux": session_name(),
    })
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
    for name in ("preflight.json", "launcher.json", "run-state.json"):
        path = RUN_DIR / name
        if path.exists():
            result[name] = json.loads(path.read_text(encoding="utf-8"))
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
