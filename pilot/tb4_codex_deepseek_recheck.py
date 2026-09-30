#!/usr/bin/env python3
"""One-task Codex × DeepSeek first-party OpenRouter requalification.

This is a thin campaign specialization of ``tb4_codex_openrouter.py``.  It
keeps the exact r10 Codex/Harbor/proxy implementation while selecting only the
DeepSeek arm and one text-only TB4 task after the account privacy change.
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
import time
from typing import Any

import tb4_codex_openrouter as base


CAMPAIGN_ID = "pilot-tb4-codex-deepseek-recheck-20260831-r1"
SCRIPT_PATH = Path(__file__).resolve()

base.CAMPAIGN_ID = CAMPAIGN_ID
base.RUN_DIR = base.ROOT / "runs" / CAMPAIGN_ID
base.HARBOR_DIR = base.RUN_DIR / "harbor"
base.TASKS = ("bun-sourcemap-leak",)
base.TARGETS = {
    "deepseek-v4-pro": {
        "model": "deepseek/deepseek-v4-pro-0813",
        "route": {
            "only": ["deepseek"],
            "allow_fallbacks": False,
            "require_parameters": False,
        },
    }
}
base.CONCURRENCY = 1
base.PROXY_PORT = 4023
base.PROXY_URL = f"http://host.docker.internal:{base.PROXY_PORT}/v1"

_base_manifest = base.manifest


def manifest() -> dict[str, Any]:
    data = _base_manifest()
    data.pop("manifest_sha256", None)
    data.update(
        {
            "campaign_id": CAMPAIGN_ID,
            "kind": "paid-real-benchmark-qualification-not-primary-result",
            "maximum_claim": (
                "Exact Codex 0.150.1 + Harbor 0.22.0 + OpenRouter Responses + "
                "DeepSeek first-party qualification on one text-only TB4 task. "
                "No claim for multimodal, compaction recovery, MCP, or numerical "
                "equivalence with the official API."
            ),
            "owner": "codex-operator-agent",
        }
    )
    data["benchmark"].update(
        {
            "planned_trials": 1,
            "oracle_preflight": "bun-sourcemap-leak verifier previously completed in the pinned TB4 checkout",
        }
    )
    data["controls"]["concurrency"] = "one DeepSeek trial"
    data["accounting"].update(
        {
            "estimated_cost_usd": "expected below $3 for one text-only task; no dollar hard stop",
            "hard_dollar_cap": None,
            "operator_alert_usd": 3.0,
        }
    )
    data["known_uncertainty"] = [
        "DeepSeek first-party effective quantization is not exposed and is not guessed.",
        "The task exercises Codex default terminal/file-edit paths but cannot prove every custom/freeform, namespace, MCP, web, image, or sub-agent shape.",
        "A 1M context and 950K compaction threshold are configured; this one task may not trigger compaction, so recovery remains a separate qualification.",
        "Actual provider/model, retries, reasoning continuity, output caps, token/cost disagreements, and verifier validity require post-run trajectory audit.",
    ]
    data["compatibility_layer"]["changes"] = [
        "openrouter-eval alias -> frozen DeepSeek target model",
        "inject provider.only=['deepseek'] and allow_fallbacks=false; no quantization is asserted",
        "inject max output 128000",
        "translate Responses custom/namespace tool shapes using the shared Codex compatibility path",
        "drop parallel_tool_calls using the shared Codex compatibility path",
    ]
    data["prior_attempt"] = {
        "campaign_id": "pilot-tb4-codex-five-model-20260831-r10",
        "outcome": (
            "all four DeepSeek trials failed before inference under the old OpenRouter "
            "account privacy setting; their raw failures and zero-cost evidence remain immutable"
        ),
        "replacement_reason": (
            "the account setting changed and provider-layer Responses plus independent "
            "PI/Claude Code/DSH evidence now confirm the first-party endpoint is reachable"
        ),
    }
    data["post_run_audit"] = "the complete single trajectory before accepting B-level evidence"
    data["source_sha256"]["pilot/tb4_codex_deepseek_recheck.py"] = base.file_sha256(
        SCRIPT_PATH
    )
    data["manifest_sha256"] = hashlib.sha256(base.canonical(data)).hexdigest()
    return data


base.manifest = manifest


def launch(approved: str) -> int:
    data = base.materialize()
    if approved != data["manifest_sha256"]:
        raise RuntimeError("approval hash does not match the frozen manifest")
    preflight_path = base.RUN_DIR / "preflight.json"
    if not preflight_path.is_file():
        raise RuntimeError("zero-cost preflight evidence is missing")
    preflight = json.loads(preflight_path.read_text())
    if (
        preflight.get("status") != "passed"
        or preflight.get("manifest_sha256") != approved
        or preflight.get("paid_api_calls") != 0
    ):
        raise RuntimeError("zero-cost preflight evidence does not match this manifest")
    if not os.environ.get("OPENROUTER_API_KEY") and not (base.ROOT / ".env").is_file():
        raise RuntimeError("OPENROUTER_API_KEY is not loaded and .env is unavailable")
    session = base.tmux_session_name()
    exists = subprocess.run(
        ["tmux", "has-session", "-t", session],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if exists.returncode == 0:
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
    command = shlex.join(["/bin/bash", "-lc", inner])
    base.write_json(
        base.RUN_DIR / "launcher.json",
        {
            "campaign_id": CAMPAIGN_ID,
            "manifest_sha256": approved,
            "launched_at_utc": base.utc_now(),
            "tmux_session": session,
            "controller_command": "environment loaded from protected .env; secrets omitted",
        },
    )
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session, "-c", str(base.ROOT), command],
        check=True,
    )
    time.sleep(1)
    print(f"Launched persistent controller in tmux session {session}")
    print(f"Status: {sys.executable} {SCRIPT_PATH} status")
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
    sub.add_parser("status")
    args = parser.parse_args()
    if args.command == "prepare":
        print(base.materialize()["manifest_sha256"])
        return 0
    if args.command == "preflight":
        return base.preflight()
    if args.command == "launch":
        return launch(args.approved_manifest_sha256)
    if args.command == "_controller":
        return base.controller(args.approved_manifest_sha256)
    return base.status()


if __name__ == "__main__":
    raise SystemExit(main())
