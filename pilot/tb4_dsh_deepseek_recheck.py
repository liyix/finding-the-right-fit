#!/usr/bin/env python3
"""Recheck DSH standard against OpenRouter's DeepSeek first-party endpoint."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pilot"))

import provider_tool_smoke as endpoint_tools
import tb4_dsh_glm as legacy
import tb4_dsh_openrouter as base


CAMPAIGN_ID = "pilot-tb4-dsh-openrouter-deepseek-recheck-20260831-r1"
PREPARED_AT_UTC = "2026-08-31T13:07:15Z"
RUN_DIR = ROOT / "runs" / CAMPAIGN_ID
MODEL = "deepseek-v4-pro"
_ORIGINAL_BUILD_MANIFEST = base.build_manifest


def configure_base() -> None:
    spec = copy.deepcopy(base.MODEL_SPECS[MODEL])
    spec["known_account_constraint"] = (
        "The same exact route returned two pre-generation 404 responses before the "
        "operator changed the OpenRouter account setting. This campaign rechecks the "
        "new external account state without fallback."
    )
    base.CAMPAIGN_ID = CAMPAIGN_ID
    base.PREPARED_AT_UTC = PREPARED_AT_UTC
    base.RUN_DIR = RUN_DIR
    base.HARBOR_JOBS_DIR = RUN_DIR / "harbor"
    base.CAMPAIGN_CONCURRENCY = 1
    base.MODEL_SPECS = {MODEL: spec}


def build_manifest() -> dict:
    configure_base()
    manifest = _ORIGINAL_BUILD_MANIFEST()
    manifest["kind"] = "paid-account-setting-recheck-and-benchmark-qualification"
    manifest["scope"].pop("per_provider_concurrency", None)
    manifest["scope"]["n_concurrent_trials"] = 1
    manifest["scope"]["expected_peak_provider_requests"] = (
        "2: DSH standard retains concurrent main and title calls"
    )
    manifest["runner"]["entry"] = "one frozen harbor run command in persistent tmux"
    manifest["cells"][0]["endpoint"]["authenticated_snapshot"] = {
        "checked_at_utc": PREPARED_AT_UTC,
        "name": "DeepSeek | deepseek/deepseek-v4-pro-20260813",
        "provider_name": "DeepSeek",
        "tag": "deepseek",
        "quantization": "unknown",
        "status": 0,
        "context_length": 1_048_576,
        "max_completion_tokens": 384_000,
        "supported_parameters": [
            "reasoning",
            "include_reasoning",
            "max_tokens",
            "temperature",
            "top_p",
            "stop",
            "frequency_penalty",
            "presence_penalty",
            "logprobs",
            "top_logprobs",
            "tools",
            "tool_choice",
            "response_format",
            "reasoning_effort",
        ],
    }
    manifest["routing_compatibility"]["credential_transport"] = (
        "Harbor host resolves OPENROUTER_API_KEY, uploads it as a mode-600 ephemeral "
        "container file, exports it inside the task shell, and deletes it on exit; "
        "the value is absent from docker-exec process arguments, configs, and logs"
    )
    manifest["accounting"]["estimated_cost_usd"] = (
        "$0.10-$3 for one text-only TB4 task; monitor only, no dollar hard stop"
    )
    manifest["post_run_audit"]["scope"] = (
        "the complete DeepSeek trajectory and every injector/provider request"
    )
    manifest["post_run_audit"]["checks"] = [
        "account setting permits generation and actual route is DeepSeek first-party",
        "standard preset, initial 26-tool catalog, tool-result and reasoning replay",
        "main/title separation, retries, 429, timeout, compaction and output cap",
        "provider/DSH/Harbor tokens and settled cost without double counting",
        "no secret in process arguments, logs, manifest or collected artifacts",
        "no skills, MCP, extra prompt, memory or benchmark-specific augmentation",
    ]
    manifest["source_sha256"]["pilot/tb4_dsh_deepseek_recheck.py"] = (
        base.sha256_file(Path(__file__))
    )
    manifest.pop("manifest_sha256", None)
    manifest["manifest_sha256"] = hashlib.sha256(
        base.canonical_json(manifest)
    ).hexdigest()
    return manifest


def verify_endpoint(api_key: str) -> dict:
    evidence = endpoint_tools.verify_endpoint_snapshot(
        {"logical_model": MODEL, "resolved_model": base.MODEL_SPECS[MODEL]["model"]},
        api_key,
    )
    endpoint = evidence["selected_endpoint"]
    if (
        endpoint.get("provider_name") != "DeepSeek"
        or endpoint.get("tag") != "deepseek"
        or endpoint.get("status") not in (0, None)
        or endpoint.get("context_length") != 1_048_576
        or endpoint.get("max_completion_tokens") != 384_000
        or not {"tools", "reasoning_effort", "max_tokens"}.issubset(
            set(endpoint.get("supported_parameters") or [])
        )
    ):
        raise RuntimeError("DeepSeek first-party endpoint metadata drifted")
    return evidence


def preflight() -> int:
    configure_base()
    manifest = build_manifest()
    api_key = legacy.load_key()
    endpoint = verify_endpoint(api_key)
    with tempfile.TemporaryDirectory(prefix="tb4-dsh-deepseek-", dir="/tmp") as raw:
        temp = Path(raw)
        legacy.injector_self_test(temp)
        config_path = temp / "resolved.json"
        base.write_json(config_path, base.harbor_config(MODEL))
        completed = subprocess.run(
            legacy.harbor_command(config_path, print_config=True),
            cwd=ROOT,
            env=legacy.tool_env("preflight-dummy"),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if completed.returncode != 0:
            print(completed.stdout[-3000:] + completed.stderr[-3000:], file=sys.stderr)
            return 2
        resolved = json.loads(completed.stdout)
        agent = (resolved.get("agents") or [{}])[0]
        kwargs = agent.get("kwargs") or {}
        if (
            agent.get("model_name") != "openrouter/deepseek/deepseek-v4-pro-0813"
            or kwargs.get("reasoning_effort") != "high"
            or kwargs.get("context_window") != 1_048_576
            or kwargs.get("max_tokens") != 384_000
            or kwargs.get("input_modalities") != ["text"]
            or kwargs.get("openrouter_route")
            != {
                "only": ["deepseek"],
                "allow_fallbacks": False,
                "require_parameters": True,
            }
        ):
            raise RuntimeError("Harbor DeepSeek configuration resolution drifted")
        install_path = temp / "install.json"
        base.write_json(
            install_path,
            base.harbor_config(MODEL, install_only=True, jobs_dir=temp / "install-only"),
        )
        installed = subprocess.run(
            legacy.harbor_command(install_path),
            cwd=ROOT,
            env=legacy.tool_env("preflight-dummy"),
            capture_output=True,
            text=True,
            timeout=1200,
            check=False,
        )
        if installed.returncode != 0:
            print(installed.stdout[-6000:] + installed.stderr[-6000:], file=sys.stderr)
            return 3
    print(json.dumps(endpoint, indent=2))
    print("Zero-cost DeepSeek first-party endpoint/config/install preflight passed.")
    print(f"Prospective manifest SHA256: {manifest['manifest_sha256']}")
    print("No model generation was made.")
    return 0


def run(approved_sha256: str) -> int:
    configure_base()
    base.build_manifest = build_manifest
    base.run(approved_sha256)
    result_paths = sorted((RUN_DIR / "harbor").glob("*/*/result.json"))
    valid = False
    if len(result_paths) == 1:
        result = json.loads(result_paths[0].read_text())
        valid = result.get("exception_info") is None
    status_path = RUN_DIR / "run-status.json"
    status = json.loads(status_path.read_text()) if status_path.exists() else {}
    status["trial_result_valid"] = valid
    base.write_json(status_path, status)
    return 0 if valid else 1


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
