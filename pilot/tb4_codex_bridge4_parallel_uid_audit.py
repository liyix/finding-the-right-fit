#!/usr/bin/env python3
"""r14: r13 with host-UID recorder persistence and exact bind-mount preflight."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import urllib.request
from typing import Any

import tb4_codex_bridge4_parallel_container_audit as r13


CAMPAIGN_ID = "pilot-tb4-codex-five-model-parallel-20260901-r14"
SCRIPT_PATH = Path(__file__).resolve()
PORTS = {
    "glm-5.3": 4060,
    "kimi-k3": 4061,
    "deepseek-v4-pro": 4062,
    "gpt-6-astra": 4063,
    "claude-opus-5": 4064,
}
AUDIT_PORTS = {name: port + 100 for name, port in PORTS.items()}


r13.CAMPAIGN_ID = CAMPAIGN_ID
r13.SCRIPT_PATH = SCRIPT_PATH
r13.PORTS = PORTS
r13.AUDIT_PORTS = AUDIT_PORTS
r13.r12.CAMPAIGN_ID = CAMPAIGN_ID
r13.r12.SCRIPT_PATH = SCRIPT_PATH
r13.r12.PORTS = PORTS
r13.r12.AUDIT_PORTS = AUDIT_PORTS
r13.r12.prior.CAMPAIGN_ID = CAMPAIGN_ID
r13.r12.prior.SCRIPT_PATH = SCRIPT_PATH
r13.r12.prior.PORTS = PORTS
r13.r12.base.CAMPAIGN_ID = CAMPAIGN_ID
r13.r12.base.RUN_DIR = r13.r12.base.ROOT / "runs" / CAMPAIGN_ID
r13.r12.base.HARBOR_DIR = r13.r12.base.RUN_DIR / "harbor"


_r13_manifest = r13.manifest


def manifest() -> dict[str, Any]:
    data = _r13_manifest()
    data.pop("manifest_sha256", None)
    data["campaign_id"] = CAMPAIGN_ID
    data["compatibility_layer"]["ports"] = PORTS
    data["compatibility_layer"]["audit_ports"] = AUDIT_PORTS
    data["compatibility_layer"]["audit_process"] = (
        "pinned bridge4 Node 22 container running as the host UID/GID, so the "
        "mode-0600 per-call audit log is writable in the bind-mounted run directory"
    )
    data["compatibility_layer"]["audit_shutdown"] = (
        "explicit docker stop by the deterministic recorder container name; no "
        "background recorder remains after a worker"
    )
    data["prior_attempt"] = {
        "campaign_id": "pilot-tb4-codex-five-model-parallel-20260901-r13",
        "outcome": (
            "GPT and Kimi each reached one upstream response header before the Node-22 "
            "recorder failed to write its host bind mount; all exits were then stopped"
        ),
        "replacement_reason": (
            "run the recorder as host UID/GID, require a real header/end record in the "
            "production bind mount during preflight, and explicitly stop its container"
        ),
    }
    data["source_sha256"][
        "pilot/tb4_codex_bridge4_parallel_container_audit.py"
    ] = r13.r12.base.file_sha256(
        r13.r12.base.ROOT
        / "pilot"
        / "tb4_codex_bridge4_parallel_container_audit.py"
    )
    data["source_sha256"][
        "pilot/tb4_codex_bridge4_parallel_uid_audit.py"
    ] = r13.r12.base.file_sha256(SCRIPT_PATH)
    data["post_run_audit"] = (
        "all native trajectories plus one provider generation record per recorder ID; "
        "zero inactive-item errors; explicit confirmation that all recorder containers stopped"
    )
    data["manifest_sha256"] = hashlib.sha256(
        r13.r12.base.canonical(data)
    ).hexdigest()
    return data


r13.manifest = manifest
r13.r12.manifest = manifest
r13.r12.base.manifest = manifest
r13.r12.prior.manifest = manifest


def materialize() -> dict[str, Any]:
    return r13.materialize()


r13.r12.prior.materialize = materialize


def _container_name(name: str) -> str:
    return f"{CAMPAIGN_ID}-audit-{name}".replace(".", "-")


_r13_auditor_command = r13._auditor_command


def _auditor_command(
    name: str,
    *,
    upstream: str,
    config_path: Path,
    telemetry_dir: Path,
    port: int,
) -> list[str]:
    command = _r13_auditor_command(
        name,
        upstream=upstream,
        config_path=config_path,
        telemetry_dir=telemetry_dir,
        port=port,
    )
    user_index = command.index("--name")
    command[user_index:user_index] = ["--user", f"{os.getuid()}:{os.getgid()}"]
    return command


r13._auditor_command = _auditor_command


_r13_start_auditor = r13.start_auditor
_AUDITOR_NAMES: dict[int, str] = {}


def start_auditor(name: str) -> tuple[subprocess.Popen[str], Any, Path]:
    process, log, telemetry_dir = _r13_start_auditor(name)
    _AUDITOR_NAMES[process.pid] = _container_name(name)
    return process, log, telemetry_dir


def _stop_named(process: subprocess.Popen[str], log: Any, name: str) -> None:
    subprocess.run(
        ["docker", "stop", "--time", "5", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.terminate()
        process.wait(timeout=5)
    log.close()


def stop_auditor(process: subprocess.Popen[str], log: Any) -> None:
    name = _AUDITOR_NAMES.pop(process.pid, None)
    if name is None:
        process.terminate()
        process.wait(timeout=5)
        log.close()
        return
    _stop_named(process, log, name)


r13.start_auditor = start_auditor
r13.r12.start_auditor = start_auditor
r13.r12.stop_auditor = stop_auditor


def _stop_preflight_process(process: subprocess.Popen[str], log: Any) -> None:
    _stop_named(process, log, _container_name("preflight"))


r13._stop_process = _stop_preflight_process


class _FakeOpenRouter(BaseHTTPRequestHandler):
    request_body = b""

    def log_message(self, _format: str, *args: Any) -> None:
        del args

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        type(self).request_body = self.rfile.read(length)
        body = json.dumps(
            {
                "id": "gen-bind-mount-preflight",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        ).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.send_header("x-generation-id", "gen-bind-mount-preflight")
        self.send_header("x-request-id", "req-bind-mount-preflight")
        self.end_headers()
        self.wfile.write(body)


def production_bind_mount_check() -> dict[str, Any]:
    name = "glm-5.3"
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOpenRouter)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with tempfile.TemporaryDirectory(prefix="r14-bind-", dir="/tmp") as raw:
        temp = Path(raw)
        config_path = temp / "config.json"
        r13.r12.base.write_json(config_path, r13.r12.audit_config(name))
        process_log = (temp / "process.log").open(
            "w", encoding="utf-8", buffering=1
        )
        port = r13.r12.base.free_tcp_port()
        process = subprocess.Popen(
            _auditor_command(
                "preflight-bind",
                upstream=f"http://127.0.0.1:{server.server_port}",
                config_path=config_path,
                telemetry_dir=temp,
                port=port,
            ),
            cwd=r13.r12.base.ROOT,
            env=os.environ.copy(),
            stdout=process_log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("bind-mount recorder exited before health check")
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/health", timeout=1
                    ) as response:
                        if response.status == 204:
                            break
                except Exception:
                    time.sleep(0.2)
            else:
                raise TimeoutError("bind-mount recorder health check timed out")

            route = r13.r12.base.TARGETS[name]["route"]
            request_body = json.dumps(
                {
                    "model": r13.r12.base.TARGETS[name]["model"],
                    "messages": [{"role": "user", "content": "local preflight"}],
                    "provider": route,
                    "reasoning_effort": "high",
                    "usage": {"include": True},
                    "stream": False,
                },
                separators=(",", ":"),
            ).encode()
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                data=request_body,
                headers={"content-type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                if response.status != 200:
                    raise RuntimeError(f"unexpected local response {response.status}")
                response.read()
        finally:
            _stop_named(process, process_log, _container_name("preflight-bind"))
            server.shutdown()
            server.server_close()

        audit_path = temp / "requests.jsonl"
        if not audit_path.is_file() or audit_path.stat().st_mode & 0o777 != 0o600:
            raise RuntimeError("production bind-mount audit log missing or not mode 0600")
        records = [json.loads(line) for line in audit_path.read_text().splitlines()]
        headers = [item for item in records if item.get("event") == "upstream_headers"]
        endings = [item for item in records if item.get("event") == "request_end"]
        if len(headers) != 1 or len(endings) != 1:
            raise RuntimeError("production bind-mount audit lifecycle is incomplete")
        header = headers[0]
        if (
            header.get("openrouter_generation_id") != "gen-bind-mount-preflight"
            or header.get("provider_validation") != "matched-existing"
            or header.get("changed_fields") != []
            or header.get("incoming_body_sha256")
            != header.get("forwarded_body_sha256")
            or header.get("request_audit", {}).get("reasoning_effort") != "high"
            or header.get("request_audit", {}).get("max_tokens_present") is not False
        ):
            raise RuntimeError("production bind-mount audit record drift")
        return {
            "status": "passed",
            "paid_api_calls": 0,
            "external_api_calls": 0,
            "host_uid_gid": f"{os.getuid()}:{os.getgid()}",
            "log_mode": "0600",
            "header_records": 1,
            "request_end_records": 1,
            "body_unchanged": True,
            "generation_id": "gen-bind-mount-preflight",
        }


_r13_preflight = r13.preflight


def preflight() -> int:
    result = _r13_preflight()
    bind_check = production_bind_mount_check()
    path = r13.r12.base.RUN_DIR / "preflight.json"
    data = json.loads(path.read_text())
    data["production_bind_mount_write"] = bind_check
    r13.r12.base.write_json(path, data)
    print("Production host-UID bind-mount recorder passed; no API request was made")
    return result


r13.preflight = preflight
r13.r12.preflight = preflight
r13.r12.prior.preflight = preflight
r13.r12.prior.worker = r13.r12.worker


if __name__ == "__main__":
    raise SystemExit(r13.r12.prior.main())
