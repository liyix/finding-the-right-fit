#!/usr/bin/env python3
"""Audit one OpenHands/GPT Responses qualification run without exposing payloads."""

from __future__ import annotations

import argparse
import base64
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen


EXPECTED_PROVIDER = {
    "allow_fallbacks": False,
    "only": ["openai"],
    "require_parameters": True,
}
REASONING_KEYS = ("id", "summary", "content", "encrypted_content", "status")
PROVIDER_KEYS = (
    "id",
    "model",
    "provider_name",
    "total_cost",
    "native_tokens_prompt",
    "native_tokens_cached",
    "native_tokens_completion",
    "native_tokens_reasoning",
    "finish_reason",
    "native_finish_reason",
    "generation_time",
    "latency",
    "streamed",
    "cancelled",
    "service_tier",
)


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def reasoning_value(item: dict[str, Any]) -> dict[str, Any]:
    return {key: item.get(key) for key in REASONING_KEYS}


def generation_id(response_id: str) -> str:
    encoded = response_id.removeprefix("resp_")
    decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
    marker = "response_id:"
    if marker not in decoded:
        raise ValueError(f"response id lacks generation marker: {response_id[:24]}")
    return decoded.split(marker, 1)[1]


def fetch_generation(gen_id: str, key: str) -> dict[str, Any]:
    url = "https://openrouter.ai/api/v1/generation?" + urlencode({"id": gen_id})
    for attempt in range(4):
        try:
            request = Request(url, headers={"Authorization": f"Bearer {key}"})
            with urlopen(request, timeout=30) as response:
                data = json.load(response).get("data")
            if not isinstance(data, dict):
                raise RuntimeError("generation response lacks data object")
            return {name: data.get(name) for name in PROVIDER_KEYS}
        except Exception:
            if attempt == 3:
                raise
            time.sleep(2**attempt)
    raise AssertionError("unreachable")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--fetch-provider", action="store_true")
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()

    manifest = json.loads((run_dir / "manifest.json").read_text())
    job_dirs = [p for p in (run_dir / "harbor").iterdir() if p.is_dir()]
    if len(job_dirs) != 1:
        raise RuntimeError(f"expected one Harbor job, found {len(job_dirs)}")
    job_dir = job_dirs[0]
    trial_dirs = [p for p in job_dir.iterdir() if p.is_dir()]
    if len(trial_dirs) != 1:
        raise RuntimeError(f"expected one trial, found {len(trial_dirs)}")
    trial_dir = trial_dirs[0]
    result = json.loads((trial_dir / "result.json").read_text())
    resolved = json.loads((trial_dir / "agent" / "resolved-llm-config.json").read_text())
    events = json.loads((trial_dir / "agent" / "openhands-events.json").read_text())
    native_log = (trial_dir / "agent" / "openhands_sdk.txt").read_text(
        errors="replace"
    )

    completions: list[dict[str, Any]] = []
    for path in (trial_dir / "agent" / "completions").glob("*.json"):
        value = json.loads(path.read_text())
        value["_path"] = path.name
        completions.append(value)
    completions.sort(key=lambda value: value.get("timestamp", 0))

    action_by_response: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        if event.get("kind") == "ActionEvent":
            action_by_response[event.get("llm_response_id", "")].append(event)

    replay_turns: list[dict[str, Any]] = []
    replay_anomalies: list[dict[str, Any]] = []
    eligible_items = replayed_items = multi_item_turns = 0
    event_persistence_anomalies = 0
    final_unreplayed_items = 0
    all_response_reasoning_items = 0
    response_ids: list[str] = []
    control_anomalies: list[str] = []
    tool_hashes: set[str] = set()
    tool_counts: set[int] = set()
    usage = Counter()
    native_cost = 0.0
    cap_hits = 0

    for index, completion in enumerate(completions):
        response = completion.get("response") or {}
        response_id = response.get("id")
        if not isinstance(response_id, str):
            control_anomalies.append(f"completion[{index}] missing response id")
            continue
        response_ids.append(response_id)
        kwargs = completion.get("kwargs") or {}
        if completion.get("llm_path") != "responses":
            control_anomalies.append(f"completion[{index}] non-Responses path")
        if completion.get("context_window") != 1_000_000:
            control_anomalies.append(f"completion[{index}] context mismatch")
        if kwargs.get("max_output_tokens") != 128_000:
            control_anomalies.append(f"completion[{index}] output mismatch")
        if (kwargs.get("reasoning") or {}).get("effort") != "high":
            control_anomalies.append(f"completion[{index}] reasoning mismatch")
        if (kwargs.get("extra_body") or {}).get("provider") != EXPECTED_PROVIDER:
            control_anomalies.append(f"completion[{index}] provider body mismatch")
        for field in ("temperature", "top_p", "top_k", "seed"):
            if field in kwargs:
                control_anomalies.append(f"completion[{index}] sent {field}")
        tools = completion.get("tools") or []
        tool_counts.add(len(tools))
        tool_hashes.add(sha256_json(tools))
        if response.get("status") != "completed":
            cap_hits += 1
        summary = completion.get("usage_summary") or {}
        for field in (
            "prompt_tokens",
            "completion_tokens",
            "reasoning_tokens",
            "cache_read_tokens",
        ):
            usage[field] += int(summary.get(field) or 0)
        native_cost += float(completion.get("cost") or 0)

        output = response.get("output") or []
        reasoning = [item for item in output if item.get("type") == "reasoning"]
        calls = [item for item in output if item.get("type") == "function_call"]
        all_response_reasoning_items += len(reasoning)
        if not calls:
            continue
        actions = action_by_response.get(response_id, [])
        if len(actions) != len(calls):
            event_persistence_anomalies += 1
        elif actions:
            event_reasoning = actions[0].get("responses_reasoning_items") or []
            if [reasoning_value(x) for x in event_reasoning] != [
                reasoning_value(x) for x in reasoning
            ]:
                event_persistence_anomalies += 1
            if any(action.get("responses_reasoning_items") for action in actions[1:]):
                event_persistence_anomalies += 1

        if index + 1 >= len(completions):
            final_unreplayed_items += len(reasoning)
            continue
        eligible_items += len(reasoning)
        multi_item_turns += len(reasoning) > 1
        next_input = completions[index + 1].get("input") or []
        observed: list[dict[str, Any]] = []
        positions: list[int] = []
        turn_anomalies: list[str] = []
        for source_item in reasoning:
            target = reasoning_value(source_item)
            matches = [
                (position, item)
                for position, item in enumerate(next_input)
                if item.get("type") == "reasoning"
                and reasoning_value(item) == target
            ]
            if len(matches) != 1:
                turn_anomalies.append(
                    f"{target.get('id')}: expected one exact replay, got {len(matches)}"
                )
                continue
            positions.append(matches[0][0])
            observed.append(reasoning_value(matches[0][1]))
            replayed_items += 1
        if positions != sorted(positions):
            turn_anomalies.append("replayed item order changed")
        if observed != [reasoning_value(item) for item in reasoning]:
            turn_anomalies.append("replayed item sequence differs")
        replay_turns.append(
            {
                "completion_index": index,
                "response_id_sha256": hashlib.sha256(response_id.encode()).hexdigest(),
                "reasoning_items": len(reasoning),
                "tool_calls": len(calls),
                "status": "passed" if not turn_anomalies else "failed",
            }
        )
        if turn_anomalies:
            replay_anomalies.append(
                {"completion_index": index, "anomalies": turn_anomalies}
            )

    action_ids = {
        event.get("tool_call_id")
        for event in events
        if event.get("kind") == "ActionEvent"
    }
    observation_ids = {
        event.get("tool_call_id")
        for event in events
        if event.get("kind") == "ObservationEvent"
    }
    event_kinds = Counter(event.get("kind") for event in events)
    activated_skills = sum(len(event.get("activated_skills") or []) for event in events)
    operator_prompt_contamination = any(
        "# Repository Instructions" in canonical(event)
        for event in events
        if event.get("kind") == "SystemPromptEvent"
    )

    provider_records: list[dict[str, Any]] = []
    provider_errors: list[str] = []
    provider_path = run_dir / "provider-generations.json"
    if args.fetch_provider:
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise RuntimeError("OPENROUTER_API_KEY is required with --fetch-provider")
        gen_ids = [generation_id(response_id) for response_id in response_ids]
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(fetch_generation, gen_id, key): gen_id for gen_id in gen_ids}
            for future in as_completed(futures):
                try:
                    provider_records.append(future.result())
                except Exception as exc:
                    provider_errors.append(f"{futures[future]}: {type(exc).__name__}: {exc}")
        provider_records.sort(key=lambda value: str(value.get("id")))
        provider_path.write_text(
            json.dumps(provider_records, indent=2, sort_keys=True) + "\n"
        )
    elif provider_path.exists():
        provider_records = json.loads(provider_path.read_text())

    provider_models = Counter(record.get("model") for record in provider_records)
    provider_names = Counter(record.get("provider_name") for record in provider_records)
    provider_cost = sum(float(record.get("total_cost") or 0) for record in provider_records)
    provider_usage = {
        field: sum(int(record.get(field) or 0) for record in provider_records)
        for field in (
            "native_tokens_prompt",
            "native_tokens_cached",
            "native_tokens_completion",
            "native_tokens_reasoning",
        )
    }

    patch = resolved.get("compatibility_patch") or {}
    completed = resolved.get("completed") or {}
    run_state = json.loads((run_dir / "run-state.json").read_text())
    hard_failures = {
        "lifecycle": not (
            run_state.get("status") == "finished"
            and run_state.get("return_code") == 0
            and result.get("exception_info") is None
            and (result.get("verifier_result") or {}).get("rewards") is not None
        ),
        "patch_self_test": not (
            patch.get("patch_id")
            == "openhands-1.44.1-openrouter-reasoning-roundtrip-v2"
            and (patch.get("self_test") or {}).get("status") == "passed"
        ),
        "controls": bool(control_anomalies)
        or tool_counts != {5}
        or len(tool_hashes) != 1,
        "multi_item_not_observed": multi_item_turns < 1,
        "reasoning_replay": bool(replay_anomalies)
        or replayed_items != eligible_items
        or event_persistence_anomalies != 0,
        "tool_closure": bool(action_ids - observation_ids),
        "provider_evidence": bool(provider_errors)
        or len(provider_records) != len(completions)
        or set(provider_models) != {"openai/gpt-6-astra-20260903"}
        or set(provider_names) != {"OpenAI"},
        "output_cap": cap_hits != 0,
    }
    accepted = not any(hard_failures.values())
    audit = {
        "campaign_id": manifest.get("campaign_id"),
        "manifest_sha256": manifest.get("manifest_sha256"),
        "audited_at_utc": datetime.now(timezone.utc).isoformat(),
        "decision": "accept_as_B_text_path_qualification" if accepted else "reject",
        "pilot_score_policy": "reward is excluded from formal results",
        "hard_failures": hard_failures,
        "lifecycle": {
            "trial_name": result.get("trial_name"),
            "started_at": result.get("started_at"),
            "finished_at": result.get("finished_at"),
            "exception_info": result.get("exception_info"),
            "verifier_rewards": (result.get("verifier_result") or {}).get("rewards"),
            "harbor_outer_retries": 0,
        },
        "resolved_controls": {
            "model": completed.get("model"),
            "base_url": completed.get("base_url"),
            "api_mode": completed.get("api_mode"),
            "uses_responses_api": completed.get("uses_responses_api"),
            "reasoning_effort": completed.get("reasoning_effort"),
            "context_window": completed.get("effective_max_input_tokens"),
            "output_limit": completed.get("effective_max_output_tokens"),
            "temperature": completed.get("temperature"),
            "top_p": completed.get("top_p"),
            "top_k": completed.get("top_k"),
            "seed": completed.get("seed"),
            "llm_attempt_limit": completed.get("num_retries"),
            "llm_timeout_seconds": completed.get("timeout"),
            "condenser": (resolved.get("study_controls") or {}).get("condenser"),
            "tool_counts": sorted(tool_counts),
            "tool_schema_hashes": sorted(tool_hashes),
            "control_anomalies": control_anomalies,
        },
        "patch": patch,
        "reasoning_replay": {
            "completion_files": len(completions),
            "all_response_reasoning_items": all_response_reasoning_items,
            "eligible_tool_turn_items": eligible_items,
            "exactly_replayed_items": replayed_items,
            "multi_item_turns": multi_item_turns,
            "max_items_in_one_turn": max(
                (turn["reasoning_items"] for turn in replay_turns), default=0
            ),
            "final_finish_items_without_next_request": final_unreplayed_items,
            "event_persistence_anomalies": event_persistence_anomalies,
            "replay_anomalies": replay_anomalies,
            "turns": replay_turns,
        },
        "events": {
            "kind_counts": dict(event_kinds),
            "action_count": len(action_ids),
            "observation_count": len(observation_ids),
            "unclosed_action_ids": sorted(str(x) for x in action_ids - observation_ids),
            "activated_skills": activated_skills,
            "operator_prompt_contamination": operator_prompt_contamination,
            "compaction_events": sum(
                count for kind, count in event_kinds.items() if "condens" in str(kind).lower()
            ),
        },
        "requests": {
            "status_not_completed_or_cap_hits": cap_hits,
            "native_log_rate_limit_error_count": len(
                re.findall(r"RateLimitError|HTTP 429|status code: 429", native_log)
            ),
            "native_log_api_error_count": len(
                re.findall(r"AuthenticationError|APIConnectionError|BadRequestError", native_log)
            ),
        },
        "tokens_and_cost": {
            "openhands_usage_summary": dict(usage),
            "openhands_completion_cost_sum_usd": native_cost,
            "harbor_agent_result": result.get("agent_result"),
            "openrouter_generation_count": len(provider_records),
            "openrouter_models": dict(provider_models),
            "openrouter_providers": dict(provider_names),
            "openrouter_native_tokens": provider_usage,
            "openrouter_total_cost_usd": provider_cost,
            "account_usage_delta_usd": run_state.get("usage_delta_usd"),
            "account_delta_note": "shared-key delta is not authoritative if other traffic overlaps",
            "provider_fetch_errors": provider_errors,
            "primary_source": "OpenRouter per-generation records",
            "secondary_sources_are_not_added": True,
        },
        "known_limits": [
            "Qualification applies only to the exact pinned OpenHands/GPT/OpenRouter text TB4 path.",
            "It does not prove multimodal, MCP, skills, other benchmarks, or concurrency above one.",
            "OpenHands/LiteLLM reports zero Responses cost here; provider records are authoritative.",
        ],
    }
    (run_dir / "audit-final.json").write_text(
        json.dumps(audit, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps({
        "decision": audit["decision"],
        "hard_failures": hard_failures,
        "completion_files": len(completions),
        "eligible_items": eligible_items,
        "replayed_items": replayed_items,
        "multi_item_turns": multi_item_turns,
        "provider_records": len(provider_records),
        "provider_cost_usd": provider_cost,
    }, indent=2, sort_keys=True))
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
