#!/usr/bin/env python3
"""Bridge4 Codex five-model qualification with auditable OpenRouter calls.

This is the immutable successor to r11.  It keeps the five model inference
workers parallel, serializes only Harbor's package-install phase, fixes the
reasoning SSE lifecycle, and records every OpenRouter generation header without
changing the LiteLLM-generated request body.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time
import urllib.request
from typing import Any

import provider_smoke as smoke
import tb4_codex_five_model_parallel as prior
import tb4_codex_openrouter as base


CAMPAIGN_ID = "pilot-tb4-codex-five-model-parallel-20260901-r12"
SCRIPT_PATH = Path(__file__).resolve()
TASK = "html-js-filter"
PROXY_IMAGE = "harness-test-provider-smoke:20260901-bridge4"
PROXY_IMAGE_ID = "sha256:2f1b6015924c96a2eebbb9d6901f51b11f17009b08b83a1aa1ffa99e439a80f6"
PORTS = {
    "glm-5.3": 4040,
    "kimi-k3": 4041,
    "deepseek-v4-pro": 4042,
    "gpt-6-astra": 4043,
    "claude-opus-5": 4044,
}
AUDIT_PORTS = {name: port + 100 for name, port in PORTS.items()}


# Retarget the already-audited r11 controller without mutating its source.
prior.CAMPAIGN_ID = CAMPAIGN_ID
prior.SCRIPT_PATH = SCRIPT_PATH
prior.TASK = TASK
prior.PORTS = PORTS
base.CAMPAIGN_ID = CAMPAIGN_ID
base.RUN_DIR = base.ROOT / "runs" / CAMPAIGN_ID
base.HARBOR_DIR = base.RUN_DIR / "harbor"
base.TASKS = (TASK,)
base.CONCURRENCY = 1
base.PROXY_IMAGE = PROXY_IMAGE
base.PROXY_IMAGE_ID = PROXY_IMAGE_ID


_prior_proxy_config = prior.proxy_config
_prior_manifest = prior.manifest
_prior_preflight = prior.preflight
_base_harbor_env = base.harbor_env


def proxy_config(name: str) -> dict[str, Any]:
    config = _prior_proxy_config(name)
    params = config["model_list"][0]["litellm_params"]
    params["api_base"] = f"http://127.0.0.1:{AUDIT_PORTS[name]}/v1"
    return config


prior.proxy_config = proxy_config
base.proxy_config = proxy_config


def audit_config(name: str) -> dict[str, Any]:
    target = base.TARGETS[name]
    return {
        "mode": "audit",
        "model": target["model"],
        "provider": target["route"],
    }


def harbor_env() -> dict[str, str]:
    env = _base_harbor_env()
    env["HARNESS_CODEX_INSTALL_LOCK"] = str(base.RUN_DIR / "codex-install.lock")
    return env


base.harbor_env = harbor_env


def manifest() -> dict[str, Any]:
    data = _prior_manifest()
    data.pop("manifest_sha256", None)
    data["campaign_id"] = CAMPAIGN_ID
    data["benchmark"]["tasks"] = [base.task_metadata(TASK)]
    data["benchmark"]["oracle_preflight"] = (
        "the pinned html-js-filter verifier previously completed in r10; "
        "all five arms use the identical task checksum"
    )
    data["compatibility_layer"].update(
        {
            "image": PROXY_IMAGE,
            "image_id": PROXY_IMAGE_ID,
            "reasoning_stream_fix": (
                "stable reasoning item ID and complete part-added/delta/done lifecycle; "
                "no reasoning-to-text downgrade"
            ),
            "audit_sidecar": (
                "per-arm audit-only pass-through; validates the frozen provider body, "
                "forwards original bytes, adds no retry/timeout, and records response IDs"
            ),
            "audit_ports": AUDIT_PORTS,
        }
    )
    data["controls"]["reasoning_summary"] = (
        "Codex model_reasoning_summary=none; wire request must contain effort=high "
        "and omit summary; Codex turn_context may serialize its local default as auto"
    )
    data["controls"]["concurrency"] = (
        "five Harbor jobs start together; host file lock serializes only agent package "
        "installation, then model inference proceeds concurrently"
    )
    data["accounting"].update(
        {
            "primary": (
                "OpenRouter generation metadata fetched for every sidecar-captured ID; "
                "provider settled total_cost is summed once"
            ),
            "estimated_cost_usd": (
                "high uncertainty: r10 html-js-filter was short for Claude, but r11 "
                "showed an uncapped 137-request overwork path; list-price exposure may "
                "range from a few dollars to roughly $50 for Claude alone"
            ),
            "operator_alert_usd": 20.0,
            "hard_dollar_cap": None,
        }
    )
    data["known_uncertainty"] = [
        "Responses custom/freeform tools become Chat functions, so server-side grammar enforcement is not preserved.",
        "The task does not qualify multimodal, MCP, sub-agent, or compaction-recovery behavior.",
        "Claude r11 showed severe over-testing and zero reported cache tokens; request count, cache, runtime, and settled cost are mandatory audit outcomes.",
        "Stock Codex GPT Responses Lite remains a separate home-field/native baseline.",
    ]
    data["prior_attempt"] = {
        "campaign_id": "pilot-tb4-codex-five-model-parallel-20260831-r11",
        "outcome": (
            "four real task lifecycles and all observed tools closed; common reasoning "
            "stream bug and missing per-call route/cost evidence blocked qualification; "
            "DeepSeek failed before inference during simultaneous apt installs"
        ),
        "replacement_reason": (
            "bridge4 reasoning lifecycle, byte-preserving route/cost recorder, serialized "
            "install phase, and a previously short text-only real task"
        ),
    }
    data["maximum_claim"] = (
        "Exact Codex 0.150.1 + Harbor 0.22.0 + bridge4 uniform Responses-to-Chat "
        "compatibility on one text-only TB4 task for five strictly routed OpenRouter "
        "models; no native-protocol, multimodal, MCP, numerical-equivalence, or "
        "compaction-recovery claim."
    )
    data["post_run_audit"] = (
        "all native trajectories plus one provider generation record for every sidecar ID"
    )
    data["source_sha256"].pop("pilot/tb4_codex_five_model_parallel.py", None)
    data["source_sha256"].update(
        {
            "pilot/tb4_codex_bridge4_parallel.py": base.file_sha256(SCRIPT_PATH),
            "pilot/tb4_codex_five_model_parallel.py": base.file_sha256(
                base.ROOT / "pilot" / "tb4_codex_five_model_parallel.py"
            ),
            "pilot/Containerfile": base.file_sha256(base.ROOT / "pilot" / "Containerfile"),
            "pilot/codex_bridge_check.py": base.file_sha256(
                base.ROOT / "pilot" / "codex_bridge_check.py"
            ),
            "pilot/provider_smoke.py": base.file_sha256(
                base.ROOT / "pilot" / "provider_smoke.py"
            ),
            "integrations/openrouter_body_injector.mjs": base.file_sha256(
                base.ROOT / "integrations" / "openrouter_body_injector.mjs"
            ),
            "pilot/patches/litellm-1.98.0-preserve-responses-reasoning.patch": base.file_sha256(
                base.ROOT
                / "pilot/patches/litellm-1.98.0-preserve-responses-reasoning.patch"
            ),
            "pilot/patches/litellm-1.98.0-kimi-tool-replay.patch": base.file_sha256(
                base.ROOT / "pilot/patches/litellm-1.98.0-kimi-tool-replay.patch"
            ),
            "pilot/patches/litellm-1.98.0-reasoning-stream-lifecycle.patch": base.file_sha256(
                base.ROOT
                / "pilot/patches/litellm-1.98.0-reasoning-stream-lifecycle.patch"
            ),
            "pilot/openrouter_body_injector_audit_check.py": base.file_sha256(
                base.ROOT / "pilot/openrouter_body_injector_audit_check.py"
            ),
        }
    )
    data["audit_configs"] = {name: audit_config(name) for name in base.TARGETS}
    data["manifest_sha256"] = hashlib.sha256(base.canonical(data)).hexdigest()
    return data


base.manifest = manifest
prior.manifest = manifest


def materialize() -> dict[str, Any]:
    data = base.materialize()
    for name in base.TARGETS:
        path = base.RUN_DIR / "configs" / f"openrouter-audit-{name}.json"
        expected = audit_config(name)
        if path.exists():
            if json.loads(path.read_text()) != expected:
                raise RuntimeError(f"refusing audit config drift: {name}")
        else:
            base.write_json(path, expected)
    return data


prior.materialize = materialize


def _wait_auditor(name: str, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"OpenRouter auditor exited during startup: {name}")
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{AUDIT_PORTS[name]}/health", timeout=1
            ) as response:
                if response.status == 204:
                    return
        except Exception:
            time.sleep(0.2)
    raise TimeoutError(f"OpenRouter auditor did not become ready: {name}")


def start_auditor(name: str) -> tuple[subprocess.Popen[str], Any, Path]:
    telemetry_dir = base.RUN_DIR / "provider-telemetry" / name
    telemetry_dir.mkdir(parents=True, exist_ok=True)
    audit_log = telemetry_dir / "requests.jsonl"
    process_log = (telemetry_dir / "process.log").open(
        "w", encoding="utf-8", buffering=1
    )
    process = subprocess.Popen(
        [
            "node",
            str(base.ROOT / "integrations" / "openrouter_body_injector.mjs"),
            "--mode",
            "audit",
            "--config",
            str(base.RUN_DIR / "configs" / f"openrouter-audit-{name}.json"),
            "--upstream",
            "https://openrouter.ai/api",
            "--log",
            str(audit_log),
            "--port",
            str(AUDIT_PORTS[name]),
        ],
        cwd=base.ROOT,
        env=os.environ.copy(),
        stdout=process_log,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    _wait_auditor(name, process)
    return process, process_log, telemetry_dir


def stop_auditor(process: subprocess.Popen[str], log: Any) -> None:
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    log.close()


def collect_provider_records(telemetry_dir: Path) -> None:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        base.write_json(
            telemetry_dir / "generation-records.json",
            {"source": "openrouter-generation-api", "records": [], "errors": [{"error": "key-not-loaded"}]},
            0o600,
        )
        return
    records = smoke.fetch_openrouter_generation_records(telemetry_dir, key)
    base.write_json(telemetry_dir / "generation-records.json", records, 0o600)


def worker(name: str, approved: str) -> int:
    data = json.loads((base.RUN_DIR / "manifest.json").read_text())
    if approved != data.get("manifest_sha256"):
        raise RuntimeError("approval hash does not match frozen manifest")
    output = base.HARBOR_DIR / base.job_name(name)
    if output.exists():
        raise RuntimeError(f"refusing to reuse Harbor output {output}")
    base.PROXY_PORT = PORTS[name]
    base.PROXY_URL = f"http://host.docker.internal:{PORTS[name]}/v1"
    auditor, auditor_log, telemetry_dir = start_auditor(name)
    proxy: subprocess.Popen[str] | None = None
    proxy_log: Any = None
    state: dict[str, Any] = {
        "model": name,
        "proxy_port": PORTS[name],
        "audit_port": AUDIT_PORTS[name],
        "started_at_utc": base.utc_now(),
        "status": "running",
    }
    state_path = base.RUN_DIR / f"worker-state-{name}.json"
    base.write_json(state_path, state)
    try:
        proxy, proxy_log = base.start_proxy(name)
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
        if proxy is not None and proxy_log is not None:
            base.stop_proxy(proxy, proxy_log)
        stop_auditor(auditor, auditor_log)
        collect_provider_records(telemetry_dir)
        state["finished_at_utc"] = base.utc_now()
        state["status"] = "finished"
        base.write_json(state_path, state)


prior.worker = worker


def preflight() -> int:
    result = _prior_preflight()
    completed = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "-v",
            f"{base.ROOT / 'integrations'}:/integrations:ro",
            "-v",
            f"{base.ROOT / 'pilot'}:/pilot:ro",
            PROXY_IMAGE,
            "python3",
            "/pilot/openrouter_body_injector_audit_check.py",
            "--injector",
            "/integrations/openrouter_body_injector.mjs",
        ],
        cwd=base.ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=5 * 60,
        check=False,
    )
    if completed.returncode != 0 or '"status": "passed"' not in completed.stdout:
        raise RuntimeError("audit-only recorder preflight failed:\n" + completed.stdout[-8000:])
    path = base.RUN_DIR / "preflight.json"
    data = json.loads(path.read_text())
    data.update(
        {
            "audit_only_recorder": "passed",
            "bridge4_audit_sidecar_fake_upstream_loop": "passed",
            "serialized_install_lock": str(base.RUN_DIR / "codex-install.lock"),
            "bridge4_image_id": PROXY_IMAGE_ID,
        }
    )
    base.write_json(path, data)
    print("Audit-only recorder preflight passed; no model API call was made")
    return result


prior.preflight = preflight


if __name__ == "__main__":
    raise SystemExit(prior.main())
