#!/usr/bin/env python3
"""Rerun the TUA OpenJiuwen canary with the task container as workspace."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import tua_openjiuwen as base


ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN_ID = "pilot-tua-openjiuwen-deepseek-20260915-r2"
QUALIFICATION_ID = "qualification-tua-openjiuwen-root-install-20260915-r1"
RUN_DIR = ROOT / "runs" / CAMPAIGN_ID
QUALIFICATION_DIR = ROOT / "runs" / QUALIFICATION_ID
AGENT_IMPORT = "integrations.harbor_openjiuwen_tua:TUAOpenJiuwenCodingAgent"

_base_agent_config = base.agent_config
_base_manifest_body = base.manifest_body


def agent_config() -> dict[str, Any]:
    config = _base_agent_config()
    config["import_path"] = AGENT_IMPORT
    return config


def source_hashes() -> dict[str, str]:
    paths = [
        Path(__file__).resolve(),
        ROOT / "pilot" / "tua_openjiuwen.py",
        ROOT / "integrations" / "harbor_openjiuwen_tua.py",
        ROOT / "integrations" / "harbor_openjiuwen.py",
        ROOT / "integrations" / "openjiuwen_agent.py",
        ROOT / "integrations" / "openjiuwen-runtime.lock",
        base.TASKS_ROOT / base.TASK / "task.toml",
        base.TASKS_ROOT / base.TASK / "instruction.md",
    ]
    return {str(path.relative_to(ROOT)): base.sha256_file(path) for path in paths}


def manifest_body() -> dict[str, Any]:
    value = _base_manifest_body()
    value["runner"]["integration"] = "custom thin TUA full-container workspace adapter"
    value["sandbox"]["workspace"] = "/"
    value["source_sha256"] = source_hashes()
    value["required_post_run_audit"].append(
        "no false path denials for benchmark-required /home or /etc access"
    )
    value["claim_limit"] = (
        "one text TUA lifecycle only; specifically requalifies the full-container "
        "workspace boundary; no full-benchmark quality, formal concurrency, "
        "multimodality, or compaction claim"
    )
    return value


def preflight() -> dict[str, Any]:
    base.check_tua_checkout()
    base.check_runtime()
    config_path = QUALIFICATION_DIR / "config.json"
    expected_config = base.harbor_config(
        install_only=True, jobs_dir=QUALIFICATION_DIR / "raw"
    )
    if config_path.is_file():
        existing = json.loads(config_path.read_text(encoding="utf-8"))
        if base.canonical(existing) != base.canonical(expected_config):
            raise RuntimeError(
                f"refusing changed qualification directory: {QUALIFICATION_DIR}"
            )
    elif QUALIFICATION_DIR.exists():
        raise RuntimeError(f"refusing partial qualification directory: {QUALIFICATION_DIR}")
    else:
        base.write_json(config_path, expected_config)

    if not sorted((QUALIFICATION_DIR / "raw").rglob("result.json")):
        resolved = base.resolve_config(config_path)
        frozen = json.loads(config_path.read_text(encoding="utf-8"))
        agent = resolved["agents"][0]
        if (
            agent["import_path"] != AGENT_IMPORT
            or agent["model_name"] != f"openrouter/{base.MODEL}"
            or agent["kwargs"]["runtime_budget_seconds"] != 2400
            or agent["kwargs"]["runtime_budget_rail_enabled"] is not False
            or agent["kwargs"]["context_compression_enabled"] is not True
            or agent["kwargs"]["openrouter_route"]
            != {
                "only": ["deepseek"],
                "allow_fallbacks": False,
                "require_parameters": True,
            }
            or resolved["agent_setup_timeout_multiplier"] != 2.0
            or frozen["retry"]["max_retries"] != 0
        ):
            raise RuntimeError("resolved OpenJiuwen TUA root-workspace configuration drift")
        completed = subprocess.run(
            base.harbor_command(config_path),
            cwd=ROOT,
            env=base.uv_environment(api_key="preflight-not-a-secret"),
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"TUA OpenJiuwen root-workspace install-only run failed: "
                f"{completed.returncode}"
            )

    base.qualification_result()
    manifest = base.materialize_paid_campaign()
    base.verify_paid_campaign()
    print("Zero-cost TUA OpenJiuwen root-workspace preflight passed; no paid model call was made.")
    print(f"Manifest SHA256 {manifest['manifest_sha256']}")
    return manifest


def launch(approved_hash: str) -> int:
    base.verify_paid_campaign()
    command = (
        f"set -a; source {shlex.quote(str(ROOT / '.env'))}; set +a; "
        f"exec {shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} "
        f"run --approved-manifest-sha256 {shlex.quote(approved_hash)}"
    )
    subprocess.run(
        [
            "tmux", "new-session", "-d", "-s", CAMPAIGN_ID,
            "-c", str(ROOT), "bash", "-lc", command,
        ],
        check=True,
    )
    print(f"Started tmux session {CAMPAIGN_ID}")
    return 0


def install_overrides() -> None:
    base.CAMPAIGN_ID = CAMPAIGN_ID
    base.QUALIFICATION_ID = QUALIFICATION_ID
    base.RUN_DIR = RUN_DIR
    base.QUALIFICATION_DIR = QUALIFICATION_DIR
    base.agent_config = agent_config
    base.source_hashes = source_hashes
    base.manifest_body = manifest_body
    base.preflight = preflight
    base.launch = launch


if __name__ == "__main__":
    install_overrides()
    raise SystemExit(base.main())
