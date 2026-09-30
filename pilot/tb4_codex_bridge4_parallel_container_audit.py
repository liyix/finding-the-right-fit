#!/usr/bin/env python3
"""r13: r12 qualification with the production audit sidecar pinned in Node 22."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
from typing import Any

import tb4_codex_bridge4_parallel as r12


CAMPAIGN_ID = "pilot-tb4-codex-five-model-parallel-20260901-r13"
SCRIPT_PATH = Path(__file__).resolve()
PORTS = {
    "glm-5.3": 4050,
    "kimi-k3": 4051,
    "deepseek-v4-pro": 4052,
    "gpt-6-astra": 4053,
    "claude-opus-5": 4054,
}
AUDIT_PORTS = {name: port + 100 for name, port in PORTS.items()}


# Retarget every shared module used by the inherited controller.
r12.CAMPAIGN_ID = CAMPAIGN_ID
r12.SCRIPT_PATH = SCRIPT_PATH
r12.PORTS = PORTS
r12.AUDIT_PORTS = AUDIT_PORTS
r12.prior.CAMPAIGN_ID = CAMPAIGN_ID
r12.prior.SCRIPT_PATH = SCRIPT_PATH
r12.prior.PORTS = PORTS
r12.base.CAMPAIGN_ID = CAMPAIGN_ID
r12.base.RUN_DIR = r12.base.ROOT / "runs" / CAMPAIGN_ID
r12.base.HARBOR_DIR = r12.base.RUN_DIR / "harbor"


_r12_manifest = r12.manifest


def manifest() -> dict[str, Any]:
    data = _r12_manifest()
    data.pop("manifest_sha256", None)
    data["campaign_id"] = CAMPAIGN_ID
    data["compatibility_layer"]["ports"] = PORTS
    data["compatibility_layer"]["audit_ports"] = AUDIT_PORTS
    data["compatibility_layer"]["audit_process"] = (
        "the same pinned bridge4 image supplies Node 22 for the production "
        "audit-only recorder; no host Node runtime is used"
    )
    data["prior_attempt"] = {
        "campaign_id": "pilot-tb4-codex-five-model-parallel-20260901-r12",
        "outcome": (
            "all five workers exited before Harbor/model invocation because the host "
            "Node runtime could not parse the .mjs recorder; API calls and cost were zero"
        ),
        "replacement_reason": (
            "run the production recorder under Node 22 from the already pinned bridge4 "
            "image and exercise that exact launcher before approval"
        ),
    }
    data["source_sha256"]["pilot/tb4_codex_bridge4_parallel.py"] = r12.base.file_sha256(
        r12.base.ROOT / "pilot" / "tb4_codex_bridge4_parallel.py"
    )
    data["source_sha256"][
        "pilot/tb4_codex_bridge4_parallel_container_audit.py"
    ] = r12.base.file_sha256(SCRIPT_PATH)
    data["post_run_audit"] = (
        "all native trajectories plus one provider generation record per sidecar ID; "
        "verify production recorder container lifecycle and zero inactive-item errors"
    )
    data["manifest_sha256"] = hashlib.sha256(r12.base.canonical(data)).hexdigest()
    return data


r12.manifest = manifest
r12.base.manifest = manifest
r12.prior.manifest = manifest


def materialize() -> dict[str, Any]:
    return r12.materialize()


r12.prior.materialize = materialize


def _auditor_command(
    name: str,
    *,
    upstream: str,
    config_path: Path,
    telemetry_dir: Path,
    port: int,
) -> list[str]:
    container = f"{CAMPAIGN_ID}-audit-{name}".replace(".", "-")
    return [
        "docker",
        "run",
        "--rm",
        "--network",
        "host",
        "--name",
        container,
        "--volume",
        f"{r12.base.ROOT / 'integrations' / 'openrouter_body_injector.mjs'}:/injector.mjs:ro",
        "--volume",
        f"{config_path.resolve()}:/config.json:ro",
        "--volume",
        f"{telemetry_dir.resolve()}:/telemetry",
        r12.PROXY_IMAGE,
        "node",
        "/injector.mjs",
        "--mode",
        "audit",
        "--config",
        "/config.json",
        "--upstream",
        upstream,
        "--log",
        "/telemetry/requests.jsonl",
        "--port",
        str(port),
    ]


def start_auditor(name: str) -> tuple[subprocess.Popen[str], Any, Path]:
    telemetry_dir = r12.base.RUN_DIR / "provider-telemetry" / name
    telemetry_dir.mkdir(parents=True, exist_ok=True)
    process_log = (telemetry_dir / "process.log").open(
        "w", encoding="utf-8", buffering=1
    )
    process = subprocess.Popen(
        _auditor_command(
            name,
            upstream="https://openrouter.ai/api",
            config_path=(
                r12.base.RUN_DIR / "configs" / f"openrouter-audit-{name}.json"
            ),
            telemetry_dir=telemetry_dir,
            port=AUDIT_PORTS[name],
        ),
        cwd=r12.base.ROOT,
        env=os.environ.copy(),
        stdout=process_log,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    r12._wait_auditor(name, process)
    return process, process_log, telemetry_dir


r12.start_auditor = start_auditor


def _stop_process(process: subprocess.Popen[str], log: Any) -> None:
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    log.close()


def production_launcher_check() -> dict[str, Any]:
    """Start the exact Dockerized recorder launcher without making a request."""
    name = "glm-5.3"
    with tempfile.TemporaryDirectory(prefix="r13-auditor-", dir="/tmp") as raw:
        temp = Path(raw)
        config_path = temp / "config.json"
        r12.base.write_json(config_path, r12.audit_config(name))
        log = (temp / "process.log").open("w", encoding="utf-8", buffering=1)
        port = 4250
        process = subprocess.Popen(
            _auditor_command(
                name="preflight",
                upstream="http://127.0.0.1:9",
                config_path=config_path,
                telemetry_dir=temp,
                port=port,
            ),
            cwd=r12.base.ROOT,
            env=os.environ.copy(),
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        old_port = AUDIT_PORTS[name]
        try:
            AUDIT_PORTS[name] = port
            r12._wait_auditor(name, process)
        finally:
            AUDIT_PORTS[name] = old_port
            _stop_process(process, log)
        return {
            "status": "passed",
            "paid_api_calls": 0,
            "runtime": "Node 22 from pinned bridge4 image",
            "request_forwarded": False,
        }


_r12_preflight = r12.preflight


def preflight() -> int:
    result = _r12_preflight()
    launcher = production_launcher_check()
    path = r12.base.RUN_DIR / "preflight.json"
    data = json.loads(path.read_text())
    data["production_auditor_launcher"] = launcher
    r12.base.write_json(path, data)
    print("Production Node-22 recorder launcher passed; no API request was made")
    return result


r12.preflight = preflight
r12.prior.preflight = preflight
r12.prior.worker = r12.worker


if __name__ == "__main__":
    raise SystemExit(r12.prior.main())
