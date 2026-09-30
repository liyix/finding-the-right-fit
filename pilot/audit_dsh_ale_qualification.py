#!/usr/bin/env python3
"""Audit one five-model DSH qualification campaign run by ALE."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pilot"))

import provider_smoke as smoke
import tb4_dsh_glm as provider_base


GENERATION_FIELDS = (
    "id",
    "model",
    "provider_name",
    "total_cost",
    "tokens_prompt",
    "tokens_completion",
    "native_tokens_prompt",
    "native_tokens_cached",
    "native_tokens_completion",
    "native_tokens_reasoning",
    "cache_discount",
    "finish_reason",
    "native_finish_reason",
    "generation_time",
    "latency",
    "streamed",
    "cancelled",
    "service_tier",
    "created_at",
    "api_type",
)


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise RuntimeError(f"expected JSON object: {path}")
    return value


def jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    records: list[dict[str, Any]] = []
    malformed = 0
    for line in path.read_text(errors="replace").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if isinstance(item, dict):
            records.append(item)
        else:
            malformed += 1
    return records, malformed


def one(root: Path, name: str) -> Path:
    matches = sorted(root.rglob(name))
    if len(matches) != 1:
        raise RuntimeError(f"expected one {name} under {root}, found {len(matches)}")
    return matches[0]


def slim_generation(record: dict[str, Any]) -> dict[str, Any]:
    return {field: record.get(field) for field in GENERATION_FIELDS}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--fetch-provider", action="store_true")
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    manifest = read_json(run_dir / "manifest.json")
    state = read_json(run_dir / "run-state.json")
    specs = {item["logical"]: item for item in manifest["models"]}

    provider_path = run_dir / "provider-generations.json"
    if args.fetch_provider:
        key = provider_base.load_key()
        fetched = smoke.fetch_openrouter_generation_records(run_dir / "raw", key)
        provider_payload = {
            "source": fetched["source"],
            "generation_ids_found": fetched["generation_ids_found"],
            "records": [slim_generation(item) for item in fetched["records"]],
            "errors": fetched["errors"],
        }
        provider_path.write_text(json.dumps(provider_payload, indent=2, sort_keys=True) + "\n")
        provider_path.chmod(0o600)
    provider_payload = read_json(provider_path)
    records_by_id = {
        item["id"]: item
        for item in provider_payload.get("records", [])
        if isinstance(item, dict) and item.get("id")
    }

    cells: list[dict[str, Any]] = []
    all_generation_ids: set[str] = set()
    secret_file_count = 0
    authorization_file_count = 0
    key = provider_base.load_key()
    for path in run_dir.rglob("*"):
        if not path.is_file() or path in {provider_path, run_dir / "audit-final.json"}:
            continue
        if path.stat().st_size > 20 * 1024 * 1024:
            continue
        content = path.read_text(errors="ignore")
        secret_file_count += int(bool(key) and key in content)
        authorization_file_count += int("Authorization: Bearer" in content)

    for logical, spec in specs.items():
        root = run_dir / "raw" / "ale" / logical
        run = read_json(one(root, "run.json"))
        trajectory = read_json(one(root, "trajectory.json"))
        evaluation = read_json(one(root, "eval_result.json"))
        session_path = one(root, "session.jsonl")
        injector_path = one(root, "openrouter-injector.jsonl")
        session, malformed_session = jsonl(session_path)
        injector, malformed_injector = jsonl(injector_path)

        end_records = [item for item in injector if item.get("event") == "request_end"]
        header_records = [item for item in injector if item.get("event") == "upstream_headers"]
        generation_ids = [
            item.get("openrouter_generation_id")
            for item in end_records
            if item.get("openrouter_generation_id")
        ]
        all_generation_ids.update(generation_ids)
        provider_records = [records_by_id[item] for item in generation_ids if item in records_by_id]

        headers = [
            (item.get("data") or {}).get("header") or {}
            for item in session
            if item.get("type") == "request/header"
        ]
        sessions = [item for item in session if item.get("type") == "session"]
        contexts = [(item.get("data") or {}) for item in session if item.get("type") == "request/context"]
        permission_presets = [(item.get("data") or {}).get("preset") for item in session if item.get("type") == "permission/preset"]
        sandbox_modes = [(item.get("data") or {}).get("mode") for item in session if item.get("type") == "sandbox/mode"]
        approval_policies = [(item.get("data") or {}).get("policy") for item in session if item.get("type") == "approval/policy"]
        assistants = [item for item in session if item.get("type") == "assistant/message"]
        tool_calls = [item for item in session if item.get("type") == "tool/call"]
        tool_results = [item for item in session if item.get("type") == "tool/result"]
        call_ids = [(item.get("data") or {}).get("callId") for item in tool_calls]
        result_ids = [
            (((item.get("data") or {}).get("message") or {}).get("source") or {}).get("callId")
            for item in tool_results
        ]
        tool_names = Counter((item.get("data") or {}).get("name") for item in tool_calls)
        reasoning_blocks = sum(
            block.get("type") == "reasoning"
            for item in assistants
            for block in ((((item.get("data") or {}).get("message") or {}).get("content")) or [])
            if isinstance(block, dict)
        )
        replay_states = sum(
            isinstance(
                (((item.get("data") or {}).get("message") or {}).get("source") or {}).get("replayState"),
                dict,
            )
            for item in assistants
        )
        native_usage = Counter()
        for item in assistants:
            usage = (item.get("data") or {}).get("usage") or {}
            for field in (
                "inputTokens",
                "outputTokens",
                "cacheReadTokens",
                "cacheWriteTokens",
                "reasoningTokens",
            ):
                native_usage[field] += int(usage.get(field) or 0)

        retry_events = [item for item in session if "retry" in str(item.get("type", "")).lower()]
        compactions = [item for item in session if "compact" in str(item.get("type", "")).lower()]
        session_errors = [item for item in session if "error" in str(item.get("type", "")).lower()]
        main_wire = [item for item in end_records if (item.get("request_audit") or {}).get("tools_count")]
        title_wire = [item for item in end_records if not (item.get("request_audit") or {}).get("tools_count")]
        main_ids = {item.get("openrouter_generation_id") for item in main_wire}
        title_ids = {item.get("openrouter_generation_id") for item in title_wire}
        main_provider_records = [item for item in provider_records if item.get("id") in main_ids]
        title_provider_records = [item for item in provider_records if item.get("id") in title_ids]
        main_cap_hits = sum(
            item.get("finish_reason") in {"length", "max_tokens"}
            or item.get("native_finish_reason") in {"length", "max_tokens"}
            for item in main_provider_records
        )

        expected_route = spec["route"]
        controls_ok = (
            len(headers) == 1
            and (headers[0].get("config") or {}).get("model") == spec["requested_model"]
            and (headers[0].get("config") or {}).get("reasoningEffort") == "high"
            and (headers[0].get("config") or {}).get("maxTokens") == spec["max_output_tokens"]
            and len(headers[0].get("tools") or []) == 40
            and contexts == [{"provider": "ale-openrouter", "model": spec["requested_model"], "contextWindow": spec["context_tokens"]}]
            and permission_presets == ["danger-full-access"]
            and sandbox_modes == ["danger-full-access"]
            and approval_policies == ["never"]
            and sum(str(item.get("name", "")).startswith("mcp__cua__") for item in headers[0].get("tools") or []) == 14
            and not [
                item for item in headers[0].get("tools") or []
                if str(item.get("name", "")).startswith("mcp__")
                and not str(item.get("name", "")).startswith("mcp__cua__")
            ]
        )
        wire_ok = all(
            item.get("upstream_status") == 200
            and item.get("upstream_response_completed") is True
            and item.get("error") is None
            and item.get("injector_retry_count") == 0
            and item.get("changed_fields") == ["provider"]
            and (item.get("request_audit") or {}).get("provider_only") == expected_route["only"]
            and (item.get("request_audit") or {}).get("provider_allow_fallbacks") is False
            and (item.get("request_audit") or {}).get("provider_require_parameters") is True
            and (item.get("request_audit") or {}).get("provider_quantizations") == expected_route.get("quantizations")
            and (item.get("request_audit") or {}).get("reasoning_effort") == "high"
            for item in end_records
        )
        route_ok = (
            len(provider_records) == len(generation_ids)
            and not provider_payload.get("errors")
            and {item.get("model") for item in provider_records} == {spec["expected_actual_model"]}
            and {item.get("provider_name") for item in provider_records} == {spec["expected_provider"]}
        )
        lifecycle_ok = (
            run.get("status") == "completed"
            and run.get("score") == 1.0
            and evaluation.get("eval_status") == "success"
            and evaluation.get("score") == 1.0
            and trajectory.get("final_metrics", {}).get("status") == "completed"
            and trajectory.get("final_metrics", {}).get("reward") == 1.0
            and (trajectory.get("extra", {}).get("dsh", {}).get("exit_code")) == 0
        )
        trajectory_ok = (
            len(sessions) == 1
            and sessions[0].get("agentPreset") == "standard"
            and malformed_session == 0
            and malformed_injector == 0
            and sorted(call_ids) == sorted(result_ids)
            and len(main_wire) == len(assistants)
            and len(title_wire) == 1
            and not retry_events
            and not session_errors
            and not compactions
            and main_cap_hits == 0
        )
        accepted = lifecycle_ok and controls_ok and wire_ok and route_ok and trajectory_ok

        cells.append(
            {
                "logical_model": logical,
                "accepted": accepted,
                "trial_id": run["run_id"],
                "score": run["score"],
                "timing_seconds": run.get("timings", {}).get("duration_s"),
                "routing": {
                    "requested_model": spec["requested_model"],
                    "actual_models": sorted({item.get("model") for item in provider_records}),
                    "actual_providers": sorted({item.get("provider_name") for item in provider_records}),
                    "strict_only": expected_route["only"],
                    "quantizations": expected_route.get("quantizations"),
                    "fallback_observed": False if route_ok else None,
                    "provider_generation_count": len(provider_records),
                    "generation_lookup_missing": len(generation_ids) - len(provider_records),
                },
                "wire": {
                    "request_count": len(end_records),
                    "main_requests": len(main_wire),
                    "title_requests": len(title_wire),
                    "status_counts": dict(Counter(str(item.get("upstream_status")) for item in end_records)),
                    "max_tokens": sorted({(item.get("request_audit") or {}).get("max_tokens") for item in end_records}),
                    "tool_catalog_sizes": sorted({(item.get("request_audit") or {}).get("tools_count") for item in end_records}),
                    "main_tool_schema_hashes": sorted({(item.get("request_audit") or {}).get("tools_sha256") for item in main_wire}),
                    "injector_retries": sum(int(item.get("injector_retry_count") or 0) for item in end_records),
                    "changed_fields": sorted({tuple(item.get("changed_fields") or []) for item in end_records}),
                },
                "trajectory": {
                    "agent_preset": sessions[0].get("agentPreset") if sessions else None,
                    "context_window": contexts[0].get("contextWindow") if len(contexts) == 1 else None,
                    "permission_preset": permission_presets[0] if len(permission_presets) == 1 else None,
                    "sandbox_mode": sandbox_modes[0] if len(sandbox_modes) == 1 else None,
                    "approval_policy": approval_policies[0] if len(approval_policies) == 1 else None,
                    "tool_catalog_size": len(headers[0].get("tools") or []) if headers else None,
                    "standard_tools": 26 if controls_ok else None,
                    "ale_cua_tools": 14 if controls_ok else None,
                    "tool_calls": len(tool_calls),
                    "tool_results": len(tool_results),
                    "tool_call_result_ids_closed": sorted(call_ids) == sorted(result_ids),
                    "tools_used": dict(sorted((str(key), value) for key, value in tool_names.items())),
                    "assistant_messages": len(assistants),
                    "reasoning_blocks": reasoning_blocks,
                    "assistant_replay_states": replay_states,
                    "compaction_count": len(compactions),
                    "main_output_cap_hits": main_cap_hits,
                    "dsh_retry_events": len(retry_events),
                    "session_error_events": len(session_errors),
                    "malformed_jsonl_lines": malformed_session + malformed_injector,
                },
                "tokens_and_cost": {
                    "provider": {
                        "cost_usd": sum(float(item.get("total_cost") or 0) for item in provider_records),
                        "main_cost_usd": sum(float(item.get("total_cost") or 0) for item in main_provider_records),
                        "title_cost_usd": sum(float(item.get("total_cost") or 0) for item in title_provider_records),
                        "native_prompt_tokens": sum(int(item.get("native_tokens_prompt") or 0) for item in provider_records),
                        "native_cached_tokens": sum(int(item.get("native_tokens_cached") or 0) for item in provider_records),
                        "native_completion_tokens": sum(int(item.get("native_tokens_completion") or 0) for item in provider_records),
                        "native_reasoning_tokens": sum(int(item.get("native_tokens_reasoning") or 0) for item in provider_records),
                        "title_native_prompt_tokens": sum(int(item.get("native_tokens_prompt") or 0) for item in title_provider_records),
                        "title_native_completion_tokens": sum(int(item.get("native_tokens_completion") or 0) for item in title_provider_records),
                    },
                    "dsh_main_only": dict(native_usage),
                    "accounting_note": "provider includes the title call; DSH/ALE counters cover main calls only and are not added",
                },
                "checks": {
                    "lifecycle_and_verifier": lifecycle_ok,
                    "resolved_controls_and_tool_catalog": controls_ok,
                    "wire_route_and_completion": wire_ok,
                    "provider_route_and_generation_coverage": route_ok,
                    "native_trajectory": trajectory_ok,
                },
            }
        )

    before = read_json(run_dir / "openrouter-usage-before.json")
    after = read_json(run_dir / "openrouter-usage-after.json")
    provider_records = list(records_by_id.values())
    campaign = {
        "planned_trials": 5,
        "completed_trials": sum(item["checks"]["lifecycle_and_verifier"] for item in cells),
        "accepted_trials": sum(item["accepted"] for item in cells),
        "reward_1_trials": sum(item["score"] == 1.0 for item in cells),
        "provider_generations": len(provider_records),
        "provider_cost_usd": sum(float(item.get("total_cost") or 0) for item in provider_records),
        "provider_native_prompt_tokens": sum(int(item.get("native_tokens_prompt") or 0) for item in provider_records),
        "provider_native_cached_tokens": sum(int(item.get("native_tokens_cached") or 0) for item in provider_records),
        "provider_native_completion_tokens": sum(int(item.get("native_tokens_completion") or 0) for item in provider_records),
        "provider_native_reasoning_tokens": sum(int(item.get("native_tokens_reasoning") or 0) for item in provider_records),
        "account_usage_delta_at_runner_shutdown_usd": float(after.get("usage") or 0) - float(before.get("usage") or 0),
        "account_delta_note": "diagnostic only because the key may be shared and settlement can lag; generation records are authoritative",
        "injector_request_end_records": sum(item["wire"]["request_count"] for item in cells),
        "injector_header_plus_end_records": sum(
            len(jsonl(one(run_dir / "raw" / "ale" / item["logical_model"], "openrouter-injector.jsonl"))[0])
            for item in cells
        ),
        "title_requests": sum(item["wire"]["title_requests"] for item in cells),
        "tool_calls": sum(item["trajectory"]["tool_calls"] for item in cells),
        "tool_results": sum(item["trajectory"]["tool_results"] for item in cells),
        "reasoning_blocks": sum(item["trajectory"]["reasoning_blocks"] for item in cells),
        "compactions": sum(item["trajectory"]["compaction_count"] for item in cells),
        "main_output_caps": sum(item["trajectory"]["main_output_cap_hits"] for item in cells),
        "retry_events": sum(item["trajectory"]["dsh_retry_events"] for item in cells),
        "injector_retries": sum(item["wire"]["injector_retries"] for item in cells),
        "secret_value_file_count": secret_file_count,
        "authorization_header_file_count": authorization_file_count,
        "generation_id_coverage": {
            "captured_unique": len(all_generation_ids),
            "provider_records": len(provider_records),
            "missing": sorted(all_generation_ids - set(records_by_id)),
            "extra": sorted(set(records_by_id) - all_generation_ids),
        },
    }
    accepted = (
        state.get("status") == "finished"
        and state.get("manifest_sha256") == manifest.get("manifest_sha256")
        and all(item["accepted"] for item in cells)
        and campaign["generation_id_coverage"]["missing"] == []
        and campaign["generation_id_coverage"]["extra"] == []
        and provider_payload.get("errors") == []
        and secret_file_count == 0
        and authorization_file_count == 0
    )
    audit = {
        "campaign_id": manifest["campaign_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "decision": "accept as ALE text-task benchmark-ready qualification evidence" if accepted else "reject pending anomaly resolution",
        "maximum_claim": (
            "B for the exact ALE local-Docker text-task lifecycle and frozen five DSH/OpenRouter routes; "
            "does not qualify multimodal tasks or compaction recovery because neither occurred"
        ),
        "audit_scope": "all five trials, every ALE result, full native DSH sessions, every injector request, and every provider generation",
        "campaign": campaign,
        "cells": cells,
        "known_limits": [
            "single easy text-only ALE task; score is qualification evidence, not model-quality evidence",
            "no compaction occurred, so compaction recovery remains untested",
            "no image/CUA tool was used, so multimodal/CUA execution remains unqualified",
            "reasoning replay state is preserved in native sessions and no continuity error occurred, but request bodies are stored only as hashes",
            "effective quantization is evidenced by strict route selection for GLM/Kimi; generation metadata has no separate quantization field",
        ],
    }
    digest = hashlib.sha256(canonical(audit)).hexdigest()
    audit["audit_sha256"] = digest
    audit_path = run_dir / "audit-final.json"
    audit_path.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n")
    audit_path.chmod(0o444)
    print(json.dumps({
        "decision": audit["decision"],
        "accepted_trials": campaign["accepted_trials"],
        "provider_generations": campaign["provider_generations"],
        "provider_cost_usd": campaign["provider_cost_usd"],
        "account_delta_usd": campaign["account_usage_delta_at_runner_shutdown_usd"],
    }, indent=2, sort_keys=True))
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
