#!/usr/bin/env python3
"""Final paid-readiness qualification for Claude Code 2.1.251.

One immutable campaign covers the remaining Claude Code evidence gaps without
changing the formal 1M context policy:

* deterministic near-window histories exercise native compaction/recovery for
  every third-party study model;
* one image/tool canary covers the three currently image-capable routes;
* one subagent burst checks the transparent provider request gate; and
* one real TB4 task rechecks the recognized Claude path with the final config.

Harbor's built-in Claude Code agent remains the harness integration.  The only
compatibility layer is the already-audited Anthropic Messages body injector.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import claude_openrouter_pin_smoke as injector
import provider_smoke as smoke
import tb4_claude_code_glm as base


ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN_ID = "pilot-claude-code-final-readiness-20260901-r1"
PREPARED_AT_UTC = "2026-09-01T03:00:00Z"
RUN_DIR = ROOT / "runs" / CAMPAIGN_ID
JOBS_DIR = RUN_DIR / "harbor"
HARBOR_VERSION = "0.22.0"
CLAUDE_CODE_VERSION = "2.1.251"
TB4_COMMIT = "452bf305c6daa62fc59061d22133a7cbc7c1572e"
TB4_TASK = "bun-sourcemap-leak"
TB4_TASK_CHECKSUM = "fb420f8f5cf1222a1644119305f67e5e3478bcc3745011904168ad447b765120"
CONTEXT_TOKENS = 1_000_000
AUTO_COMPACT_TOKENS = 1_000_000
OUTPUT_TOKENS = 64_000
HISTORY_TOKEN_UNITS = 940_000
REQUEST_GATE_MAX_IN_FLIGHT = 2
GLOBAL_CONCURRENCY = 4
PER_MODEL_CONCURRENCY = 1
WHOLE_RUN_TIMEOUT_SECONDS = 24 * 60 * 60
UPSTREAM_URL = "https://openrouter.ai/api"
INJECTOR_BASE_PORT = 4210
ENVIRONMENT_IMAGE = (
    "harborframework/terminal-bench:html-js-filter-environment-84a8dc9541acfe09"
    "@sha256:72aefd5ceb952e93e1fbc6e14787b9b91975adc218912b73ab3fb86e335fdcbf"
)
EXPECTED_COST_USD = "$8-$30 total; long-context compaction output is uncertain"


MODELS: dict[str, dict[str, Any]] = {
    name: copy.deepcopy(base.MODELS[name])
    for name in ("claude-opus-5", "gpt-6-astra", "glm-5.3", "kimi-k3")
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

UNKNOWN_MODELS = ("gpt-6-astra", "glm-5.3", "kimi-k3", "deepseek-v4-pro")
IMAGE_MODELS = ("claude-opus-5", "gpt-6-astra", "kimi-k3")

COMPACTION_INSTRUCTION = """This is a qualification-only continuation with a deterministic long prior history.
Continue the existing session. Use the Bash tool to write exactly
COMPACTION_RECOVERED to /app/qualification-result.txt, read the file back with
Bash, then briefly confirm completion. Do not use subagents.
"""

IMAGE_INSTRUCTION = """This is a qualification-only image/tool canary. Use Bash to create
/app/canary.png by base64-decoding this PNG payload:
iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=
Then use the Read tool on /app/canary.png. After Read returns, use Bash to write
exactly IMAGE_BLOCK_RECOVERED to /app/qualification-result.txt. Do not use
subagents. The required evidence is an image content block plus a recovered
tool loop, not a color answer.
"""

BURST_INSTRUCTION = """This is a qualification-only request-pacing canary. In one parallel
tool turn, launch exactly four built-in Agent subagents using the same model.
Each subagent should return one distinct word only: alpha, beta, gamma, or
delta. After all four results return, use Bash to write exactly
SUBAGENT_BURST_RECOVERED to /app/qualification-result.txt. Do not launch more
than four subagents.
"""


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    path.chmod(mode)


def injector_port(logical_model: str) -> int:
    return INJECTOR_BASE_PORT + list(MODELS).index(logical_model)


def container_injector_url(logical_model: str) -> str:
    return f"http://host.docker.internal:{injector_port(logical_model)}"


def compose_overlay() -> dict[str, Any]:
    return {
        "services": {"main": {"extra_hosts": ["host.docker.internal:host-gateway"]}}
    }


def injector_spec(logical_model: str) -> dict[str, Any]:
    model = MODELS[logical_model]
    spec = {
        "cell_id": f"{CAMPAIGN_ID}--{logical_model}",
        "channel": "openrouter",
        "harness": "claude-code",
        "harness_version": CLAUDE_CODE_VERSION,
        "logical_model": logical_model,
        "actual_model": model["request_model"],
        "provider": "openrouter",
        "key_env": "OPENROUTER_API_KEY",
        "protocol": "anthropic-messages",
        "translated": False,
        "translation": None,
        "proxy_required": False,
        "body_injector_required": True,
        "compatibility_variant": "transparent-provider-body-injector-with-request-gate",
        "compatibility_drop_params": [],
        "base_url": UPSTREAM_URL,
        "anthropic_base_url_override": container_injector_url(logical_model),
        "openrouter_route": model["route"],
    }
    injector.validate_spec(spec)
    return spec


def claude_settings() -> dict[str, Any]:
    # These controls intentionally live in Claude's official settings env layer.
    # Harbor 0.22 treats values of any *TOKEN* environment key as secrets and
    # otherwise replaces the literal numbers inside downloaded JSON telemetry.
    return {
        "env": {
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS": str(CONTEXT_TOKENS),
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(AUTO_COMPACT_TOKENS),
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(OUTPUT_TOKENS),
        }
    }


def agent_config(
    logical_model: str, *, load_trajectory: Path | None = None
) -> dict[str, Any]:
    model = MODELS[logical_model]
    alias = model["cli_model"]
    config: dict[str, Any] = {
        "name": "claude-code",
        "model_name": alias,
        "n_concurrent": PER_MODEL_CONCURRENCY,
        "skills": [],
        "resume_trajectory": False,
        "extra_allowed_hosts": ["host.docker.internal"],
        "include_logs": ["**/*"],
        "kwargs": {
            "version": CLAUDE_CODE_VERSION,
            "reasoning_effort": "high",
            "config": claude_settings(),
        },
        "env": {
            "ANTHROPIC_AUTH_TOKEN": "${OPENROUTER_API_KEY}",
            "ANTHROPIC_BASE_URL": container_injector_url(logical_model),
            "ANTHROPIC_CUSTOM_HEADERS": "X-OpenRouter-Metadata: enabled",
            "ANTHROPIC_MODEL": alias,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": alias,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": alias,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": alias,
            "CLAUDE_CODE_SUBAGENT_MODEL": alias,
            "CLAUDE_CODE_EFFORT_LEVEL": "high",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        },
        "mcp_servers": [],
    }
    if load_trajectory is not None:
        config["load_trajectory"] = str(load_trajectory)
    return config


def deterministic_history_text() -> str:
    return " x" * HISTORY_TOKEN_UNITS


def atif_history(logical_model: str) -> dict[str, Any]:
    model = MODELS[logical_model]
    return {
        "schema_version": "ATIF-v1.7",
        "session_id": str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"{CAMPAIGN_ID}/{logical_model}/history")
        ),
        "agent": {
            "name": "claude-code",
            "version": CLAUDE_CODE_VERSION,
            "model_name": model["cli_model"],
        },
        "steps": [
            {
                "step_id": 1,
                "source": "user",
                "message": deterministic_history_text(),
            },
            {
                "step_id": 2,
                "source": "agent",
                "model_name": model["cli_model"],
                "message": "Acknowledged deterministic qualification payload.",
            },
        ],
    }


def task_toml(task_name: str, *, timeout_sec: int) -> str:
    return f'''schema_version = "1.0"

[task]
name = "qualification/{task_name}"
description = "Claude Code transport/readiness qualification; never a benchmark score"

[metadata]
category = "Infrastructure"
tags = ["qualification-only"]

[agent]
timeout_sec = {float(timeout_sec)}

[verifier]
timeout_sec = 120.0

[environment]
build_timeout_sec = 600.0
cpus = 2
memory_mb = 8192
storage_mb = 10240
gpus = 0
docker_image = "{ENVIRONMENT_IMAGE}"
'''


def verifier_script(marker: str) -> str:
    return f'''#!/bin/sh
set -eu
mkdir -p /logs/verifier
echo 0 > /logs/verifier/reward.txt
if [ -f /app/qualification-result.txt ] && [ "$(cat /app/qualification-result.txt)" = "{marker}" ]; then
  echo 1 > /logs/verifier/reward.txt
fi
'''


def materialize_qualification_dataset(path: Path) -> None:
    tasks = {
        "compaction-recovery": (COMPACTION_INSTRUCTION, "COMPACTION_RECOVERED", 7200),
        "image-tool-canary": (IMAGE_INSTRUCTION, "IMAGE_BLOCK_RECOVERED", 1800),
        "subagent-burst-canary": (
            BURST_INSTRUCTION,
            "SUBAGENT_BURST_RECOVERED",
            1800,
        ),
    }
    for name, (instruction, marker, timeout) in tasks.items():
        task_dir = path / name
        tests_dir = task_dir / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "instruction.md").write_text(instruction)
        (task_dir / "task.toml").write_text(task_toml(name, timeout_sec=timeout))
        test_path = tests_dir / "test.sh"
        test_path.write_text(verifier_script(marker))
        test_path.chmod(0o755)


def materialize_histories(path: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for logical_model in UNKNOWN_MODELS:
        target = path / f"{logical_model}.atif.json"
        write_json(target, atif_history(logical_model), mode=0o444)
        result[logical_model] = target
    return result


def wave_configs(
    *, overlay_path: Path, dataset_path: Path, history_paths: dict[str, Path]
) -> list[tuple[str, dict[str, Any]]]:
    common = {
        "jobs_dir": str(JOBS_DIR),
        "n_attempts": 1,
        "install_only": False,
        "timeout_multiplier": 1.0,
        "retry": {"max_retries": 0},
        "environment": {
            "type": "docker",
            "force_build": False,
            "delete": True,
            "cpu_enforcement_policy": "auto",
            "memory_enforcement_policy": "auto",
            "extra_docker_compose": [str(overlay_path)],
        },
        "verifier": {},
        "tasks": [],
        "artifacts": [],
        "extra_instruction_paths": [],
        "extra_instructions": [],
    }

    def make(
        name: str,
        models: tuple[str, ...],
        dataset: Path,
        task_name: str,
        *,
        load: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        config = copy.deepcopy(common)
        config.update(
            {
                "job_name": f"{CAMPAIGN_ID}--{name}",
                "n_concurrent_trials": min(GLOBAL_CONCURRENCY, len(models)),
                "agents": [
                    agent_config(
                        model,
                        load_trajectory=history_paths[model] if load else None,
                    )
                    for model in models
                ],
                "datasets": [{"path": str(dataset), "task_names": [task_name]}],
            }
        )
        return name, config

    return [
        make(
            "compaction",
            UNKNOWN_MODELS,
            dataset_path,
            "compaction-recovery",
            load=True,
        ),
        make("image", IMAGE_MODELS, dataset_path, "image-tool-canary"),
        make(
            "request-gate",
            ("gpt-6-astra",),
            dataset_path,
            "subagent-burst-canary",
        ),
        make(
            "claude-tb4",
            ("claude-opus-5",),
            ROOT / "vendor" / "terminal-bench",
            TB4_TASK,
        ),
    ]


def fixture_description() -> dict[str, Any]:
    history_digest = hashlib.sha256(deterministic_history_text().encode()).hexdigest()
    return {
        "compaction_history": {
            "format": "ATIF-v1.7 converted by Harbor to native Claude Code JSONL",
            "payload": "the two-character sequence ' x' repeated",
            "repetitions": HISTORY_TOKEN_UNITS,
            "utf8_bytes": HISTORY_TOKEN_UNITS * 2,
            "sha256": history_digest,
            "purpose": (
                "deterministically approach the unchanged 1M window; success requires a "
                "native compaction event, recovery, Bash tool loop, and verifier result"
            ),
        },
        "image_canary": {
            "models": list(IMAGE_MODELS),
            "expected": "Read produces an image block and the following Bash call succeeds",
            "excluded_text_only_routes": ["glm-5.3", "deepseek-v4-pro"],
        },
        "request_gate_canary": {
            "model": "gpt-6-astra",
            "prompted_parallel_subagents": 4,
            "max_provider_requests_in_flight": REQUEST_GATE_MAX_IN_FLIGHT,
        },
    }


def other_harbor_processes() -> list[str]:
    """Return other live Harbor launch commands without exposing environments."""
    completed = subprocess.run(
        ["ps", "-eo", "args="],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    return [
        line.strip()
        for line in completed.stdout.splitlines()
        if "harbor run" in line
        and CAMPAIGN_ID not in line
        and "ps -eo" not in line
    ]


def assert_injector_ports_available() -> None:
    sockets: list[socket.socket] = []
    try:
        for name in MODELS:
            handle = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            handle.bind(("0.0.0.0", injector_port(name)))
            sockets.append(handle)
    finally:
        for handle in sockets:
            handle.close()


def build_manifest() -> dict[str, Any]:
    overlay = RUN_DIR / "host-gateway.compose.json"
    dataset = RUN_DIR / "qualification-dataset"
    histories = {
        name: RUN_DIR / "histories" / f"{name}.atif.json" for name in UNKNOWN_MODELS
    }
    configs = {
        name: config
        for name, config in wave_configs(
            overlay_path=overlay, dataset_path=dataset, history_paths=histories
        )
    }
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "campaign_id": CAMPAIGN_ID,
        "kind": "paid-claude-code-final-readiness-pilot-not-for-primary-scores",
        "prepared_at_utc": PREPARED_AT_UTC,
        "approval": {
            "required_before_launch": True,
            "target": "campaign ID plus manifest SHA256",
            "launch_requires_exact_hash": True,
        },
        "scope": {
            "benchmark": "qualification fixtures plus one Terminal-Bench 4.0 task",
            "tb4_version": "v4.0.0",
            "tb4_commit": TB4_COMMIT,
            "tb4_task": TB4_TASK,
            "tb4_task_checksum": TB4_TASK_CHECKSUM,
            "replicate": 1,
            "planned_trials": 9,
            "waves": [
                {"name": "compaction", "trials": 4, "concurrency": 4},
                {"name": "image", "trials": 3, "concurrency": 3},
                {"name": "request-gate", "trials": 1, "concurrency": 1},
                {"name": "claude-tb4", "trials": 1, "concurrency": 1},
            ],
            "fixtures": fixture_description(),
            "quality_claim": (
                "readiness of the exact observed paths only; qualification prompts and seeded "
                "history are not benchmark scores and do not estimate model quality"
            ),
        },
        "runner": {
            "name": "Harbor",
            "version": HARBOR_VERSION,
            "commit": "4407eb5",
            "entry": (
                "python pilot/claude_code_final_readiness.py run "
                "--approved-manifest-sha256 <sha256>"
            ),
            "integration": "Harbor built-in claude-code agent; no custom agent adapter",
            "resolved_wave_configs": configs,
        },
        "repository": {
            "git_head": subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            ).stdout.strip(),
            "working_tree": (
                "shared dirty workspace; the source SHA256 map and immutable run manifest "
                "are authoritative for this campaign"
            ),
        },
        "harness": {
            "name": "Claude Code",
            "version": CLAUDE_CODE_VERSION,
            "install": f"official @anthropic-ai/claude-code@{CLAUDE_CODE_VERSION}",
            "entry": (
                "claude --verbose --output-format=stream-json --settings <Harbor-uploaded> "
                "--permission-mode=bypassPermissions --effort high --print"
            ),
        },
        "model_paths": {
            name: {
                "requested_model": model["request_model"],
                "claude_code_model": model["cli_model"],
                "expected_actual_model": model["actual_model"],
                "provider": model["provider"],
                "provider_endpoint_tag": model["provider_tag"],
                "quantization": model["quantization"],
                "protocol": "OpenRouter Anthropic Messages skin; no response translation",
                "route": model["route"],
            }
            for name, model in MODELS.items()
        },
        "controls": {
            "reasoning_effort": (
                "high via Harbor reasoning_effort=high, Claude --effort high, "
                "CLAUDE_CODE_EFFORT_LEVEL=high, and audited wire output_config.effort"
            ),
            "thinking_budget": "adaptive/default; MAX_THINKING_TOKENS unset",
            "context_window": (
                "1,000,000 for every arm; Claude retains its official [1m] selector and "
                "third-party IDs remain literal/unspoofed"
            ),
            "compaction": (
                "CLAUDE_CODE_AUTO_COMPACT_WINDOW=1000000; native policy unchanged; "
                "qualification seeds long history instead of lowering the window"
            ),
            "output_budget": (
                "CLAUDE_CODE_MAX_OUTPUT_TOKENS=64000 for every arm; exact wire max_tokens "
                "is mandatory evidence"
            ),
            "control_source": (
                "the three numeric Claude Code controls use official settings.json env; this "
                "avoids Harbor 0.22 secret scrubbing corrupting numeric JSON telemetry"
            ),
            "max_turns": "unset / Claude Code default",
            "temperature": "omitted / provider default",
            "top_p": "omitted / provider default",
            "seed": None,
            "tools": "Claude Code installed defaults",
            "skills": [],
            "mcp_servers": [],
            "subagents": (
                "default availability; main/default/subagent aliases all route to the same "
                "study model; explicitly exercised only by the qualification canary"
            ),
            "memory": "isolated per-trial CLAUDE_CONFIG_DIR; seeded only where disclosed",
            "timeouts": {
                "qualification_agent": "1800s image/gate; 7200s compaction",
                "tb4_agent": "task-native 28800s",
                "verifier": "120s qualification; task-native 1800s TB4",
                "runner_wall": WHOLE_RUN_TIMEOUT_SECONDS,
                "llm_stream": "Claude Code native default",
            },
            "retries": {
                "harbor_outer": 0,
                "injector": 0,
                "claude_code_native": "default; every upstream request recorded",
            },
            "concurrency": {
                "trial_global": GLOBAL_CONCURRENCY,
                "trial_per_model_provider": PER_MODEL_CONCURRENCY,
                "request_gate_per_provider": REQUEST_GATE_MAX_IN_FLIGHT,
                "request_gate_semantics": (
                    "queue only; no retry, timeout, body change, response change, or request drop"
                ),
            },
            "benchmark_specific_augmentation": False,
        },
        "sandbox": {
            "provider": "local Docker via Harbor",
            "qualification_image": ENVIRONMENT_IMAGE,
            "qualification_resources": {"cpus": 2, "memory_mb": 8192, "gpus": 0},
            "tb4_resources": base.load_task_metadata(TB4_TASK),
            "network": (
                "task-default outbound network; host.docker.internal only reaches the local "
                "transparent provider injector"
            ),
        },
        "compatibility_layer": {
            "request_change": "add strict top-level OpenRouter provider object",
            "request_gate_max_in_flight": REQUEST_GATE_MAX_IN_FLIGHT,
            "unchanged": [
                "model",
                "messages",
                "system",
                "tools",
                "thinking",
                "metadata",
                "headers",
                "response bytes",
                "SSE chunking",
            ],
            "retries": 0,
            "prompt_or_response_logging": False,
        },
        "accounting": {
            "primary": "OpenRouter per-generation token, cost, route, and quantization records",
            "secondary": [
                "Claude Code native usage/modelUsage",
                "Harbor source-qualified aggregation",
                "injector one record per HTTP attempt",
            ],
            "litellm": "not present",
            "double_count_rule": "never sum reports of the same call",
            "estimated_provider_cost_usd": EXPECTED_COST_USD,
            "hard_dollar_kill": None,
        },
        "launch_gate": {
            "condition": (
                "exact approved manifest hash, key present, ports free, and cached image present"
            ),
            "overlap_seen_at_freeze": "pilot-tb4-openhands-five-model-b-20260901-r1",
            "overlap_policy": (
                "user explicitly approved immediate launch with other Harbor campaigns using "
                "the shared OpenRouter key on 2026-09-01; within-campaign trial and request "
                "limits remain unchanged; latency and 429 evidence cannot qualify isolated "
                "formal concurrency"
            ),
            "approval_basis": (
                "direct user instruction: launch immediately because overlapping use is "
                "acceptable and large-scale campaigns must tolerate aggregate concurrency"
            ),
        },
        "qualification_criteria": {
            "compaction": (
                "each unknown-model trajectory has a structural native compaction marker, "
                "a post-compaction Messages request, recovered Bash call/result, marker file, "
                "and verifier result; no context/session termination"
            ),
            "image": (
                "image-capable routes show an image content block on wire and recover to the "
                "following Bash tool/result and verifier"
            ),
            "request_gate": (
                "provider injector high-water <=2, at least one queued request if the model "
                "obeys the parallel-subagent instruction, no injected retries, session recovers"
            ),
            "claude_tb4": (
                "exact final config completes a real TB4 lifecycle with strict Anthropic route"
            ),
            "all": (
                "wire max_tokens=64000 and effort=high; exact provider/permaslug; no fallback; "
                "complete native session and ATIF trajectory; all retries/429/costs classified"
            ),
        },
        "post_run_audit": {
            "required": True,
            "scope": "all nine trajectories plus every injector request",
            "decision": "accept, rerun, or exclude only after structural trajectory audit",
        },
        "source_sha256": {
            "pilot/claude_code_final_readiness.py": sha256_file(Path(__file__)),
            "pilot/tb4_claude_code_glm.py": sha256_file(
                ROOT / "pilot" / "tb4_claude_code_glm.py"
            ),
            "pilot/claude_openrouter_pin_smoke.py": sha256_file(
                ROOT / "pilot" / "claude_openrouter_pin_smoke.py"
            ),
            "pilot/provider_smoke.py": sha256_file(ROOT / "pilot" / "provider_smoke.py"),
        },
    }
    manifest["manifest_sha256"] = hashlib.sha256(canonical_json(manifest)).hexdigest()
    return manifest


def harbor_command(config_path: Path, *, print_config: bool = False) -> list[str]:
    command = [
        "uvx",
        "--from",
        f"harbor=={HARBOR_VERSION}",
        "harbor",
        "run",
        "--config",
        str(config_path),
    ]
    command.append("--print-config" if print_config else "--yes")
    return command


def tool_env(api_key: str | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["UV_CACHE_DIR"] = base.UV_CACHE_DIR
    env["UV_TOOL_DIR"] = base.UV_TOOL_DIR
    if api_key is not None:
        env["OPENROUTER_API_KEY"] = api_key
    return env


def validate_atif_with_harbor(path: Path) -> None:
    harbor_python = Path(os.environ.get("HARBOR_PYTHON", sys.executable))
    code = (
        "import json,sys; "
        "from harbor.models.trajectories import Trajectory; "
        "from harbor.agents.installed.claude_code import ClaudeCode; "
        "from pathlib import Path; "
        "p=Path(sys.argv[1]); t=Trajectory.model_validate(json.loads(p.read_text())); "
        "a=ClaudeCode(logs_dir=Path(sys.argv[2]), model_name=t.agent.model_name, "
        "version='2.1.251'); n,c=a.atif_to_native_trajectory(t,t.session_id); "
        "assert n.endswith('.jsonl') and len(c)>1800000"
    )
    subprocess.run(
        [str(harbor_python), "-c", code, str(path), str(path.parent / "logs")],
        check=True,
        cwd=ROOT,
        timeout=60,
    )


def zero_cost_preflight() -> int:
    manifest = build_manifest()
    assert_injector_ports_available()
    if not smoke.load_env_value(ROOT / ".env", "OPENROUTER_API_KEY"):
        raise RuntimeError("OPENROUTER_API_KEY is missing from .env")
    image = subprocess.run(
        ["docker", "image", "inspect", ENVIRONMENT_IMAGE, "--format", "{{.Id}}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if image.returncode != 0 or not image.stdout.strip().startswith("sha256:"):
        raise RuntimeError("required pinned qualification Docker image is not cached")
    injector.offline_self_test()
    with tempfile.TemporaryDirectory(prefix="cc-final-readiness-", dir="/tmp") as raw:
        directory = Path(raw)
        dataset = directory / "dataset"
        histories = materialize_histories(directory / "histories")
        materialize_qualification_dataset(dataset)
        overlay = directory / "host-gateway.compose.json"
        write_json(overlay, compose_overlay())
        for path in histories.values():
            validate_atif_with_harbor(path)
        for wave_name, config in wave_configs(
            overlay_path=overlay, dataset_path=dataset, history_paths=histories
        ):
            config_path = directory / f"{wave_name}.json"
            write_json(config_path, config)
            completed = subprocess.run(
                harbor_command(config_path, print_config=True),
                cwd=ROOT,
                env=tool_env("preflight-placeholder"),
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if completed.returncode != 0:
                print(completed.stdout[-4000:] + completed.stderr[-4000:], file=sys.stderr)
                return 2
            resolved = json.loads(completed.stdout)
            agents = resolved.get("agents") or []
            if not agents:
                raise RuntimeError(f"Harbor lost agents for wave {wave_name}")
            for agent in agents:
                settings = ((agent.get("kwargs") or {}).get("config") or {}).get("env")
                if settings != claude_settings()["env"]:
                    raise RuntimeError(f"Harbor changed Claude settings in {wave_name}")
                leaked = set(agent.get("env") or {}) & set(claude_settings()["env"])
                if leaked:
                    raise RuntimeError(
                        f"numeric token controls leaked into scrubbed env in {wave_name}: {leaked}"
                    )
                if (agent.get("kwargs") or {}).get("reasoning_effort") != "high":
                    raise RuntimeError(f"Harbor changed reasoning effort in {wave_name}")
        if len(deterministic_history_text().encode()) != HISTORY_TOKEN_UNITS * 2:
            raise RuntimeError("deterministic history byte count changed")
    print("Zero-cost preflight passed: ATIF conversion, four Harbor configs, settings parity,")
    print("injector request gate, strict routes, and telemetry-scrub avoidance")
    overlaps = other_harbor_processes()
    print(f"Other live Harbor launch commands at preflight: {len(overlaps)}")
    if overlaps:
        print("Shared-key overlap is explicitly approved; launch will not wait")
    print(f"Prospective manifest SHA256: {manifest['manifest_sha256']}")
    print("No model API call was made")
    return 0


def start_injector(logical_model: str, spec_path: Path) -> tuple[subprocess.Popen[str], Any]:
    process_log = (RUN_DIR / f"injector-{logical_model}.process.log").open(
        "w", encoding="utf-8"
    )
    process = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "pilot" / "claude_openrouter_pin_smoke.py"),
            "_proxy",
            "--spec",
            str(spec_path),
            "--host",
            "0.0.0.0",
            "--port",
            str(injector_port(logical_model)),
            "--upstream",
            UPSTREAM_URL,
            "--log-path",
            str(RUN_DIR / f"injector-{logical_model}.jsonl"),
            "--max-in-flight",
            str(REQUEST_GATE_MAX_IN_FLIGHT),
        ],
        cwd=ROOT,
        stdout=process_log,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        injector.wait_for_port(process, injector_port(logical_model))
    except Exception:
        stop_process(process)
        process_log.close()
        raise
    return process, process_log


def stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def run_approved(approved_hash: str) -> int:
    manifest = build_manifest()
    if approved_hash != manifest["manifest_sha256"]:
        print(f"ERROR approval hash mismatch; expected {manifest['manifest_sha256']}", file=sys.stderr)
        return 2
    if RUN_DIR.exists():
        print(f"ERROR immutable run directory already exists: {RUN_DIR}", file=sys.stderr)
        return 2
    overlaps = other_harbor_processes()
    assert_injector_ports_available()
    api_key = smoke.load_env_value(ROOT / ".env", "OPENROUTER_API_KEY")
    if not api_key:
        print("ERROR OPENROUTER_API_KEY is missing from .env", file=sys.stderr)
        return 2

    RUN_DIR.mkdir(parents=True)
    dataset = RUN_DIR / "qualification-dataset"
    histories = materialize_histories(RUN_DIR / "histories")
    materialize_qualification_dataset(dataset)
    overlay = RUN_DIR / "host-gateway.compose.json"
    write_json(overlay, compose_overlay(), mode=0o444)
    write_json(RUN_DIR / "manifest.json", manifest, mode=0o444)
    write_json(
        RUN_DIR / "approval.json",
        {
            "campaign_id": CAMPAIGN_ID,
            "approved_manifest_sha256": approved_hash,
            "recorded_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "shared_key_overlap_explicitly_approved": True,
            "other_harbor_launch_command_count_at_start": len(overlaps),
        },
        mode=0o444,
    )
    specs: dict[str, Path] = {}
    for name in MODELS:
        path = RUN_DIR / f"injector-spec-{name}.json"
        write_json(path, injector_spec(name), mode=0o444)
        specs[name] = path

    configs = wave_configs(
        overlay_path=overlay, dataset_path=dataset, history_paths=histories
    )
    config_paths: list[tuple[str, Path]] = []
    for wave_name, config in configs:
        path = RUN_DIR / f"harbor-{wave_name}.json"
        write_json(path, config, mode=0o444)
        config_paths.append((wave_name, path))

    processes: list[tuple[subprocess.Popen[str], Any]] = []
    return_codes: dict[str, int] = {}
    started = time.monotonic()
    try:
        for name, path in specs.items():
            processes.append(start_injector(name, path))
        for wave_name, config_path in config_paths:
            with (RUN_DIR / f"harbor-{wave_name}.console.log").open("w") as log:
                completed = subprocess.run(
                    harbor_command(config_path),
                    cwd=ROOT,
                    env=tool_env(api_key),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=WHOLE_RUN_TIMEOUT_SECONDS,
                    check=False,
                )
            return_codes[wave_name] = completed.returncode
            # Do not silently continue after a broken lifecycle; later waves have
            # different claims and can be launched only if prior infrastructure ran.
            if completed.returncode not in {0, 1}:
                break
    except subprocess.TimeoutExpired:
        return_codes["runner"] = 124
    finally:
        for process, process_log in reversed(processes):
            stop_process(process)
            process_log.close()

    write_json(
        RUN_DIR / "run-summary.json",
        {
            "campaign_id": CAMPAIGN_ID,
            "manifest_sha256": approved_hash,
            "wave_return_codes": return_codes,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "audit_status": "mandatory structural trajectory audit pending",
        },
        mode=0o444,
    )
    print(json.dumps(return_codes, sort_keys=True))
    print(f"Raw run: {RUN_DIR}")
    print("Mandatory trajectory/log audit is required before readiness claims change")
    return 0 if len(return_codes) == len(config_paths) else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("manifest")
    commands.add_parser("preflight")
    run = commands.add_parser("run")
    run.add_argument("--approved-manifest-sha256", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "manifest":
        print(json.dumps(build_manifest(), indent=2, ensure_ascii=False))
        return 0
    if args.command == "preflight":
        return zero_cost_preflight()
    if args.command == "run":
        return run_approved(args.approved_manifest_sha256)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
