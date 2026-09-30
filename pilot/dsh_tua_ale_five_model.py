#!/usr/bin/env python3
"""One frozen ALE-CLI qualification campaign for DSH across five models.

The five independent model lanes run concurrently on the same local-Docker ALE
task.  This is a lifecycle/trajectory qualification pilot, never a primary
benchmark score.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pilot"))

import tb4_dsh_glm as provider_base
from integrations.ale import ALE_AGENT_CLASSES, ALE_DOCKER_IMAGE, prepare_agent_source
from integrations.ale_agents.study_deepseek.config import (
    DSH_MODEL_SPECS,
)


CAMPAIGN_ID = "pilot-ale-dsh-five-model-qualification-20260902-r7"
RUN_DIR = ROOT / "runs" / CAMPAIGN_ID
ALE_ROOT = ROOT / "vendor" / "agents-last-exam"
ALE_TASK = "computing_math/os_log_permission_guard_v1"
DSH_VERSION = "0.1.1-rc.2"
LANE_CONCURRENCY = 5

MODELS = {
    "claude-opus-5": "anthropic/claude-opus-5",
    "gpt-6-astra": "openai/gpt-6-astra",
    "glm-5.3": "z-ai/glm-5.3",
    "kimi-k3": "moonshotai/kimi-k3",
    "deepseek-v4-pro": "deepseek/deepseek-v4-pro-0813",
}

EXPECTED_ENDPOINTS = {
    "claude-opus-5": ("anthropic/claude-opus-5-20260723", "Anthropic"),
    "gpt-6-astra": ("openai/gpt-6-astra-20260903", "OpenAI"),
    "glm-5.3": ("z-ai/glm-5.3-20260816", "Z.AI"),
    "kimi-k3": ("moonshotai/kimi-k3-20260715", "Moonshot AI"),
    "deepseek-v4-pro": ("deepseek/deepseek-v4-pro-20260813", "DeepSeek"),
}


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        digest.update(f"{sha256_file(path)}  {relative}\n".encode())
    return digest.hexdigest()


def write_json(path: Path, value: Any, *, mode: int = 0o444) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    path.chmod(mode)


def write_yaml(path: Path, value: Any, *, mode: int = 0o444) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    path.chmod(mode)


def ale_agent(logical: str) -> dict[str, Any]:
    return {
        "class": ALE_AGENT_CLASSES["deepseek-harness"][1],
        "id": f"deepseek-harness--{logical}",
        "model": MODELS[logical],
        "executor": "sandbox",
        "config": {
            "provider": "openrouter",
            "api_key": None,
            "cli_version": DSH_VERSION,
            "reasoning_effort": "high",
            "permission_mode": "danger-full-access",
            "base_url": "http://127.0.0.1:4010/v1",
            "injector_upstream": "https://openrouter.ai/api",
            "injector_port": 4010,
            "mcp_policy": "ale-cua-only",
        },
    }


def ale_environment() -> dict[str, Any]:
    return {
        "snapshots": {
            "cpu-free-ubuntu": {
                "provider": "docker",
                "image": "ale-ubuntu22-docker",
                "docker": {
                    "image_ref": ALE_DOCKER_IMAGE,
                    "shm_size": "2g",
                    "resolution": [1024, 768],
                    "privileged": False,
                    "enable_dind": False,
                },
            }
        },
        "task_data_source": f"local:{ALE_ROOT / 'task-data'}",
        "output_path": "local",
    }


def ale_experiment(logical: str) -> dict[str, Any]:
    configs = RUN_DIR / "configs" / "ale"
    return {
        "name": f"{CAMPAIGN_ID}--{logical}",
        "agent": str((configs / "agents" / f"{logical}.yaml").resolve()),
        "environment": str((configs / "environment.yaml").resolve()),
        "tasks": str((configs / "tasks.txt").resolve()),
        "output": {"root": str((RUN_DIR / "raw" / "ale" / logical).resolve())},
        "concurrency": 1,
        "wall_time_s": None,
        "auto_resume": False,
        "max_attempts": 1,
        "cleanup_mode": "delete",
        "prompt_suffix": "",
    }


def relevant_source_hashes() -> dict[str, str]:
    paths = [
        Path(__file__).resolve(),
        ROOT / "pyproject.toml",
        ROOT / "uv.lock",
        ROOT / "integrations" / "openrouter_body_injector.mjs",
        ROOT / "integrations" / "patches" / "dsh-0.1.1-rc.2-headless-standard.patch",
        ROOT / "integrations" / "ale_agents" / "study_deepseek" / "config.py",
        ROOT / "integrations" / "ale_agents" / "study_deepseek" / "deployer.py",
    ]
    return {str(path.relative_to(ROOT)): sha256_file(path) for path in paths}


def materialization_source_hashes() -> dict[str, str]:
    """Record generators without making later unrelated edits runtime blockers."""
    paths = [ROOT / "experiment.yaml", ROOT / "integrations" / "ale.py"]
    return {str(path.relative_to(ROOT)): sha256_file(path) for path in paths}


def manifest_body(source_manifest: dict[str, Any]) -> dict[str, Any]:
    ale_card = json.loads(
        (ALE_ROOT / "tasks" / ALE_TASK / "task_card.json").read_text()
    )
    return {
        "schema_version": 1,
        "campaign_id": CAMPAIGN_ID,
        "kind": "paid-ALE-lifecycle-qualification-not-primary-scores",
        "approval": {
            "required_before_launch": True,
            "approved_manifest_sha256": None,
        },
        "scope": {
            "planned_trials": 5,
            "replicate": 1,
            "primary_scores": False,
            "schedule": "five independent ALE model lanes concurrent",
            "maximum_active_trials": 5,
            "maximum_active_trials_per_model_provider": 1,
            "ale": {
                "benchmark": "Agents' Last Exam / ALE-CLI",
                "commit": "0b6465b13c85b5a0a017d4c88bcf979a519a5e1f",
                "task": ALE_TASK,
                "task_title": ale_card["title"],
                "task_card_sha256": sha256_file(
                    ALE_ROOT / "tasks" / ALE_TASK / "task_card.json"
                ),
                "task_timeout_seconds": int(ale_card["vm"]["timeout"]),
                "local_docker_subset": "registered 99/105 development subset",
                "variant": "base",
                "task_data_variant_sha256": tree_sha256(
                    ALE_ROOT / "task-data" / ALE_TASK / "base"
                ),
            },
        },
        "runner": {
                "name": "ALE official",
                "commit": "0b6465b13c85b5a0a017d4c88bcf979a519a5e1f",
                "derived_source": source_manifest,
                "auto_resume": False,
                "max_attempts": 1,
                "cleanup_mode": "delete",
        },
        "harness": {
            "name": "DeepSeek Harness",
            "version": DSH_VERSION,
            "upstream_commit": "b150a551b8d465e31e418e1b2eaf5e79bbb7d28e",
            "entry": "dsh --profile headless --patch <resolved> <task>",
            "profile": "headless one-shot launcher",
            "agent_preset": "unmodified upstream standard preset",
            "standard_patch_sha256": sha256_file(
                ROOT / "integrations/patches/dsh-0.1.1-rc.2-headless-standard.patch"
            ),
            "provider_injector_sha256": sha256_file(
                ROOT / "integrations/openrouter_body_injector.mjs"
            ),
            "compatibility_changes": (
                "mount standard in a fresh headless session; add strict provider body; "
                "ALE additionally exposes only its runner-native CUA MCP; force "
                "chokidar polling because ALE containers share the host inotify quota"
            ),
        },
        "models": [
            {
                "logical": logical,
                "requested_model": model,
                "expected_actual_model": EXPECTED_ENDPOINTS[logical][0],
                "expected_provider": EXPECTED_ENDPOINTS[logical][1],
                "base_url": "https://openrouter.ai/api/v1",
                "protocol": "OpenAI Chat Completions with SSE; no translation",
                "route": DSH_MODEL_SPECS[model]["route"],
                "context_tokens": DSH_MODEL_SPECS[model]["context"],
                "max_output_tokens": DSH_MODEL_SPECS[model]["max_output"],
                "input_modalities": DSH_MODEL_SPECS[model]["input"],
                "cache_retention": DSH_MODEL_SPECS[model].get("cache_retention"),
            }
            for logical, model in MODELS.items()
        ],
        "controls": {
            "reasoning_effort": (
                "high via pi-ai provider reasoning=high and model reasoningEfforts mapping"
            ),
            "temperature_top_p_seed": "omitted; provider defaults",
            "max_turns": "standard preset native/unset",
            "compaction": "DSH standard native; every event is a primary outcome",
            "tools": "upstream standard catalog; ALE adds only benchmark-native CUA MCP",
            "skills": [],
            "extra_prompts": [],
            "memory": "fresh DSH_HOME/session for every trial",
            "subagents": "standard-native; no override",
            "benchmark_specific_augmentation": False,
            "dsh_permission": "danger-full-access inside the benchmark outer sandbox",
            "auxiliary_calls": "default title call retained and billed",
        },
        "timeouts_and_retries": {
            "ale_outer": "task-native 7200s; evaluate 7200s",
            "ale_dsh_llm": (
                "no explicit request-wall timeout; pi-ai native stream-idle timeout 300s; "
                "title call end-to-end timeout 60s"
            ),
            "dsh_tool_timeouts": (
                "bash foreground default 120s with model-selectable cap 600s; "
                "ALE CUA MCP call 60s; LSP 60s; remaining tools keep rc.2 defaults"
            ),
            "dsh_install": "npm install 1200s; each apt setup command 300s",
            "injector_startup": "20s; no request retry",
            "dsh_provider_retries": (
                "native normal policy, up to 5 with exponential backoff/jitter; audit actual"
            ),
            "ale_whole_trial_retries": 0,
            "ale_cua_command_attempts": 8,
            "ale_task_session_attempts": 4,
        },
        "sandbox": {
                "provider": "ALE DockerProvider",
                "image_ref": ALE_DOCKER_IMAGE,
                "image_manifest_digest": ALE_DOCKER_IMAGE.rsplit("@", 1)[1],
                "machine_type": ale_card["vm"]["machineType"],
                "cpus": 4,
                "memory_gb": 15,
                "shm": "2g",
                "resolution": [1024, 768],
                "privileged": False,
                "nested_runtime": False,
                "task_data_source": f"local:{ALE_ROOT / 'task-data'}",
                "task_data_variant_sha256": tree_sha256(
                    ALE_ROOT / "task-data" / ALE_TASK / "base"
                ),
                "network": "enabled for harness install and model API",
        },
        "accounting": {
            "primary": "OpenRouter settled per-generation route/token/cost metadata",
            "secondary": "DSH native usage plus ALE normalized view, never summed",
            "title_call": "provider total includes it; main DSH session does not",
            "litellm": "not present",
            "hard_dollar_stop": None,
            "estimated_total_usd": "$0.25-$10; Claude trajectory length dominates uncertainty",
            "runtime_secret_scope": (
                "only OPENROUTER_API_KEY is loaded by the launcher and ALE's fixed "
                "credential allowlist; HUGGING_FACE_TOKEN is not passed to the sandbox"
            ),
        },
        "qualification_limits": {
            "not_formal_scores": True,
            "ale_local_docker_is_not_full_105": True,
            "selected_task_is_text_instruction_with_runner-native_CUA": True,
            "does_not_qualify_broad_multimodal_model_input": True,
            "ale_cua_server_startup_is_required_but_tool_use_is_not_forced": True,
        },
        "required_post_run_audit": [
            "every actual model/provider/generation ID, quantization route and fallback",
            "standard preset/tool catalog plus tool-call/result closure and subagents",
            "reasoning continuity, cache write/read, compaction and output-cap events",
            "429/5xx, native retries, title calls, timeouts and verifier/grader validity",
            "native/runner/provider token-cost reconciliation without double counting",
            "no secret, skill, prompt, MCP (except ALE CUA), or modality contamination",
        ],
        "materialization_source_sha256": materialization_source_hashes(),
        "source_sha256": relevant_source_hashes(),
    }


def config_hashes() -> dict[str, str]:
    root = RUN_DIR / "configs"
    return {
        str(path.relative_to(RUN_DIR)): sha256_file(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def materialize() -> dict[str, Any]:
    manifest_path = RUN_DIR / "manifest.json"
    if manifest_path.exists():
        return json.loads(manifest_path.read_text())
    if RUN_DIR.exists():
        raise RuntimeError(f"refusing partially existing campaign directory: {RUN_DIR}")
    RUN_DIR.mkdir(parents=True)
    source_manifest = prepare_agent_source(
        json.loads(json.dumps(yaml.safe_load((ROOT / "experiment.yaml").read_text()))),
        RUN_DIR / "source",
        ["deepseek-harness"],
    )
    configs = RUN_DIR / "configs"
    for logical in MODELS:
        write_yaml(configs / "ale" / "agents" / f"{logical}.yaml", ale_agent(logical))
        write_yaml(
            configs / "ale" / "experiments" / f"{logical}.yaml",
            ale_experiment(logical),
        )
    write_yaml(configs / "ale" / "environment.yaml", ale_environment())
    task_path = configs / "ale" / "tasks.txt"
    task_path.parent.mkdir(parents=True, exist_ok=True)
    task_path.write_text(ALE_TASK + "\n")
    task_path.chmod(0o444)
    body = manifest_body(source_manifest)
    body["config_sha256"] = config_hashes()
    manifest = dict(body)
    manifest["manifest_sha256"] = hashlib.sha256(canonical(body)).hexdigest()
    write_json(manifest_path, manifest)
    return manifest


def verify_frozen(manifest: dict[str, Any]) -> None:
    recorded = manifest.get("manifest_sha256")
    body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if hashlib.sha256(canonical(body)).hexdigest() != recorded:
        raise RuntimeError("manifest hash mismatch")
    if config_hashes() != manifest["config_sha256"]:
        raise RuntimeError("frozen config hash mismatch")
    if relevant_source_hashes() != manifest["source_sha256"]:
        raise RuntimeError("relevant repository source changed after freeze")


def verify_ale_import_boundary() -> None:
    """Prove ALE runner and study deployer resolve from the frozen source tree."""
    source = (RUN_DIR / "source").resolve()
    probe = subprocess.run(
        [
            str(ROOT / ".venv" / "bin" / "python"),
            "-c",
            (
                "import inspect, json; "
                "from ale_run.executors import sandbox; "
                "from ale_run.agents.study_deepseek.deployer import StudyDeepSeekDeployer; "
                "print(json.dumps({"
                "'runner': inspect.getfile(sandbox), "
                "'deployer': inspect.getfile(StudyDeepSeekDeployer), "
                "'selected_root': str(sandbox._host_ale_root_for_deployer(StudyDeepSeekDeployer))"
                "}))"
            ),
        ],
        cwd=ALE_ROOT,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join((str(source), str(ALE_ROOT))),
            "PYTHONSAFEPATH": "1",
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    if probe.returncode != 0:
        raise RuntimeError(f"ALE import-boundary probe failed: {probe.stderr[-2000:]}")
    resolved = json.loads(probe.stdout.strip().splitlines()[-1])
    for field in ("runner", "deployer", "selected_root"):
        if not Path(resolved[field]).resolve().is_relative_to(source):
            raise RuntimeError(
                f"ALE {field} escaped frozen source tree: {resolved[field]}"
            )


def preflight() -> int:
    manifest = materialize()
    verify_frozen(manifest)
    verify_ale_import_boundary()
    ale_env = os.environ.copy()
    ale_env["PYTHONPATH"] = os.pathsep.join(
        (str(RUN_DIR / "source"), str(ALE_ROOT))
    )
    ale_env["PYTHONSAFEPATH"] = "1"
    for logical in MODELS:
        experiment = RUN_DIR / "configs" / "ale" / "experiments" / f"{logical}.yaml"
        result = subprocess.run(
            [
                str(ROOT / ".venv" / "bin" / "python"), "-m", "ale_run", "run",
                str(experiment), "--dry-run", "--disable-resume",
            ],
            cwd=ALE_ROOT,
            env=ale_env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0 or "units (1):" not in result.stdout:
            raise RuntimeError(
                f"ALE loader failed for {logical}: {(result.stdout + result.stderr)[-3000:]}"
            )

    image = subprocess.run(
        ["docker", "image", "inspect", ALE_DOCKER_IMAGE, "--format", "{{.Id}} {{.Size}}"],
        capture_output=True, text=True, timeout=30,
    )
    if image.returncode != 0:
        raise RuntimeError("exact ALE image is unavailable to this Docker daemon")
    task_variant = ALE_ROOT / "task-data" / ALE_TASK / "base"
    for required in ("input", "reference"):
        if not (task_variant / required).is_dir():
            raise RuntimeError(f"ALE task data missing required {required}/ directory")
    if tree_sha256(task_variant) != manifest["sandbox"]["task_data_variant_sha256"]:
        raise RuntimeError("ALE task data changed after manifest freeze")
    print("Zero-cost preflight passed for 5 ALE DSH trials.")
    print(f"ALE image      {image.stdout.strip()}")
    print(f"Manifest SHA   {manifest['manifest_sha256']}")
    print("No model generation was made.")
    return 0


def wait_process(process: subprocess.Popen[Any], timeout: int) -> int:
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        return 124


def run_command(command: list[str], *, cwd: Path, env: dict[str, str], log: Path, timeout: int) -> int:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(
            command, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT,
            text=True, start_new_session=True,
        )
        return wait_process(process, timeout)


def write_state(state: dict[str, Any], lock: threading.Lock) -> None:
    with lock:
        path = RUN_DIR / "run-state.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
        os.replace(temporary, path)


def run_lane(
    logical: str, api_key: str, state: dict[str, Any], lock: threading.Lock
) -> int:
    state["lanes"][logical] = {"stage": "ale", "status": "running"}
    write_state(state, lock)

    ale_env = os.environ.copy()
    ale_env["OPENROUTER_API_KEY"] = api_key
    ale_env["PYTHONPATH"] = os.pathsep.join(
        filter(
            None,
            (
                str(RUN_DIR / "source"),
                str(ALE_ROOT),
                ale_env.get("PYTHONPATH"),
            ),
        )
    )
    ale_env["PYTHONSAFEPATH"] = "1"
    ale_rc = run_command(
        [
            str(ROOT / ".venv" / "bin" / "python"), "-m", "ale_run", "run",
            str(RUN_DIR / "configs" / "ale" / "experiments" / f"{logical}.yaml"),
            "--disable-resume",
        ],
        cwd=ALE_ROOT,
        env=ale_env,
        log=RUN_DIR / "logs" / f"{logical}--ale.log",
        timeout=4 * 60 * 60,
    )
    state["lanes"][logical].update(
        {"ale_return_code": ale_rc, "stage": "done", "status": "finished"}
    )
    write_state(state, lock)
    return ale_rc


def run(approved_hash: str) -> int:
    manifest = materialize()
    verify_frozen(manifest)
    verify_ale_import_boundary()
    if approved_hash != manifest["manifest_sha256"]:
        raise RuntimeError("approved manifest hash does not match frozen campaign")
    if (RUN_DIR / "run-state.json").exists() or (RUN_DIR / "raw").exists():
        raise RuntimeError("refusing to reuse an already-started campaign")
    api_key = provider_base.load_key()
    before = provider_base.common.current_key_usage(api_key)
    write_json(RUN_DIR / "openrouter-usage-before.json", before, mode=0o600)
    state: dict[str, Any] = {
        "campaign_id": CAMPAIGN_ID,
        "manifest_sha256": approved_hash,
        "status": "running",
        "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "lanes": {},
    }
    lock = threading.Lock()
    write_state(state, lock)
    results: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=LANE_CONCURRENCY) as pool:
        futures = {
            pool.submit(run_lane, logical, api_key, state, lock): logical
            for logical in MODELS
        }
        for future in as_completed(futures):
            logical = futures[future]
            try:
                results[logical] = future.result()
            except Exception as exc:  # preserve other independent lanes
                results[logical] = 125
                state["lanes"][logical] = {
                    "stage": "done", "status": "launcher-error",
                    "error": f"{type(exc).__name__}: {exc}",
                }
                write_state(state, lock)
    after = provider_base.common.current_key_usage(api_key)
    write_json(RUN_DIR / "openrouter-usage-after.json", after, mode=0o600)
    state.update(
        {
            "status": "finished",
            "finished_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "return_codes": results,
            "trajectory_audit_required": True,
        }
    )
    write_state(state, lock)
    return 0 if all(code == 0 for code in results.values()) else 1


def launch(approved_hash: str) -> int:
    if subprocess.run(
        ["tmux", "has-session", "-t", CAMPAIGN_ID], capture_output=True
    ).returncode == 0:
        raise RuntimeError(f"tmux session already exists: {CAMPAIGN_ID}")
    command = (
        f"exec {shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} "
        f"run --approved-manifest-sha256 {shlex.quote(approved_hash)}"
    )
    subprocess.run(
        [
            "tmux", "new-session", "-d", "-s", CAMPAIGN_ID, "-c", str(ROOT),
            "bash", "-lc", command,
        ],
        check=True,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("materialize")
    sub.add_parser("preflight")
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--approved-manifest-sha256", required=True)
    launch_parser = sub.add_parser("launch")
    launch_parser.add_argument("--approved-manifest-sha256", required=True)
    args = parser.parse_args()
    if args.command == "materialize":
        print(materialize()["manifest_sha256"])
        return 0
    if args.command == "preflight":
        return preflight()
    if args.command == "run":
        return run(args.approved_manifest_sha256)
    return launch(args.approved_manifest_sha256)


if __name__ == "__main__":
    raise SystemExit(main())
