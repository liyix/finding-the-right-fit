#!/usr/bin/env python3
"""One-task DSH×Claude cache-control requalification through OpenRouter."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pilot"))

import tb4_dsh_openrouter as base


CAMPAIGN_ID = "pilot-tb4-dsh-claude-cache-20260901-r1"
PREPARED_AT_UTC = "2026-09-01T12:20:00Z"
RUN_DIR = ROOT / "runs" / CAMPAIGN_ID
HARBOR_JOBS_DIR = RUN_DIR / "harbor"
LOGICAL_MODEL = "claude-opus-5"
CACHE_RETENTION = "short"


def harbor_config(
    *, install_only: bool = False, jobs_dir: Path | None = None
) -> dict[str, Any]:
    config = base.harbor_config(
        LOGICAL_MODEL,
        install_only=install_only,
        jobs_dir=jobs_dir or HARBOR_JOBS_DIR,
    )
    config["job_name"] = base.job_name(LOGICAL_MODEL, install_only=install_only)
    kwargs = config["agents"][0]["kwargs"]
    kwargs["cache_retention"] = CACHE_RETENTION
    kwargs["compat"] = {
        **kwargs["compat"],
        "cacheControlFormat": "anthropic",
    }
    return config


def build_manifest() -> dict[str, Any]:
    parent = base.build_manifest()
    manifest = copy.deepcopy(parent)
    manifest.pop("manifest_sha256", None)
    manifest.update(
        {
            "campaign_id": CAMPAIGN_ID,
            "kind": "paid-cache-compatibility-requalification-not-primary-score",
            "prepared_at_utc": PREPARED_AT_UTC,
        }
    )
    manifest["scope"].update(
        {
            "planned_cells": 1,
            "planned_trials": 1,
            "campaign_concurrency": 1,
            "per_provider_concurrency": 1,
        }
    )
    cell = next(
        item for item in manifest["cells"] if item["logical_model"] == LOGICAL_MODEL
    )
    cell["resolved_job_config"] = harbor_config()
    cell["qualification_target"] = (
        "real Harbor lifecycle plus non-zero provider/native cache-write and "
        "cache-read evidence on a multi-request task"
    )
    manifest["cells"] = [cell]
    manifest["runner"]["entry"] = "one frozen harbor run command"
    manifest["routing_compatibility"]["compat_fields"][
        "cacheControlFormat"
    ] = "anthropic"
    manifest["controls"]["prompt_cache"] = {
        "retention": CACHE_RETENTION,
        "source": "explicit compatibility freeze of pi-ai's native short default",
        "wire_markers": (
            "Anthropic cache_control ephemeral on system prompt, last tool, "
            "and last conversation text"
        ),
        "long_retention": False,
        "agent_behavior_change": False,
    }
    manifest["accounting"]["estimated_cost_usd"] = (
        "$5-$10, estimated from the prior identical task's $16.912435 zero-cache "
        "run and DSH output volume; no dollar hard stop"
    )
    manifest["post_run_audit"]["checks"].extend(
        [
            "cache-control markers remain present across the multi-turn tool loop",
            "provider and DSH cache-write/read counters are non-zero and reconciled",
            "cache-read ratio, total prompt tokens, and cost are compared with the prior zero-cache run",
        ]
    )
    manifest["qualification_criteria"] = [
        "actual model is anthropic/claude-opus-5-20260723 and provider is Anthropic",
        "no fallback, protocol translation, injector retry, or Harbor outer retry",
        "standard preset and 26-tool catalog; tool calls and results close normally",
        "at least one provider/native cache write and one later cache read",
        "verifier result is reached; score is not used as primary benchmark data",
        "complete native trajectory and every provider generation are audited",
    ]
    manifest["comparison_parent"] = {
        "campaign_id": base.CAMPAIGN_ID,
        "manifest_sha256": "0d4775f83c24aba56572bc2c6b85f99f87700632e7e38567ec8a4aa05da0f1de",
        "same_task_cost_usd": 16.912435,
        "same_task_provider_requests": 50,
        "same_task_cache_read_tokens": 0,
        "only_intended_behavioral_delta": (
            "restore provider-native Anthropic prompt-cache markers"
        ),
    }
    manifest["zero_cost_wire_evidence"] = {
        "library": "@earendil-works/pi-ai@0.82.1 used by DSH rc.2",
        "method": "onPayload capture aborted before HTTP dispatch",
        "baseline_markers": 0,
        "candidate_markers": 3,
        "candidate_marker_locations": [
            "messages[system].content[0].cache_control",
            "tools[-1].cache_control",
            "messages[last-conversation].content[-1].cache_control",
        ],
        "marker_value": {"type": "ephemeral"},
        "non_cache_field_diff": [],
        "network_model_calls": 0,
    }
    manifest["source_sha256"] = {
        "pilot/tb4_dsh_claude_cache.py": base.sha256_file(Path(__file__)),
        "pilot/tb4_dsh_openrouter.py": base.sha256_file(
            ROOT / "pilot" / "tb4_dsh_openrouter.py"
        ),
        "integrations/harbor_deepseek.py": base.sha256_file(
            ROOT / "integrations" / "harbor_deepseek.py"
        ),
        "integrations/openrouter_body_injector.mjs": base.sha256_file(
            ROOT / "integrations" / "openrouter_body_injector.mjs"
        ),
    }
    manifest["manifest_sha256"] = hashlib.sha256(
        base.canonical_json(manifest)
    ).hexdigest()
    return manifest


def preflight() -> int:
    manifest = build_manifest()
    with tempfile.TemporaryDirectory(prefix="tb4-dsh-claude-cache-", dir="/tmp") as raw:
        temp = Path(raw)
        base.legacy.injector_self_test(temp)
        path = temp / "config.json"
        base.write_json(path, harbor_config(jobs_dir=temp / "jobs"))
        completed = base.subprocess.run(
            base.legacy.harbor_command(path, print_config=True),
            cwd=ROOT,
            env=base.legacy.tool_env("preflight-dummy"),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stdout[-3000:] + completed.stderr[-3000:])
        resolved = json.loads(completed.stdout)
        kwargs = resolved["agents"][0]["kwargs"]
        expected_compat = {
            "supportsDeveloperRole": False,
            "supportsReasoningEffort": True,
            "maxTokensField": "max_tokens",
            "thinkingFormat": "openrouter",
            "cacheControlFormat": "anthropic",
        }
        if kwargs.get("cache_retention") != CACHE_RETENTION:
            raise RuntimeError("Harbor resolution lost cache_retention=short")
        if kwargs.get("compat") != expected_compat:
            raise RuntimeError("Harbor resolution changed the Claude compat object")
    print("Zero-cost preflight passed; no model generation was made.")
    print(f"Prospective manifest SHA256: {manifest['manifest_sha256']}")
    return 0


def freeze(approved_sha256: str) -> tuple[Path, dict[str, Any]]:
    manifest = build_manifest()
    if approved_sha256 != manifest["manifest_sha256"]:
        raise RuntimeError("approved manifest hash does not match resolved configuration")
    if RUN_DIR.exists():
        raise RuntimeError(f"immutable run directory already exists: {RUN_DIR}")
    RUN_DIR.mkdir(parents=True)
    config_path = RUN_DIR / "config.json"
    base.write_json(config_path, harbor_config())
    base.write_json(RUN_DIR / "manifest.json", manifest)
    base.write_json(
        RUN_DIR / "approval.json",
        {
            "campaign_id": CAMPAIGN_ID,
            "approved_manifest_sha256": approved_sha256,
        },
    )
    return config_path, manifest


def run(approved_sha256: str) -> int:
    api_key = base.legacy.load_key()
    config_path, manifest = freeze(approved_sha256)
    base.RUN_DIR = RUN_DIR
    base.HARBOR_JOBS_DIR = HARBOR_JOBS_DIR
    before = base.legacy.common.current_key_usage(api_key)
    base.write_json(RUN_DIR / "openrouter-usage-before.json", before, mode=0o600)
    return_code = base.run_cell(LOGICAL_MODEL, config_path, api_key)
    after = base.legacy.common.current_key_usage(api_key)
    base.write_json(RUN_DIR / "openrouter-usage-after.json", after, mode=0o600)
    audit = base.audit_cell(LOGICAL_MODEL, api_key, return_code)
    audit.update(
        {
            "campaign_id": CAMPAIGN_ID,
            "manifest_sha256": manifest["manifest_sha256"],
            "decision": "pending mandatory full trajectory and cache audit",
            "comparison_parent": manifest["comparison_parent"],
        }
    )
    base.write_json(RUN_DIR / "audit.json", audit)
    base.write_json(
        RUN_DIR / "run-status.json",
        {
            "campaign_id": CAMPAIGN_ID,
            "manifest_sha256": manifest["manifest_sha256"],
            "return_code": return_code,
            "completed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )
    return 0 if return_code == 0 else 1


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
