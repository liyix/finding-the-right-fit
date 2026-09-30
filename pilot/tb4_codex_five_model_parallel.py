#!/usr/bin/env python3
"""Parallel one-task Codex qualification for all five study models.

The previous r10 controller ran model jobs sequentially and its native
Responses passthrough exposed provider-specific custom-tool replay failures.
This campaign runs five independent Harbor jobs concurrently and uses the
already pinned LiteLLM Responses-to-Chat compatibility path uniformly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import traceback
from typing import Any

import tb4_codex_openrouter as base


CAMPAIGN_ID = "pilot-tb4-codex-five-model-parallel-20260831-r11"
SCRIPT_PATH = Path(__file__).resolve()
TASK = "bun-sourcemap-leak"
PORTS = {
    "glm-5.3": 4030,
    "kimi-k3": 4031,
    "deepseek-v4-pro": 4032,
    "gpt-6-astra": 4033,
    "claude-opus-5": 4034,
}

base.CAMPAIGN_ID = CAMPAIGN_ID
base.RUN_DIR = base.ROOT / "runs" / CAMPAIGN_ID
base.HARBOR_DIR = base.RUN_DIR / "harbor"
base.TASKS = (TASK,)
base.CONCURRENCY = 1

_base_harbor_config = base.harbor_config
_base_proxy_config = base.proxy_config
_base_manifest = base.manifest


def harbor_config(name: str) -> dict[str, Any]:
    """Resolve each concurrent model job to its own localhost proxy port."""
    old_url = base.PROXY_URL
    try:
        base.PROXY_URL = f"http://host.docker.internal:{PORTS[name]}/v1"
        config = _base_harbor_config(name)
    finally:
        base.PROXY_URL = old_url
    config["agents"][0]["n_concurrent"] = 1
    config["n_concurrent_trials"] = 1
    return config


def proxy_config(name: str) -> dict[str, Any]:
    """Use one disclosed Responses-to-Chat compatibility path in every arm."""
    config = _base_proxy_config(name)
    params = config["model_list"][0]["litellm_params"]
    params["use_chat_completions_api"] = True
    params.pop("max_tokens", None)
    params["extra_body"] = {
        "provider": base.TARGETS[name]["route"],
        "usage": {"include": True},
    }
    return config


base.harbor_config = harbor_config
base.proxy_config = proxy_config


def manifest() -> dict[str, Any]:
    data = _base_manifest()
    data.pop("manifest_sha256", None)
    data["campaign_id"] = CAMPAIGN_ID
    data["kind"] = "paid-real-benchmark-qualification-not-primary-result"
    data["benchmark"].update(
        {
            "planned_trials": 5,
            "oracle_preflight": (
                "the pinned bun-sourcemap-leak verifier has previously completed; "
                "all five arms use the identical task checksum"
            ),
        }
    )
    for name, path in data["model_paths"].items():
        path["protocol"] = (
            "Codex Responses -> LiteLLM Responses-to-Chat -> "
            "OpenRouter Chat Completions"
        )
        path["proxy_port"] = PORTS[name]
    data["controls"].update(
        {
            "reasoning_summary": (
                "Codex model_reasoning_summary=none; r10 exposed summary=auto telemetry "
                "on translated paths, so exact wire and replay behavior must be audited"
            ),
            "max_output_tokens": (
                "unset: Codex sends no public output-cap control and the compatibility "
                "proxy injects none; provider endpoint default/effective stop is audited"
            ),
            "concurrency": (
                "five model jobs concurrently; one TB4 trial and one proxy deployment "
                "per model, total planned agent concurrency 5"
            ),
            "tools": (
                "identical GPT-derived Codex catalog in every arm; LiteLLM uniformly "
                "maps Responses function/custom/namespace tools to Chat functions, "
                "which preserves names/results but cannot preserve freeform grammar enforcement"
            ),
        }
    )
    data["compatibility_layer"]["changes"] = [
        "openrouter-eval alias -> one frozen target model per isolated deployment",
        "inject frozen provider.only, declared quantization where applicable, and allow_fallbacks=false",
        "translate Responses messages/reasoning/function/custom/namespace tools to Chat uniformly",
        "do not inject an output-token cap",
        "request OpenRouter usage in responses for provider-side token/cost audit",
        "drop parallel_tool_calls uniformly",
    ]
    data["compatibility_layer"]["ports"] = PORTS
    data["accounting"].update(
        {
            "primary": (
                "OpenRouter response usage and settled provider activity/generation "
                "records where available"
            ),
            "estimated_cost_usd": (
                "expected below $10 total from prior same-task token counts; planning "
                "envelope $25; no automatic dollar hard stop"
            ),
            "hard_dollar_cap": None,
            "operator_alert_usd": 20.0,
        }
    )
    data["known_uncertainty"] = [
        "Responses-to-Chat is a protocol translation, not stock Codex GPT Responses Lite.",
        "Freeform grammar enforcement is lost when a Codex custom tool becomes a Chat function; the same translation is applied to all five arms.",
        "The strict route policy is auditable, but OpenRouter may not expose an independently queryable generation record for every Responses-to-Chat call.",
        "This one text-only task cannot qualify multimodal, MCP, every namespace/sub-agent path, or compaction recovery.",
        "Actual route, usage/cost, reasoning continuity, retries, output stops, and every tool result require post-run audit before any B decision.",
    ]
    data["prior_attempt"] = {
        "campaign_id": "pilot-tb4-codex-five-model-20260831-r10",
        "outcome": (
            "four models completed this text task but lacked route/cost wire evidence; "
            "GPT had seven provider-rejected apply_patch result replays, DeepSeek was "
            "blocked before inference by the then-current account setting"
        ),
        "replacement_reason": (
            "run all five model jobs concurrently, remove the unregistered 128K Codex "
            "output override, and use the uniformly qualified Chat translation path"
        ),
    }
    data["maximum_claim"] = (
        "Exact Codex 0.150.1 + Harbor 0.22.0 + uniform LiteLLM 1.98.0 "
        "Responses-to-Chat compatibility on one text-only TB4 task for five strictly "
        "routed OpenRouter models; no native-protocol, multimodal, MCP, numerical-"
        "equivalence, or compaction-recovery claim."
    )
    data["owner"] = "codex-operator-agent"
    data["post_run_audit"] = "all five complete native trajectories and provider telemetry"
    data["source_sha256"]["pilot/tb4_codex_five_model_parallel.py"] = base.file_sha256(
        SCRIPT_PATH
    )
    data["manifest_sha256"] = hashlib.sha256(base.canonical(data)).hexdigest()
    return data


base.manifest = manifest


def materialize() -> dict[str, Any]:
    return base.materialize()


def preflight() -> int:
    data = materialize()
    parity_path = base.ROOT / "runs/qualification-codex-controls-20260831/catalog-summary.json"
    parity = json.loads(parity_path.read_text())
    if parity.get("status") != "passed" or parity.get("paid_api_calls") != 0:
        raise RuntimeError("offline Codex catalog parity evidence is missing")
    for name in base.TARGETS:
        saved_harbor = json.loads(
            (base.RUN_DIR / "configs" / f"harbor-{name}.json").read_text()
        )
        saved_proxy = json.loads(
            (base.RUN_DIR / "configs" / f"litellm-{name}.json").read_text()
        )
        if saved_harbor != harbor_config(name) or saved_proxy != proxy_config(name):
            raise RuntimeError(f"materialized config drift: {name}")
        params = saved_proxy["model_list"][0]["litellm_params"]
        if params.get("use_chat_completions_api") is not True or "max_tokens" in params:
            raise RuntimeError(f"incorrect translated/output policy: {name}")

    # Exact pinned bridge image: local fake provider only, never a model API.
    bridge = subprocess.run(
        [
            "docker", "run", "--rm", "--network", "host",
            "--volume", f"{base.ROOT}:/repo:ro",
            base.PROXY_IMAGE,
            "python", "/repo/pilot/codex_bridge_check.py",
        ],
        cwd=base.ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=10 * 60,
        check=False,
    )
    if bridge.returncode != 0 or '"status": "passed"' not in bridge.stdout:
        raise RuntimeError("exact-image Responses-to-Chat preflight failed:\n" + bridge.stdout[-8000:])

    # Harbor import/install lifecycle, still without invoking the agent.
    with tempfile.TemporaryDirectory(prefix="codex-r11-install-", dir="/tmp") as raw:
        temp = Path(raw)
        install = harbor_config("gpt-6-astra")
        install["job_name"] = "qualification-codex-import-r11"
        install["jobs_dir"] = str(temp / "harbor")
        install["install_only"] = True
        config_path = temp / "install-only.json"
        base.write_json(config_path, install)
        completed = subprocess.run(
            base.harbor_command(config_path),
            cwd=base.ROOT,
            env=base.harbor_env(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30 * 60,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError("Harbor install-only preflight failed:\n" + completed.stdout[-8000:])

    base.write_json(
        base.RUN_DIR / "preflight.json",
        {
            "status": "passed",
            "completed_at_utc": base.utc_now(),
            "manifest_sha256": data["manifest_sha256"],
            "paid_api_calls": 0,
            "exact_image_responses_to_chat": "passed",
            "harbor_install_only": "passed",
            "parallel_ports": PORTS,
        },
    )
    print("Zero-cost five-model preflight passed; no model API call was made")
    print(f"Prospective manifest SHA256: {data['manifest_sha256']}")
    return 0


def worker(name: str, approved: str) -> int:
    data = json.loads((base.RUN_DIR / "manifest.json").read_text())
    if approved != data.get("manifest_sha256"):
        raise RuntimeError("approval hash does not match frozen manifest")
    if name not in PORTS:
        raise RuntimeError(f"unknown model worker: {name}")
    output = base.HARBOR_DIR / base.job_name(name)
    if output.exists():
        raise RuntimeError(f"refusing to reuse Harbor output {output}")
    base.PROXY_PORT = PORTS[name]
    base.PROXY_URL = f"http://host.docker.internal:{PORTS[name]}/v1"
    process, proxy_log = base.start_proxy(name)
    state: dict[str, Any] = {
        "model": name,
        "proxy_port": PORTS[name],
        "started_at_utc": base.utc_now(),
        "status": "running",
    }
    state_path = base.RUN_DIR / f"worker-state-{name}.json"
    base.write_json(state_path, state)
    try:
        completed = subprocess.run(
            base.harbor_command(base.RUN_DIR / "configs" / f"harbor-{name}.json"),
            cwd=base.ROOT,
            env=base.harbor_env(),
            timeout=9 * 60 * 60,
            check=False,
        )
        state["return_code"] = completed.returncode
        result_path = output / "result.json"
        if result_path.is_file():
            result = json.loads(result_path.read_text())
            state["n_errored_trials"] = result.get("stats", {}).get("n_errored_trials")
        else:
            state["n_errored_trials"] = None
        return completed.returncode or (1 if state.get("n_errored_trials") else 0)
    finally:
        base.stop_proxy(process, proxy_log)
        state["finished_at_utc"] = base.utc_now()
        state["status"] = "finished"
        base.write_json(state_path, state)


def controller(approved: str) -> int:
    log_path = base.RUN_DIR / "controller.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8", buffering=1) as controller_log:
        os.dup2(controller_log.fileno(), sys.stdout.fileno())
        os.dup2(controller_log.fileno(), sys.stderr.fileno())
        controller_state = {
            "campaign_id": CAMPAIGN_ID,
            "manifest_sha256": approved,
            "pid": os.getpid(),
            "started_at_utc": base.utc_now(),
            "status": "running",
        }
        base.write_json(base.RUN_DIR / "controller-state.json", controller_state)
        workers: dict[str, tuple[subprocess.Popen[str], Any]] = {}
        try:
            for name in base.TARGETS:
                handle = (base.RUN_DIR / f"worker-{name}.log").open(
                    "a", encoding="utf-8", buffering=1
                )
                proc = subprocess.Popen(
                    [
                        sys.executable,
                        str(SCRIPT_PATH),
                        "_worker",
                        "--model",
                        name,
                        "--approved-manifest-sha256",
                        approved,
                    ],
                    cwd=base.ROOT,
                    env=os.environ.copy(),
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                workers[name] = (proc, handle)
            results = {}
            for name, (proc, handle) in workers.items():
                code = proc.wait(timeout=9 * 60 * 60)
                handle.close()
                results[name] = code
            overall = 0 if all(code == 0 for code in results.values()) else 1
            base.write_json(base.RUN_DIR / "run-state.json", {"workers": results})
            return overall
        except BaseException:
            traceback.print_exc()
            raise
        finally:
            for proc, handle in workers.values():
                if proc.poll() is None:
                    proc.terminate()
                if not handle.closed:
                    handle.close()
            controller_state["finished_at_utc"] = base.utc_now()
            controller_state["status"] = "finished"
            base.write_json(base.RUN_DIR / "controller-state.json", controller_state)


def launch(approved: str) -> int:
    data = materialize()
    if approved != data["manifest_sha256"]:
        raise RuntimeError("approval hash does not match frozen manifest")
    preflight_path = base.RUN_DIR / "preflight.json"
    preflight_data = json.loads(preflight_path.read_text()) if preflight_path.is_file() else {}
    if (
        preflight_data.get("status") != "passed"
        or preflight_data.get("paid_api_calls") != 0
        or preflight_data.get("manifest_sha256") != approved
    ):
        raise RuntimeError("matching zero-cost preflight is missing")
    session = CAMPAIGN_ID
    if subprocess.run(
        ["tmux", "has-session", "-t", session],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0:
        raise RuntimeError(f"tmux session already exists: {session}")
    inner = (
        f"set -a; source {shlex.quote(str(base.ROOT / '.env'))}; set +a; exec "
        + shlex.join(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "_controller",
                "--approved-manifest-sha256",
                approved,
            ]
        )
    )
    base.write_json(
        base.RUN_DIR / "launcher.json",
        {
            "campaign_id": CAMPAIGN_ID,
            "manifest_sha256": approved,
            "launched_at_utc": base.utc_now(),
            "tmux_session": session,
            "worker_models": list(base.TARGETS),
            "secrets": "loaded from protected .env; values omitted",
        },
    )
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session, "-c", str(base.ROOT),
         shlex.join(["/bin/bash", "-lc", inner])],
        check=True,
    )
    time.sleep(1)
    print(f"Launched five parallel model workers in tmux session {session}")
    return 0


def status() -> int:
    alive = subprocess.run(
        ["tmux", "has-session", "-t", CAMPAIGN_ID],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0
    result: dict[str, Any] = {"campaign_id": CAMPAIGN_ID, "tmux_alive": alive}
    for name in base.TARGETS:
        path = base.RUN_DIR / f"worker-state-{name}.json"
        result[name] = json.loads(path.read_text()) if path.is_file() else {"status": "not-started"}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("prepare")
    sub.add_parser("preflight")
    launch_parser = sub.add_parser("launch")
    launch_parser.add_argument("--approved-manifest-sha256", required=True)
    controller_parser = sub.add_parser("_controller")
    controller_parser.add_argument("--approved-manifest-sha256", required=True)
    worker_parser = sub.add_parser("_worker")
    worker_parser.add_argument("--model", required=True)
    worker_parser.add_argument("--approved-manifest-sha256", required=True)
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
    if args.command == "_worker":
        return worker(args.model, args.approved_manifest_sha256)
    return status()


if __name__ == "__main__":
    raise SystemExit(main())
