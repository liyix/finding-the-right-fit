#!/usr/bin/env python3
"""Claude Code 2.1.251 uniform-64K Harbor qualification pilot.

This thin entry point reuses the already-audited Harbor/Claude Code runner and
transparent Anthropic Messages provider injector.  It changes only the frozen
campaign matrix and the registered Claude Code study output control.  It is a
pilot for compatibility evidence and must not enter primary benchmark scores.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import tb4_claude_code_glm as base


ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN_ID = "pilot-tb4-claude-code-64k-readiness-20260831-r1"
PREPARED_AT_UTC = "2026-08-31T15:00:00Z"
RUN_DIR = ROOT / "runs" / CAMPAIGN_ID
HARBOR_JOB_NAME = "terminal-bench-4--claude-code--64k-readiness--rep-1"
HARBOR_JOBS_DIR = RUN_DIR / "harbor"
HARBOR_JOB_DIR = HARBOR_JOBS_DIR / HARBOR_JOB_NAME
TASKS = ("html-js-filter",)
OUTPUT_TOKENS = 64_000


MODELS: dict[str, dict[str, Any]] = {
    name: copy.deepcopy(base.MODELS[name])
    for name in ("gpt-6-astra", "glm-5.3", "kimi-k3")
}
MODELS["deepseek-v4-pro"] = {
    "request_model": "deepseek/deepseek-v4-pro-0813",
    "cli_model": "deepseek/deepseek-v4-pro-0813",
    "actual_model": "deepseek/deepseek-v4-pro-20260813",
    "provider": "DeepSeek",
    "provider_tag": "deepseek",
    "quantization": "unknown",
    "context_length": 1_048_576,
    "max_completion_tokens": 384_000,
    "route": {
        "only": ["deepseek"],
        "allow_fallbacks": False,
        "require_parameters": False,
    },
}

# Every arm is a different first-party provider lane.  Harbor may run the four
# lanes together, while each provider/model lane remains at concurrency one.
N_CONCURRENT = len(MODELS)
PER_MODEL_CONCURRENCY = 1


# Freeze the imported runner's globals before any config is expanded.  Runtime
# behavior remains in the canonical runner; this module is only a matrix/control
# specification.
base.CAMPAIGN_ID = CAMPAIGN_ID
base.PREPARED_AT_UTC = PREPARED_AT_UTC
base.RUN_DIR = RUN_DIR
base.HARBOR_JOB_NAME = HARBOR_JOB_NAME
base.HARBOR_JOBS_DIR = HARBOR_JOBS_DIR
base.HARBOR_JOB_DIR = HARBOR_JOB_DIR
base.TASKS = TASKS
base.MODELS = MODELS
base.N_CONCURRENT = N_CONCURRENT
base.PER_MODEL_CONCURRENCY = PER_MODEL_CONCURRENCY

_base_agent_config = base.agent_config
_base_build_manifest = base.build_manifest


def agent_config(logical_model: str) -> dict[str, Any]:
    """Add the one registered Claude Code output fairness control."""
    config = _base_agent_config(logical_model)
    config["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(OUTPUT_TOKENS)
    return config


base.agent_config = agent_config


def build_manifest() -> dict[str, Any]:
    manifest = _base_build_manifest()
    manifest["campaign_id"] = CAMPAIGN_ID
    manifest["prepared_at_utc"] = PREPARED_AT_UTC
    manifest["scope"].update(
        {
            "tasks": [base.load_task_metadata(name) for name in TASKS],
            "planned_trials": len(TASKS) * len(MODELS),
            "concurrency": {
                "global": N_CONCURRENT,
                "per_model": PER_MODEL_CONCURRENCY,
                "per_provider": 1,
                "policy": (
                    "four distinct first-party provider lanes may run concurrently; "
                    "each provider/model lane has exactly one trial"
                ),
            },
        }
    )
    manifest["harness"]["entry"] = (
        "claude --verbose --output-format=stream-json --permission-mode="
        "bypassPermissions --effort high --print; Harbor supplies the frozen model"
    )
    for name, path in manifest["model_paths"].items():
        path["terminal_loop_evidence"] = (
            "GPT/GLM/Kimi passed exact 1M/64K/high terminal loops; DeepSeek passed "
            "1M/32K/high and this campaign is its first exact 64K wire/Harbor qualification"
            if name == "deepseek-v4-pro"
            else "passed the exact 1M/64K/high route and terminal tool loop"
        )
        path["qualification_target"] = (
            "prove max_tokens=64000 on wire, strict actual route, real Harbor lifecycle, "
            "tool/thinking replay, accounting, and trajectory completeness"
        )
    manifest["controls"].update(
        {
            "reasoning_effort": (
                "high via Harbor reasoning_effort=high, Claude Code --effort high, "
                "CLAUDE_CODE_EFFORT_LEVEL=high, and wire output_config.effort audit"
            ),
            "thinking_budget": (
                "Claude Code/provider adaptive default; MAX_THINKING_TOKENS unset; "
                "reasoning token use remains a measured outcome"
            ),
            "context_window": (
                "1,000,000 study control via CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000; "
                "all four IDs remain their literal third-party slugs with no [1m] suffix "
                "or recognized-model spoof"
            ),
            "compaction": (
                "CLAUDE_CODE_AUTO_COMPACT_WINDOW=1000000; native policy retained. This "
                "single-task benchmark pilot does not replace the separately required "
                "representative compaction-recovery qualification for unknown model IDs."
            ),
            "output_budget": (
                "CLAUDE_CODE_MAX_OUTPUT_TOKENS=64000 for every arm; registered within-"
                "Claude-Code study normalization matching the recognized Opus 5 wire limit, "
                "not an endpoint-advertised maximum. Exact wire max_tokens must be audited."
            ),
            "sampling": {
                "temperature": "omitted by Claude Code; provider default",
                "top_p": "omitted by Claude Code; provider default",
                "seed": None,
            },
            "subagents": (
                "Claude Code default availability retained; main model, Sonnet/Opus/Haiku "
                "aliases, and CLAUDE_CODE_SUBAGENT_MODEL all use the same literal model slug"
            ),
        }
    )
    manifest["accounting"]["estimated_provider_cost_usd"] = (
        "$5-$25 total; task/model behavior and shared-key load create wide uncertainty"
    )
    manifest["launch_gate"].update(
        {
            "condition": (
                "explicit approval of this exact manifest hash; source/hash/key/ports "
                "rechecked immediately before launch"
            ),
            "reason": (
                "qualify the four third-party Claude Code paths under the newly frozen "
                "uniform-64K study control"
            ),
            "known_overlapping_campaigns_at_freeze": [],
            "interpretation_limit": (
                "one text-only task per path qualifies only the observed Harbor lifecycle; "
                "it does not qualify multimodality, formal concurrency, model quality, or "
                "the separately required forced compaction-recovery path"
            ),
        }
    )
    manifest["post_run_audit"].update(
        {
            "scope": "all four pilot trajectories",
            "checks": [
                "wire max_tokens=64000 and output_config.effort=high for every request",
                "actual model/provider, strict no-fallback route, request/retry/429 count",
                "default tool catalog plus complete tool-result and thinking replay",
                "unexpected project skills/MCP/prompt injection or cross-model subagents",
                "output-cap, compaction, timeout, verifier, artifact, and failure class",
                "provider versus Claude Code versus Harbor token/cost disagreement",
            ],
        }
    )
    manifest["source_sha256"] = {
        "pilot/tb4_claude_code_64k_readiness.py": base.sha256_file(Path(__file__)),
        "pilot/tb4_claude_code_glm.py": base.sha256_file(
            ROOT / "pilot" / "tb4_claude_code_glm.py"
        ),
        "pilot/claude_openrouter_pin_smoke.py": base.sha256_file(
            ROOT / "pilot" / "claude_openrouter_pin_smoke.py"
        ),
        "pilot/provider_smoke.py": base.sha256_file(
            ROOT / "pilot" / "provider_smoke.py"
        ),
    }
    manifest.pop("manifest_sha256", None)
    manifest["manifest_sha256"] = hashlib.sha256(
        base.canonical_json(manifest)
    ).hexdigest()
    return manifest


base.build_manifest = build_manifest


def main() -> int:
    return base.main()


if __name__ == "__main__":
    raise SystemExit(main())
