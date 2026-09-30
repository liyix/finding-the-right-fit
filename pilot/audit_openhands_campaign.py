#!/usr/bin/env python3
"""Audit a multi-model OpenHands/Harbor campaign and fetch provider billing."""

from __future__ import annotations

import argparse
import base64
from collections import Counter
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


PROVIDER_NAMES = {
    "claude-opus-5": "Anthropic",
    "gpt-6-astra": "OpenAI",
    "glm-5.3": "Z.AI",
    "kimi-k3": "Moonshot AI",
    "deepseek-v4-pro": "DeepSeek",
}
PROVIDER_MODELS = {
    "claude-opus-5": "anthropic/claude-opus-5-20260723",
    "gpt-6-astra": "openai/gpt-6-astra-20260903",
    "glm-5.3": "z-ai/glm-5.3-20260816",
    "kimi-k3": "moonshotai/kimi-k3-20260715",
    "deepseek-v4-pro": "deepseek/deepseek-v4-pro-20260813",
}
GENERATION_FIELDS = (
    "id", "model", "provider_name", "total_cost", "native_tokens_prompt",
    "native_tokens_cached", "native_tokens_completion", "native_tokens_reasoning",
    "finish_reason", "native_finish_reason", "generation_time", "latency",
    "streamed", "cancelled", "service_tier", "created_at", "api_type",
)
REASONING_KEYS = ("id", "summary", "content", "encrypted_content", "status")


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def gen_id(response_id: str) -> str:
    if response_id.startswith("gen-"):
        return response_id
    encoded = response_id.removeprefix("resp_")
    decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
    return decoded.split("response_id:", 1)[1]


def fetch_generation(generation_id: str, key: str) -> dict[str, Any]:
    url = "https://openrouter.ai/api/v1/generation?" + urlencode({"id": generation_id})
    for attempt in range(4):
        try:
            with urlopen(Request(url, headers={"Authorization": f"Bearer {key}"}), timeout=30) as response:
                data = json.load(response).get("data")
            if not isinstance(data, dict):
                raise RuntimeError("generation response lacks data")
            return {field: data.get(field) for field in GENERATION_FIELDS}
        except Exception:
            if attempt == 3:
                raise
            time.sleep(2**attempt)
    raise AssertionError("unreachable")


def reasoning_value(item: dict[str, Any]) -> dict[str, Any]:
    return {key: item.get(key) for key in REASONING_KEYS}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--fetch-provider", action="store_true")
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    manifest = json.loads((run_dir / "manifest.json").read_text())
    run_state = json.loads((run_dir / "run-state.json").read_text())
    model_specs = {item["logical"]: item for item in manifest["models"]}

    arms: dict[str, Any] = {}
    all_generation_ids: list[str] = []
    for logical, spec in model_specs.items():
        job = next((p for p in (run_dir / "harbor").iterdir() if f"--{logical}--" in p.name), None)
        if job is None:
            arms[logical] = {"hard_failures": {"missing_job": True}}
            continue
        trial = next((p for p in job.iterdir() if p.is_dir()), None)
        if trial is None:
            arms[logical] = {"hard_failures": {"missing_trial": True}}
            continue
        job_result = json.loads((job / "result.json").read_text())
        result = json.loads((trial / "result.json").read_text())
        resolved = json.loads((trial / "agent/resolved-llm-config.json").read_text())
        events = json.loads((trial / "agent/openhands-events.json").read_text())
        native_log = (trial / "agent/openhands_sdk.txt").read_text(errors="replace")
        completions = []
        for path in (trial / "agent/completions").glob("*.json"):
            item = json.loads(path.read_text())
            item["_path"] = path.name
            completions.append(item)
        completions.sort(key=lambda item: item.get("timestamp", 0))

        controls = []
        completed = resolved.get("completed") or {}
        expected_model = "openrouter/" + spec["id"]
        if completed.get("model") != expected_model:
            controls.append("resolved model mismatch")
        for field, expected in (
            ("base_url", "https://openrouter.ai/api/v1"),
            ("reasoning_effort", "high"),
            ("effective_max_input_tokens", 1_000_000),
            ("effective_max_output_tokens", 128_000),
        ):
            if completed.get(field) != expected:
                controls.append(f"resolved {field} mismatch")
        for field in ("temperature", "top_p", "top_k", "seed"):
            if completed.get(field) is not None:
                controls.append(f"resolved {field} was set")
        patch = resolved.get("compatibility_patch") or {}
        if patch.get("patch_id") != "openhands-1.44.1-openrouter-reasoning-roundtrip-v2" or (patch.get("self_test") or {}).get("status") != "passed":
            controls.append("reasoning patch/self-test mismatch")

        tool_hashes: set[str] = set()
        tool_counts: set[int] = set()
        response_ids: list[str] = []
        cap_hits = 0
        replay_eligible = replayed = emitted_reasoning = 0
        replay_anomalies: list[str] = []
        native_usage = Counter()
        native_cost = 0.0
        for index, completion in enumerate(completions):
            response = completion.get("response") or {}
            response_id = response.get("id")
            if not isinstance(response_id, str):
                controls.append(f"request {index} missing response id")
                continue
            response_ids.append(response_id)
            kwargs = completion.get("kwargs") or {}
            responses_path = "output" in response
            if completion.get("context_window") != 1_000_000:
                controls.append(f"request {index} context mismatch")
            if responses_path:
                if kwargs.get("max_output_tokens") != 128_000:
                    controls.append(f"request {index} output mismatch")
                if (kwargs.get("reasoning") or {}).get("effort") != "high":
                    controls.append(f"request {index} reasoning mismatch")
                if response.get("status") != "completed":
                    cap_hits += 1
            else:
                if kwargs.get("max_tokens") != 128_000:
                    controls.append(f"request {index} output mismatch")
                if kwargs.get("reasoning_effort") != "high":
                    controls.append(f"request {index} reasoning mismatch")
                choices = response.get("choices") or []
                if not choices or choices[0].get("finish_reason") in {"length", "content_filter"}:
                    cap_hits += 1
            if (kwargs.get("extra_body") or {}).get("provider") != spec["provider_route"]:
                controls.append(f"request {index} provider route mismatch")
            for field in ("temperature", "top_p", "top_k", "seed"):
                if field in kwargs:
                    controls.append(f"request {index} sent {field}")
            tools = completion.get("tools") or []
            tool_counts.add(len(tools))
            tool_hashes.add(digest(tools))
            summary = completion.get("usage_summary") or {}
            for field in ("prompt_tokens", "completion_tokens", "reasoning_tokens", "cache_read_tokens"):
                native_usage[field] += int(summary.get(field) or 0)
            native_cost += float(completion.get("cost") or 0)

            if responses_path:
                reasoning = [item for item in response.get("output") or [] if item.get("type") == "reasoning"]
                calls = [item for item in response.get("output") or [] if item.get("type") == "function_call"]
                emitted_reasoning += len(reasoning)
                if calls and index + 1 < len(completions):
                    replay_eligible += len(reasoning)
                    next_input = completions[index + 1].get("input") or []
                    observed = [reasoning_value(item) for item in next_input if item.get("type") == "reasoning"]
                    expected = [reasoning_value(item) for item in reasoning]
                    matches = sum(observed.count(item) == 1 for item in expected)
                    replayed += matches
                    if matches != len(expected):
                        replay_anomalies.append(f"request {index}: Responses reasoning replay mismatch")
            else:
                message = ((response.get("choices") or [{}])[0].get("message") or {})
                reasoning = message.get("reasoning_details") or []
                emitted_reasoning += len(reasoning)
                if message.get("tool_calls") and index + 1 < len(completions):
                    replay_eligible += len(reasoning)
                    next_messages = completions[index + 1].get("messages") or []
                    exact = sum(1 for item in next_messages if item.get("role") == "assistant" and item.get("reasoning_details") == reasoning)
                    if reasoning and exact != 1:
                        replay_anomalies.append(f"request {index}: Chat reasoning replay mismatch")
                    replayed += len(reasoning) if not reasoning or exact == 1 else 0

        action_ids = {event.get("tool_call_id") for event in events if event.get("kind") == "ActionEvent" and event.get("tool_call_id")}
        observation_ids = {event.get("tool_call_id") for event in events if event.get("kind") == "ObservationEvent" and event.get("tool_call_id")}
        event_kinds = Counter(event.get("kind") for event in events)
        rate_limits = len(re.findall(r"RateLimitError|HTTP 429|status code: 429", native_log))
        api_errors = len(re.findall(r"AuthenticationError|APIConnectionError|BadRequestError|Invalid signature", native_log, re.I))
        retry_markers = len(re.findall(r"Retrying (?:litellm|API|request)|will retry in|Retrying after", native_log, re.I))
        all_generation_ids.extend(gen_id(item) for item in response_ids)
        hard = {
            "lifecycle": not (
                (job_result.get("stats") or {}).get("n_completed_trials") == 1
                and (job_result.get("stats") or {}).get("n_errored_trials") == 0
                and result.get("exception_info") is None
                and (result.get("verifier_result") or {}).get("rewards") is not None
            ),
            "controls": bool(controls) or tool_counts != {5} or len(tool_hashes) != 1,
            "reasoning_replay": bool(replay_anomalies) or replayed != replay_eligible,
            "tool_closure": bool(action_ids - observation_ids),
            "request_errors_or_retries": bool(rate_limits or api_errors or retry_markers),
            "output_cap": cap_hits != 0,
        }
        arms[logical] = {
            "decision": "accept_TUA_lifecycle_qualification" if not any(hard.values()) else "reject",
            "hard_failures": hard,
            "trial": {"name": result.get("trial_name"), "reward": (result.get("verifier_result") or {}).get("rewards"), "exception": result.get("exception_info")},
            "requests": {"count": len(completions), "rate_limit_errors": rate_limits, "api_errors": api_errors, "retry_markers": retry_markers, "cap_hits": cap_hits},
            "controls": {"anomalies": controls, "tools_per_request": sorted(tool_counts), "tool_schema_hash": sorted(tool_hashes), "uses_responses_api": completed.get("uses_responses_api")},
            "reasoning": {"emitted_items": emitted_reasoning, "eligible_items": replay_eligible, "exactly_replayed_items": replayed, "anomalies": replay_anomalies, "note": "zero emitted items is valid lifecycle evidence but does not newly exercise replay"},
            "events": {"counts": dict(event_kinds), "actions": len(action_ids), "observations": len(observation_ids), "unclosed_action_ids": sorted(str(item) for item in action_ids - observation_ids), "activated_skills": sum(len(event.get("activated_skills") or []) for event in events), "compaction_events": sum(count for kind, count in event_kinds.items() if "condens" in str(kind).lower())},
            "native_accounting": {"usage": dict(native_usage), "cost_usd": native_cost},
            "response_ids": response_ids,
        }

    provider_path = run_dir / "provider-generations.json"
    provider_errors: list[str] = []
    if args.fetch_provider:
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise RuntimeError("OPENROUTER_API_KEY is required")
        records = []
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(fetch_generation, item, key): item for item in sorted(set(all_generation_ids))}
            for future in as_completed(futures):
                try:
                    records.append(future.result())
                except Exception as exc:
                    provider_errors.append(f"{futures[future]}: {type(exc).__name__}: {exc}")
        records.sort(key=lambda item: str(item.get("id")))
        provider_path.write_text(json.dumps(records, indent=2, sort_keys=True) + "\n")
    else:
        records = json.loads(provider_path.read_text())

    records_by_id = {item.get("id"): item for item in records}
    for logical, arm in arms.items():
        if "response_ids" not in arm:
            continue
        ids = [gen_id(item) for item in arm.pop("response_ids")]
        selected = [records_by_id[item] for item in ids if item in records_by_id]
        expected_provider = PROVIDER_NAMES[logical]
        expected_model = PROVIDER_MODELS[logical]
        route_errors = [
            item.get("id") for item in selected
            if item.get("provider_name") != expected_provider
            or item.get("model") != expected_model
            or item.get("cancelled") is True
        ]
        missing = sorted(set(ids) - set(records_by_id))
        arm["provider"] = {
            "expected_name": expected_provider,
            "generation_count": len(selected),
            "missing_ids": missing,
            "routing_errors": route_errors,
            "models": dict(Counter(item.get("model") for item in selected)),
            "providers": dict(Counter(item.get("provider_name") for item in selected)),
            "tokens": {field: sum(int(item.get(field) or 0) for item in selected) for field in ("native_tokens_prompt", "native_tokens_cached", "native_tokens_completion", "native_tokens_reasoning")},
            "cost_usd": sum(float(item.get("total_cost") or 0) for item in selected),
        }
        arm["hard_failures"]["provider_evidence"] = bool(missing or route_errors or provider_errors or len(selected) != arm["requests"]["count"])
        arm["decision"] = "accept_TUA_lifecycle_qualification" if not any(arm["hard_failures"].values()) else "reject"

    accepted = all(arm.get("decision") == "accept_TUA_lifecycle_qualification" for arm in arms.values())
    audit = {
        "campaign_id": manifest["campaign_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "audited_at_utc": datetime.now(timezone.utc).isoformat(),
        "decision": "accept_all_five_as_exact_TUA_lifecycle_qualification" if accepted else "reject_one_or_more_arms",
        "claim_limit": "qualification pilot only; reward excluded from formal results; no multimodal/compaction/output-cap/concurrency-above-five claim",
        "run_state": run_state,
        "provider_fetch_errors": provider_errors,
        "provider_generation_count": len(records),
        "provider_total_cost_usd": sum(float(item.get("total_cost") or 0) for item in records),
        "shared_key_delta_note": "not attributable because other campaigns shared the key",
        "arms": arms,
    }
    (run_dir / "audit-final.json").write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"decision": audit["decision"], "generations": len(records), "cost_usd": audit["provider_total_cost_usd"], "arms": {key: value.get("decision") for key, value in arms.items()}}, indent=2, sort_keys=True))
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
