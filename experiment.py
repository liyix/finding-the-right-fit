#!/usr/bin/env python3
"""Local, zero-cost planning and environment checks for the eval campaign."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "experiment.yaml"


def load_config(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"config not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("config root must be a mapping")
    return data


def canonical_hash(config: dict[str, Any]) -> str:
    payload = json.dumps(
        config, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_manifest_sha256(root: Path) -> str:
    """Hash sha256sum-style file records, relative to root's parent."""
    digest = hashlib.sha256()
    files = sorted(
        path for path in root.rglob("*") if path.is_file() and not path.is_symlink()
    )
    for path in files:
        relative = path.relative_to(root.parent).as_posix()
        digest.update(f"{file_sha256(path)}  {relative}\n".encode("utf-8"))
    return digest.hexdigest()


def validate(config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    expected_counts = {"models": 5, "harnesses": 5, "benchmarks": 3}
    for section, count in expected_counts.items():
        items = config.get(section)
        if not isinstance(items, list):
            errors.append(f"{section} must be a list")
            continue
        if len(items) != count:
            errors.append(f"{section} must contain exactly {count} entries")
        ids = [item.get("id") for item in items if isinstance(item, dict)]
        if len(ids) != len(items) or any(not isinstance(item_id, str) for item_id in ids):
            errors.append(f"every {section} entry must have a string id")
        elif len(set(ids)) != len(ids):
            errors.append(f"{section} ids must be unique")

    campaign = config.get("campaign")
    if not isinstance(campaign, dict):
        errors.append("campaign must be a mapping")
    else:
        repetitions = campaign.get("repetitions")
        if not isinstance(repetitions, int) or repetitions < 1:
            errors.append("campaign.repetitions must be a positive integer")

    benchmarks = config.get("benchmarks", [])
    if isinstance(benchmarks, list):
        for item in benchmarks:
            if not isinstance(item, dict):
                continue
            tasks = item.get("tasks")
            if not isinstance(tasks, int) or tasks < 1:
                errors.append(f"benchmark {item.get('id', '?')} has invalid task count")

    providers = config.get("providers")
    if not isinstance(providers, dict):
        errors.append("providers must be a mapping")
    else:
        for provider_id in ("zai", "deepseek", "moonshot", "openrouter"):
            provider = providers.get(provider_id)
            if not isinstance(provider, dict):
                errors.append(f"providers.{provider_id} must be a mapping")
                continue
            for key in ("api_key_env", "billing_source"):
                if not isinstance(provider.get(key), str) or not provider[key]:
                    errors.append(
                        f"providers.{provider_id}.{key} must be a non-empty string"
                    )

    for item in config.get("harnesses", []):
        if not isinstance(item, dict):
            continue
        version = item.get("version")
        if not isinstance(version, (str, int, float)) or not str(version):
            errors.append(f"harness {item.get('id', '?')} must have an exact version")
        elif "pending" in str(version).lower():
            errors.append(f"harness {item.get('id', '?')} version cannot be pending")
        registered_models = item.get("study_models")
        if registered_models is not None:
            known_models = {
                model.get("id")
                for model in config.get("models", [])
                if isinstance(model, dict)
            }
            if (
                not isinstance(registered_models, list)
                or not registered_models
                or any(not isinstance(model_id, str) for model_id in registered_models)
            ):
                errors.append(
                    f"harness {item.get('id', '?')} study_models must be a non-empty string list"
                )
            elif len(set(registered_models)) != len(registered_models):
                errors.append(
                    f"harness {item.get('id', '?')} study_models must be unique"
                )
            elif unknown := set(registered_models) - known_models:
                errors.append(
                    f"harness {item.get('id', '?')} has unknown study_models: "
                    + ", ".join(sorted(unknown))
                )

        registry_schema = item.get("registry_schema")
        if registry_schema is not None:
            harness_id = item.get("id", "?")
            if registry_schema != 1:
                errors.append(f"harness {harness_id} has unsupported registry_schema")
            if item.get("registry_status") not in {"draft", "complete"}:
                errors.append(
                    f"harness {harness_id} registry_status must be draft or complete"
                )
            required_mappings = (
                "install",
                "runners",
                "model_transport",
                "controls",
                "telemetry",
                "qualification",
            )
            for key in required_mappings:
                if not isinstance(item.get(key), dict):
                    errors.append(f"harness {harness_id}.{key} must be a mapping")

            runners = item.get("runners")
            if isinstance(runners, dict):
                for runner_id in ("harbor", "ale"):
                    if not isinstance(runners.get(runner_id), dict):
                        errors.append(
                            f"harness {harness_id}.runners.{runner_id} must be a mapping"
                        )

            qualification = item.get("qualification")
            if isinstance(qualification, dict):
                if qualification.get("source_of_truth") != (
                    "reports/provider-compatibility.md"
                ):
                    errors.append(
                        f"harness {harness_id} qualification must reference the "
                        "canonical compatibility report"
                    )
                for benchmark_id in (
                    "terminal-bench-4",
                    "tua-bench",
                    "ale-cli",
                ):
                    record = qualification.get(benchmark_id)
                    if not isinstance(record, dict):
                        errors.append(
                            f"harness {harness_id}.qualification.{benchmark_id} "
                            "must be a mapping"
                        )
                        continue
                    if record.get("level") not in {"B", "T", "C", "none"}:
                        errors.append(
                            f"harness {harness_id}.qualification.{benchmark_id} "
                            "has invalid level"
                        )
                    evidence = record.get("evidence")
                    if record.get("level") != "none" and (
                        not isinstance(evidence, list) or not evidence
                    ):
                        errors.append(
                            f"harness {harness_id}.qualification.{benchmark_id} "
                            "requires evidence"
                        )

            if item.get("registry_status") == "complete":
                if not registered_models:
                    errors.append(
                        f"harness {harness_id} complete registry requires study_models"
                    )
                transport = item.get("model_transport")
                if isinstance(transport, dict) and (
                    transport.get("allow_fallbacks") is not False
                ):
                    errors.append(
                        f"harness {harness_id} complete registry must disable fallbacks"
                    )

    approval = config.get("approval")
    if not isinstance(approval, dict):
        errors.append("approval must be a mapping")
    else:
        for key in (
            "require_before_every_paid_run",
            "require_post_run_trajectory_audit",
        ):
            if approval.get(key) is not True:
                errors.append(f"approval.{key} must remain true")
    return errors


def unresolved(config: dict[str, Any]) -> list[tuple[str, str]]:
    results: list[tuple[str, str]] = []
    markers = ("pending", "blocked", "unchecked")

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                walk(child, f"{path}.{key}" if path else str(key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                label = child.get("id", index) if isinstance(child, dict) else index
                walk(child, f"{path}[{label}]")
        elif isinstance(value, str) and any(marker in value.lower() for marker in markers):
            results.append((path, value))

    walk(config, "")
    return results


def find_by_id(
    config: dict[str, Any], section: str, item_id: str
) -> dict[str, Any] | None:
    return next((item for item in config[section] if item["id"] == item_id), None)


def study_model_ids(config: dict[str, Any], harness: dict[str, Any]) -> list[str]:
    """Return the registered model row for one harness."""
    configured = harness.get("study_models")
    if configured is not None:
        return configured
    return [model["id"] for model in config["models"]]


def study_cells(config: dict[str, Any]) -> list[tuple[str, str]]:
    return [
        (harness["id"], model_id)
        for harness in config["harnesses"]
        for model_id in study_model_ids(config, harness)
    ]


def qualification_level(harness: dict[str, Any], benchmark_id: str) -> str:
    """Return the evidence level registered for one harness/benchmark path."""
    qualification = harness.get("qualification", {})
    record = qualification.get(benchmark_id, {}) if isinstance(qualification, dict) else {}
    return str(record.get("level", "none")) if isinstance(record, dict) else "none"


def plan(
    config: dict[str, Any],
    *,
    benchmark_id: str | None,
    harness_id: str | None,
    model_id: str | None,
    task_count: int,
    full_matrix: bool,
) -> int:
    errors = validate(config)
    if errors:
        for error in errors:
            print(f"ERROR  {error}")
        return 2

    repetitions = config["campaign"]["repetitions"]
    digest = canonical_hash(config)
    campaign_id = f"{config['campaign']['kind']}-{digest[:12]}"

    print(f"Campaign      {campaign_id} ({config['campaign']['status']})")
    print(f"Config SHA256 {digest}")

    selectors = (benchmark_id, harness_id, model_id)
    if full_matrix:
        if any(selectors):
            print("ERROR  --all cannot be combined with slice selectors")
            return 2
        task_sum = sum(
            item["local_docker"]["tasks"] if item["id"] == "ale-cli"
            else item["tasks"]
            for item in config["benchmarks"]
        )
        cells_per_benchmark = len(study_cells(config))
        jobs = cells_per_benchmark * len(config["benchmarks"]) * repetitions
        trials = cells_per_benchmark * task_sum * repetitions
        print(
            f"Scope         registered sparse design: {cells_per_benchmark} cells × "
            f"{len(config['benchmarks'])} benchmarks"
        )
        print(f"Workload      {jobs} atomic jobs; {trials:,} task trials")
        qualified_jobs = sum(
            len(study_model_ids(config, harness)) * repetitions
            for harness in config["harnesses"]
            for benchmark in config["benchmarks"]
            if qualification_level(harness, benchmark["id"]) == "B"
        )
        print(f"Qualification {qualified_jobs}/{jobs} atomic jobs currently at B")
        for harness in config["harnesses"]:
            for benchmark in config["benchmarks"]:
                level = qualification_level(harness, benchmark["id"])
                if level != "B":
                    print(
                        f"  - {benchmark['id']} × {harness['id']} × "
                        f"{len(study_model_ids(config, harness))} model(s): {level}"
                    )
    else:
        if not all(selectors):
            print(
                "ERROR  select exactly one benchmark, harness, and model; "
                "use --all only to inspect the registered design"
            )
            print(
                "Example       experiment.py plan --benchmark terminal-bench-4 "
                "--harness pi --model gpt-6-astra --task-count 1"
            )
            return 2
        if task_count < 1:
            print("ERROR  --task-count must be a positive integer")
            return 2

        benchmark = find_by_id(config, "benchmarks", benchmark_id)
        harness = find_by_id(config, "harnesses", harness_id)
        model = find_by_id(config, "models", model_id)
        missing = [
            ("benchmark", benchmark_id, benchmark),
            ("harness", harness_id, harness),
            ("model", model_id, model),
        ]
        for kind, selected_id, item in missing:
            if item is None:
                print(f"ERROR  unknown {kind}: {selected_id}")
                return 2
        assert benchmark is not None and harness is not None and model is not None
        allowed_models = study_model_ids(config, harness)
        if model_id not in allowed_models:
            print(
                f"ERROR  {harness_id} × {model_id} is outside the registered study matrix"
            )
            print("Allowed models " + ", ".join(allowed_models))
            return 2
        study_benchmark_tasks = (
            benchmark["local_docker"]["tasks"]
            if benchmark_id == "ale-cli" else benchmark["tasks"]
        )
        if task_count > study_benchmark_tasks:
            print(
                f"ERROR  --task-count {task_count} exceeds "
                f"{benchmark_id} study size ({study_benchmark_tasks})"
            )
            return 2

        trials = task_count * repetitions
        print(
            f"Scope         {benchmark_id} × {harness_id}@{harness['version']} × "
            f"{model_id}"
        )
        print(f"Task subset   {task_count} task(s); exact IDs not yet frozen")
        print(f"Workload      {repetitions} atomic job(s); {trials} task trial(s)")
        print(f"Route         {model.get('availability', 'unknown')}")
        level = qualification_level(harness, benchmark_id)
        print(f"Qualification {level}")
        if level != "B":
            print("WARNING       path is not qualified for primary-result use")

    pending = unresolved(config)
    print(f"Unresolved    {len(pending)} configuration values")
    for path, value in pending:
        print(f"  - {path}: {value}")
    print("Safety        local planning only; no network or model API call")
    return 0


def dotenv_keys(path: Path) -> set[str]:
    keys: set[str] = set()
    if not path.exists():
        return keys
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key.startswith("export "):
            key = key.removeprefix("export ").strip()
        if key:
            keys.add(key)
    return keys


def command_version(command: str, args: list[str]) -> tuple[bool, str]:
    executable = shutil.which(command)
    if executable is None:
        return False, "not found"
    try:
        result = subprocess.run(
            [executable, *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    output = (result.stdout or result.stderr).strip().splitlines()
    detail = output[0] if output else f"exit {result.returncode}"
    return result.returncode == 0, detail


def git_ignores(path: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "check-ignore", "--quiet", str(path)],
            cwd=ROOT,
            check=False,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def doctor(config: dict[str, Any], config_path: Path) -> int:
    failures = validate(config)
    warnings: list[str] = []
    checks: list[tuple[str, bool, str]] = []

    checks.append(
        (
            "python",
            sys.version_info >= (3, 12),
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        )
    )
    for command, args in (("uv", ["--version"]), ("docker", ["--version"])):
        ok, detail = command_version(command, args)
        checks.append((command, ok, detail))

    docker_ok, docker_detail = command_version(
        "docker", ["info", "--format", "{{.ServerVersion}}"]
    )
    checks.append(("docker-daemon", docker_ok, docker_detail))

    env_path = ROOT / ".env"
    env_keys = dotenv_keys(env_path)
    if env_path.exists():
        mode = stat.S_IMODE(env_path.stat().st_mode)
        secure = mode & 0o077 == 0
        checks.append((".env-permissions", secure, oct(mode)))
        checks.append((".env-gitignore", git_ignores(env_path), "ignored"))
    else:
        checks.append((".env", False, "not found"))

    required_env = {
        provider["api_key_env"]
        for provider in config["providers"].values()
        if isinstance(provider, dict) and "api_key_env" in provider
    }
    for key in sorted(required_env):
        sources = []
        if key in os.environ:
            sources.append("process")
        if key in env_keys:
            sources.append(".env")
        checks.append((f"secret:{key}", bool(sources), "+".join(sources) or "absent"))

    pending = unresolved(config)
    if pending:
        warnings.append(f"{len(pending)} unresolved configuration values remain")
    unavailable_models = [
        item["id"]
        for item in config["models"]
        if item.get("availability")
        not in {
            "verified",
            "authenticated-model-list-visible",
            "strict-openrouter-terminal-qualified",
        }
    ]
    if unavailable_models:
        warnings.append("model routes not verified: " + ", ".join(unavailable_models))

    print(f"Config        {config_path}")
    print(f"Config SHA256 {canonical_hash(config)}")
    for name, ok, detail in checks:
        print(f"{'OK' if ok else 'FAIL':4} {name:18} {detail}")
        if not ok:
            failures.append(f"{name}: {detail}")
    for warning in warnings:
        print(f"WARN {warning}")
    print("Safety        local checks only; .env values were not loaded or printed")
    return 0 if not failures else 2


def benchmark_doctor(
    config: dict[str, Any], config_path: Path, benchmark_id: str
) -> int:
    if benchmark_id == "ale-cli":
        from integrations.ale import benchmark_doctor as ale_benchmark_doctor

        return ale_benchmark_doctor(config, config_path)

    failures = validate(config)
    benchmark = find_by_id(config, "benchmarks", benchmark_id)
    if benchmark is None:
        print(f"ERROR  unknown benchmark: {benchmark_id}")
        return 2
    if benchmark_id == "terminal-bench-4":
        tasks_root = ROOT / benchmark["tasks_path"]
        checks: list[tuple[str, bool, str]] = []

        def add_tb(name: str, ok: bool, detail: str) -> None:
            checks.append((name, ok, detail))
            if not ok:
                failures.append(f"{name}: {detail}")

        add_tb("tasks-path", tasks_root.is_dir(), str(tasks_root))
        task_files = sorted(tasks_root.glob("*/task.toml")) if tasks_root.is_dir() else []
        task_ids = [path.parent.name for path in task_files]
        task_id_digest = hashlib.sha256("\n".join(task_ids).encode()).hexdigest()
        add_tb("task-count", len(task_files) == benchmark["tasks"], str(len(task_files)))
        add_tb("task-id-hash", task_id_digest == benchmark["task_id_sha256"], task_id_digest)
        parsed_tasks: list[dict[str, Any]] = []
        for path in task_files:
            try:
                parsed_tasks.append(tomllib.loads(path.read_text(encoding="utf-8")))
            except (OSError, tomllib.TOMLDecodeError) as exc:
                failures.append(f"cannot parse {path}: {exc}")
        if parsed_tasks:
            add_tb(
                "agent-timeouts",
                all(task.get("agent", {}).get("timeout_sec") == 28800.0 for task in parsed_tasks),
                "66/66 expected at 28800s",
            )
            add_tb(
                "task-tree-hash",
                tree_manifest_sha256(tasks_root) == benchmark["task_tree_sha256"],
                tree_manifest_sha256(tasks_root),
            )
        print(f"Config        {config_path}")
        print(f"Benchmark     {benchmark_id} ({benchmark['dataset']})")
        for name, ok, detail in checks:
            print(f"{'OK' if ok else 'FAIL':4} {name:18} {detail}")
        print("INFO study runner     harbor==0.22.0")
        print("Safety        local files only; no model API call")
        return 0 if not failures else 2
    if benchmark_id != "tua-bench":
        print(f"ERROR  unsupported benchmark-doctor target: {benchmark_id}")
        return 2

    repository = ROOT / benchmark["repository_path"]
    tasks_root = ROOT / benchmark["tasks_path"]
    checks: list[tuple[str, bool, str]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append((name, ok, detail))
        if not ok:
            failures.append(f"{name}: {detail}")

    add("repository", repository.is_dir(), str(repository))
    add("tasks-path", tasks_root.is_dir(), str(tasks_root))
    if repository.is_dir():
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository,
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
        actual_commit = result.stdout.strip() if result.returncode == 0 else result.stderr
        add("source-commit", actual_commit == benchmark["commit"], actual_commit)

    task_files = sorted(tasks_root.glob("*/task.toml")) if tasks_root.is_dir() else []
    task_ids = [path.parent.name for path in task_files]
    task_id_digest = hashlib.sha256("\n".join(task_ids).encode("utf-8")).hexdigest()
    add("task-count", len(task_files) == benchmark["tasks"], str(len(task_files)))
    add(
        "task-id-hash",
        task_id_digest == benchmark["task_id_sha256"],
        task_id_digest,
    )

    parsed_tasks: list[dict[str, Any]] = []
    for path in task_files:
        try:
            parsed_tasks.append(tomllib.loads(path.read_text(encoding="utf-8")))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            failures.append(f"cannot parse {path}: {exc}")
    if parsed_tasks:
        timeout_ok = all(
            task["agent"]["timeout_sec"] == 2400.0
            and task["verifier"]["timeout_sec"] == 2400.0
            and task["environment"]["build_timeout_sec"] == 2400.0
            for task in parsed_tasks
        )
        add("task-timeouts", timeout_ok, "agent/verifier/build=2400s")
        isolation_ok = all(
            task["environment"].get("allow_internet") is True
            and task["environment"].get("gpus") == 0
            and task["environment"].get("mcp_servers") == []
            for task in parsed_tasks
        )
        add("task-runtime", isolation_ok, "internet=true; gpu=0; mcp=[]")

    setup_script = repository / "repo_env" / "setup_env.py"
    targets: list[Path] = []
    if setup_script.is_file():
        spec = importlib.util.spec_from_file_location("_tua_setup_env", setup_script)
        if spec is None or spec.loader is None:
            failures.append(f"cannot import setup script: {setup_script}")
        else:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            targets = module.downloaded_data_targets()
    else:
        failures.append(f"setup script missing: {setup_script}")

    missing_targets = [path for path in targets if not path.is_file()]
    add(
        "setup-assets",
        len(targets) == benchmark["setup"]["asset_targets"] and not missing_targets,
        f"{len(targets) - len(missing_targets)}/{len(targets)} present",
    )
    if targets and not missing_targets:
        asset_records = "".join(
            f"{file_sha256(path)}  {path.relative_to(repository).as_posix()}\n"
            for path in targets
        )
        asset_digest = hashlib.sha256(asset_records.encode("utf-8")).hexdigest()
        asset_bytes = sum(path.stat().st_size for path in targets)
        add(
            "asset-hash",
            asset_digest == benchmark["setup"]["asset_manifest_sha256"],
            asset_digest,
        )
        add(
            "asset-bytes",
            asset_bytes == benchmark["setup"]["asset_bytes"],
            str(asset_bytes),
        )

    if tasks_root.is_dir() and task_files:
        tree_digest = tree_manifest_sha256(tasks_root)
        add(
            "task-tree-hash",
            tree_digest == benchmark["task_tree_sha256"],
            tree_digest,
        )

    # Resolve all five DSH arms against the exact TUA tasks path without
    # launching Harbor, Docker, or a model.  The 2x setup multiplier is only
    # the current cross-harness structural candidate; a paid manifest must
    # still present and obtain approval for the final shared setup policy.
    try:
        from integrations.ale_agents.study_deepseek.config import (
            build_harbor_dataset_config,
        )

        dsh_configs: list[dict[str, Any]] = []
        for model in config["models"]:
            openrouter = model["openrouter"]
            route = {
                "only": openrouter["provider_only"],
                "allow_fallbacks": False,
                "require_parameters": True,
            }
            if quantizations := openrouter.get("quantizations"):
                route["quantizations"] = quantizations
            compat: dict[str, Any] = {
                "supportsDeveloperRole": False,
                "supportsReasoningEffort": True,
                "maxTokensField": "max_tokens",
                "thinkingFormat": "openrouter",
            }
            cache_retention = None
            if model["id"] == "claude-opus-5":
                compat["cacheControlFormat"] = "anthropic"
                cache_retention = "short"
            dsh_configs.append(build_harbor_dataset_config(
                job_name=f"tua--deepseek-harness--{model['id']}--structural",
                jobs_dir=Path("/tmp/tua-dsh-structural"),
                dataset_path=tasks_root,
                task_names=["106-create-charles-ssh-user"],
                model_id=openrouter["model_id"],
                route=route,
                context_window=openrouter["endpoint_context_tokens"],
                max_tokens=openrouter["endpoint_max_output_tokens"],
                input_modalities=(
                    ["text", "image"] if openrouter["supports_vision"] else ["text"]
                ),
                compat=compat,
                cache_retention=cache_retention,
                agent_setup_timeout_multiplier=2.0,
            ))
        dsh_ok = len(dsh_configs) == 5 and all(
            item["datasets"][0]["path"] == str(tasks_root)
            and item["timeout_multiplier"] == 1.0
            and item["agent_timeout_multiplier"] == 1.0
            and item["verifier_timeout_multiplier"] == 1.0
            and item["environment_build_timeout_multiplier"] == 1.0
            and item["agent_setup_timeout_multiplier"] == 2.0
            and item["retry"]["max_retries"] == 0
            and item["agents"][0]["name"]
                == "integrations.harbor_deepseek:DeepSeekHarness"
            and item["agents"][0]["skills"] == []
            and item["agents"][0]["mcp_servers"] == []
            and item["agents"][0]["kwargs"]["version"] == "0.1.1-rc.2"
            and item["agents"][0]["kwargs"]["reasoning_effort"] == "high"
            and item["agents"][0]["kwargs"]["openrouter_route"]["allow_fallbacks"]
                is False
            and item["agents"][0]["kwargs"]["openrouter_route"]["require_parameters"]
                is True
            for item in dsh_configs
        )
        add("tua-dsh-configs", dsh_ok, f"{len(dsh_configs)}/5 strict-route arms")
    except Exception as exc:  # noqa: BLE001 - doctor must report, not crash
        add("tua-dsh-configs", False, f"{type(exc).__name__}: {exc}")

    print(f"Config        {config_path}")
    print(f"Benchmark     {benchmark_id}")
    for name, ok, detail in checks:
        print(f"{'OK' if ok else 'FAIL':4} {name:18} {detail}")
    print(
        "INFO upstream runner  "
        f"{benchmark['setup']['upstream_harbor_package']} (setup only)"
    )
    print(
        "INFO study runner     "
        f"{benchmark['setup']['study_harbor_package']} (all experiment jobs)"
    )
    print("Safety        local files only; no model API call")
    return 0 if not failures else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan the harness evaluation without making model API calls."
    )
    parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG, help="experiment YAML"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan_parser = subparsers.add_parser(
        "plan", help="validate and summarize one explicit experiment slice"
    )
    plan_parser.add_argument("--benchmark", help="one benchmark id")
    plan_parser.add_argument("--harness", help="one harness id")
    plan_parser.add_argument("--model", help="one model id")
    plan_parser.add_argument(
        "--task-count", type=int, default=1, help="planning estimate (default: 1)"
    )
    plan_parser.add_argument(
        "--all",
        dest="full_matrix",
        action="store_true",
        help="summarize the registered study matrix; still does not run it",
    )
    setup_parser = subparsers.add_parser(
        "setup", help="install/check the pinned host runner for one harness; never call models"
    )
    setup_parser.add_argument("--harness", required=True)
    setup_parser.add_argument(
        "--check-only", action="store_true", help="print commands without executing them"
    )
    doctor_parser = subparsers.add_parser(
        "doctor", help="run local environment checks; never call APIs"
    )
    doctor_parser.add_argument("--harness")
    doctor_parser.add_argument(
        "--benchmark", action="append", help="repeat to check selected benchmarks"
    )
    prepare_parser = subparsers.add_parser(
        "prepare", help="freeze a resolved campaign and run zero-cost config checks"
    )
    prepare_parser.add_argument("--local", type=Path, default=ROOT / "local.yaml")
    prepare_parser.add_argument("--run-id", required=True)
    prepare_parser.add_argument("--harness", required=True)
    prepare_parser.add_argument("--model", action="append")
    prepare_parser.add_argument("--benchmark", action="append")
    prepare_parser.add_argument("--task", action="append")
    prepare_parser.add_argument(
        "--purpose", choices=("formal", "pilot"), default="formal"
    )
    prepare_parser.add_argument("--concurrency", type=int)
    prepare_parser.add_argument("--unit-concurrency", type=int)
    prepare_parser.add_argument(
        "--qualification-gate",
        action="store_true",
        help="allow the registered PI×ALE first-formal-shard gate only",
    )
    run_parser = subparsers.add_parser(
        "run", help="launch one explicitly approved immutable manifest in tmux"
    )
    run_parser.add_argument("--manifest", type=Path, required=True)
    run_parser.add_argument("--approved-manifest-sha256", required=True)
    status_parser = subparsers.add_parser("status", help="show compact campaign status")
    status_parser.add_argument("--manifest", type=Path, required=True)
    audit_parser = subparsers.add_parser(
        "audit", help="summarize results and preserve a post-run audit record"
    )
    audit_parser.add_argument("--manifest", type=Path, required=True)
    audit_parser.add_argument(
        "--fetch-provider", action="store_true",
        help="fetch settled OpenRouter generation metadata (no model generation)",
    )
    execute_parser = subparsers.add_parser("_execute", help=argparse.SUPPRESS)
    execute_parser.add_argument("--manifest", type=Path, required=True)
    execute_parser.add_argument("--approved-manifest-sha256", required=True)
    benchmark_parser = subparsers.add_parser(
        "benchmark-doctor",
        help="validate a prepared benchmark checkout and assets; never call APIs",
    )
    benchmark_parser.add_argument("--benchmark", required=True, help="benchmark id")
    ale_agent_parser = subparsers.add_parser(
        "ale-agent-doctor",
        help="offline-check study ALE deployers; never call model APIs",
    )
    ale_agent_parser.add_argument(
        "--harness",
        action="append",
        choices=("claude-code", "pi", "codex", "openhands", "deepseek-harness", "openjiuwen"),
        help="repeat to select deployers; default checks all implemented candidates",
    )
    ale_prepare_parser = subparsers.add_parser(
        "ale-prepare-agents",
        help="materialize a no-overwrite derived ALE source tree",
    )
    ale_prepare_parser.add_argument("--output", type=Path, required=True)
    ale_prepare_parser.add_argument(
        "--harness",
        action="append",
        choices=("claude-code", "pi", "codex", "openhands", "deepseek-harness", "openjiuwen"),
        help="repeat to select deployers; default prepares all implemented candidates",
    )
    ale_canary_parser = subparsers.add_parser(
        "ale-prepare-canary",
        help="freeze the first CC/PI ALE lifecycle canary; never call model APIs",
    )
    ale_canary_parser.add_argument("--output", type=Path, required=True)
    ale_pi_canary_parser = subparsers.add_parser(
        "ale-prepare-pi-four-model-canary",
        help="freeze one four-model PI ALE lifecycle campaign; never call model APIs",
    )
    ale_pi_canary_parser.add_argument("--output", type=Path, required=True)
    ale_pi_modal_parser = subparsers.add_parser(
        "ale-prepare-pi-cua-multimodal-canary",
        help="freeze PI x five-model real ALE GUI/CUA campaign; never call model APIs",
    )
    ale_pi_modal_parser.add_argument("--output", type=Path, required=True)
    ale_codex_canary_parser = subparsers.add_parser(
        "ale-prepare-codex-canary",
        help="freeze one stock-Codex GPT ALE lifecycle canary; never call model APIs",
    )
    ale_codex_canary_parser.add_argument("--output", type=Path, required=True)
    ale_openhands_canary_parser = subparsers.add_parser(
        "ale-prepare-openhands-canary",
        help="freeze selected OpenHands ALE lifecycle canaries; never call model APIs",
    )
    ale_openhands_canary_parser.add_argument("--output", type=Path, required=True)
    ale_openhands_canary_parser.add_argument(
        "--model",
        action="append",
        choices=(
            "claude-opus-5",
            "gpt-6-astra",
            "glm-5.3",
            "kimi-k3",
            "deepseek-v4-pro",
        ),
        help="repeat to select models; default is the GLM canary",
    )
    ale_openhands_canary_parser.add_argument(
        "--concurrency", type=int, default=1
    )
    ale_openhands_canary_parser.add_argument(
        "--task",
        action="append",
        help="repeat to select ALE tasks; default is the short text canary",
    )
    ale_openjiuwen_canary_parser = subparsers.add_parser(
        "ale-prepare-openjiuwen-canary",
        help="freeze an openJiuwen ALE lifecycle canary; never call model APIs",
    )
    ale_openjiuwen_canary_parser.add_argument("--output", type=Path, required=True)
    ale_openjiuwen_canary_parser.add_argument(
        "--model",
        action="append",
        choices=(
            "claude-opus-5", "gpt-6-astra", "gpt-6-astra", "glm-5.3",
            "kimi-k3", "deepseek-v4-pro",
        ),
        help="repeat to select models; default is DeepSeek V4 Pro",
    )
    ale_openjiuwen_canary_parser.add_argument("--concurrency", type=int, default=1)
    ale_openjiuwen_canary_parser.add_argument(
        "--task", action="append",
        help="repeat to select ALE tasks; default is the short text canary",
    )
    ale_openjiuwen_canary_parser.add_argument("--replacement-of-campaign")
    ale_openjiuwen_canary_parser.add_argument("--replacement-of-manifest-sha256")
    ale_openjiuwen_canary_parser.add_argument("--replacement-audit-sha256")
    ale_openjiuwen_canary_parser.add_argument("--estimated-cost-low-usd", type=float)
    ale_openjiuwen_canary_parser.add_argument("--estimated-cost-high-usd", type=float)
    ale_openjiuwen_canary_parser.add_argument("--estimated-cost-basis")
    ale_canary_doctor_parser = subparsers.add_parser(
        "ale-canary-doctor",
        help="validate a frozen ALE canary; never load secret values or call models",
    )
    ale_canary_doctor_parser.add_argument("--campaign-dir", type=Path, required=True)
    ale_run_canary_parser = subparsers.add_parser(
        "ale-run-canary",
        help="run one explicitly approved frozen ALE CC/PI canary",
    )
    ale_run_canary_parser.add_argument("--campaign-dir", type=Path, required=True)
    ale_run_canary_parser.add_argument(
        "--approved-manifest-sha256", required=True
    )
    ale_pi_audit_parser = subparsers.add_parser(
        "ale-audit-pi-canary",
        help="audit one completed four-model PI ALE campaign",
    )
    ale_pi_audit_parser.add_argument("--campaign-dir", type=Path, required=True)
    ale_codex_audit_parser = subparsers.add_parser(
        "ale-audit-codex-canary",
        help="audit one completed stock-Codex GPT ALE campaign",
    )
    ale_codex_audit_parser.add_argument("--campaign-dir", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config_path = args.config.resolve()
    try:
        config = load_config(config_path)
    except ValueError as exc:
        print(f"ERROR  {exc}", file=sys.stderr)
        return 2
    if args.command == "setup":
        from integrations.campaign import setup_environment

        try:
            commands = setup_environment(
                config, harness_id=args.harness, execute=not args.check_only
            )
        except (ValueError, OSError, subprocess.CalledProcessError) as exc:
            print(f"ERROR  {exc}", file=sys.stderr)
            return 2
        for command in commands:
            print(("Planned       " if args.check_only else "Managed       ") + shlex.join(command))
        print(
            "Result        commands checked only"
            if args.check_only
            else "Result        pinned host setup completed"
        )
        print("Safety        no model API call")
        return 0
    if args.command == "prepare":
        from integrations.campaign import prepare_campaign, preflight_campaign

        try:
            manifest = prepare_campaign(
                config,
                local_path=args.local.resolve(),
                run_id=args.run_id,
                harness_id=args.harness,
                model_ids=args.model,
                benchmark_ids=args.benchmark,
                explicit_tasks=args.task,
                purpose=args.purpose,
                concurrency=args.concurrency,
                unit_concurrency=args.unit_concurrency,
                qualification_gate=args.qualification_gate,
            )
            evidence = preflight_campaign(
                ROOT / "runs" / args.run_id / "manifest.json"
            )
        except (FileExistsError, ValueError, RuntimeError, OSError) as exc:
            print(f"ERROR  {exc}", file=sys.stderr)
            return 2
        print(f"Run           {manifest['run_id']}")
        print(f"Manifest      {manifest['manifest_sha256']}")
        print(f"Units         {len(manifest['units'])}")
        print(f"Trials        {manifest['expected_trials']}")
        contract = manifest["resolved_contract"]["harness"]
        registered = contract["controls"]
        context_control = registered.get("context", {})
        output_control = registered.get("output", {})
        if context_control.get("bundled_tokens") is not None:
            context_summary = (
                f"{context_control['bundled_tokens']} raw / "
                f"{context_control['effective_tokens']} effective / "
                f"compact@{context_control['auto_compact_trigger_tokens']}"
            )
        elif context_control.get("per_model_tokens"):
            context_summary = "per-model " + json.dumps(
                context_control["per_model_tokens"], separators=(",", ":")
            )
        else:
            context_summary = str(
                context_control.get("policy") or context_control.get("tokens")
            )
        if output_control.get("configured_tokens", "missing") is None:
            output_summary = "harness-native; output cap absent on wire"
        else:
            output_summary = str(
                output_control.get("policy") or output_control.get("tokens")
            )
        print(f"Harness       {contract['id']} {contract['version']}")
        print(f"Reasoning     {manifest['controls']['reasoning_effort']}")
        print("Context       " + context_summary)
        print("Output        " + output_summary)
        print(
            "Routes        " + "; ".join(
                f"{unit['model']}={','.join(unit['expected_route']['provider_only'])}"
                for unit in {
                    item["model"]: item for item in manifest["units"]
                }.values()
            )
        )
        print(
            f"Concurrency   trials={manifest['controls']['trial_concurrency']}; "
            f"units={manifest['controls']['unit_concurrency']}"
        )
        print(f"Preflight     {evidence['status']}; paid_api_calls=0")
        if manifest["qualification"]["gate_paths"]:
            print(
                "Qualification first-shard gate; audit must pass before primary use"
            )
        print("Approval      required before experiment.py run")
        return 0
    if args.command == "run":
        from integrations.campaign import launch_campaign

        try:
            session = launch_campaign(
                args.manifest.resolve(), args.approved_manifest_sha256
            )
        except (ValueError, OSError, subprocess.CalledProcessError) as exc:
            print(f"ERROR  {exc}", file=sys.stderr)
            return 2
        print(f"Launched      tmux {session}")
        print("Next          experiment.py status --manifest <manifest>")
        return 0
    if args.command == "_execute":
        from integrations.campaign import execute_campaign

        try:
            return execute_campaign(
                args.manifest.resolve(), args.approved_manifest_sha256
            )
        except (ValueError, OSError, RuntimeError) as exc:
            print(f"ERROR  {exc}", file=sys.stderr)
            return 2
    if args.command == "status":
        from integrations.campaign import campaign_status

        try:
            value = campaign_status(args.manifest.resolve())
        except (ValueError, OSError) as exc:
            print(f"ERROR  {exc}", file=sys.stderr)
            return 2
        print(json.dumps(value, indent=2, sort_keys=True))
        return 0
    if args.command == "audit":
        from integrations.campaign import audit_campaign

        try:
            value = audit_campaign(
                args.manifest.resolve(), fetch_provider=args.fetch_provider
            )
        except (FileExistsError, ValueError, OSError) as exc:
            print(f"ERROR  {exc}", file=sys.stderr)
            return 2
        print(f"Run           {value['run_id']}")
        print(
            f"Results       {value['completion']['result_files']}/"
            f"{value['completion']['expected_trials']}"
        )
        print(f"Provider cost ${value['provider']['cost_usd']:.8f}")
        print(f"Decision      {value['decision']}")
        print("Reminder      trajectory/log review is mandatory before accepting results")
        return 0
    if args.command == "plan":
        return plan(
            config,
            benchmark_id=args.benchmark,
            harness_id=args.harness,
            model_id=args.model,
            task_count=args.task_count,
            full_matrix=args.full_matrix,
        )
    if args.command == "doctor":
        result = doctor(config, config_path)
        if result != 0:
            return result
        for benchmark_id in args.benchmark or []:
            result = benchmark_doctor(config, config_path, benchmark_id)
            if result != 0:
                return result
        if args.harness and (not args.benchmark or "ale-cli" in args.benchmark):
            from integrations.ale import agent_doctor

            result = agent_doctor(config, [args.harness])
            if result != 0:
                return result
        return 0
    if args.command == "benchmark-doctor":
        return benchmark_doctor(config, config_path, args.benchmark)
    if args.command == "ale-agent-doctor":
        from integrations.ale import agent_doctor

        return agent_doctor(
            config,
            args.harness
            or ["claude-code", "pi", "codex", "openhands", "deepseek-harness"],
        )
    if args.command == "ale-prepare-agents":
        from integrations.ale import prepare_agent_source

        try:
            manifest = prepare_agent_source(
                config,
                args.output.resolve(),
                args.harness
                or ["claude-code", "pi", "codex", "openhands", "deepseek-harness"],
            )
        except (FileExistsError, ValueError, OSError) as exc:
            print(f"ERROR  {exc}", file=sys.stderr)
            return 2
        print(f"Output        {args.output.resolve()}")
        print(f"Tree SHA256   {manifest['ale_run_tree_sha256']}")
        print("Safety        source preparation only; no network or model API call")
        return 0
    if args.command == "ale-prepare-canary":
        from integrations.ale import prepare_canary

        try:
            manifest = prepare_canary(config, args.output.resolve())
        except (FileExistsError, ValueError, KeyError, OSError) as exc:
            print(f"ERROR  {exc}", file=sys.stderr)
            return 2
        print(f"Campaign      {manifest['campaign_id']}")
        print(f"Output        {args.output.resolve()}")
        print(f"Manifest SHA  {manifest['manifest_sha256']}")
        print("Trials        2 sequential; Claude Code×Claude, PI×GLM")
        print("Safety        manifest preparation only; no model API call")
        return 0
    if args.command == "ale-prepare-pi-four-model-canary":
        from integrations.ale import prepare_canary

        try:
            manifest = prepare_canary(
                config, args.output.resolve(), pi_remaining=True
            )
        except (FileExistsError, ValueError, KeyError, OSError) as exc:
            print(f"ERROR  {exc}", file=sys.stderr)
            return 2
        print(f"Campaign      {manifest['campaign_id']}")
        print(f"Output        {args.output.resolve()}")
        print(f"Manifest SHA  {manifest['manifest_sha256']}")
        print("Trials        4 sequential; PI x Claude/GPT/Kimi/DeepSeek")
        print("Safety        manifest preparation only; no model API call")
        return 0
    if args.command == "ale-prepare-pi-cua-multimodal-canary":
        from integrations.ale import prepare_canary

        try:
            manifest = prepare_canary(
                config, args.output.resolve(), pi_modal=True
            )
        except (FileExistsError, ValueError, KeyError, OSError) as exc:
            print(f"ERROR  {exc}", file=sys.stderr)
            return 2
        print(f"Campaign      {manifest['campaign_id']}")
        print(f"Output        {args.output.resolve()}")
        print(f"Manifest SHA  {manifest['manifest_sha256']}")
        print("Trials        5; PI x all models on one real ALE GUI/CUA task; concurrency=3")
        print("Safety        manifest preparation only; no model API call")
        return 0
    if args.command == "ale-prepare-codex-canary":
        from integrations.ale import prepare_canary

        try:
            manifest = prepare_canary(
                config, args.output.resolve(), codex_only=True
            )
        except (FileExistsError, ValueError, KeyError, OSError) as exc:
            print(f"ERROR  {exc}", file=sys.stderr)
            return 2
        print(f"Campaign      {manifest['campaign_id']}")
        print(f"Output        {args.output.resolve()}")
        print(f"Manifest SHA  {manifest['manifest_sha256']}")
        print("Trials        1; stock Codex 0.150.1 x GPT-5.6 Sol")
        print("Safety        manifest preparation only; no model API call")
        return 0
    if args.command == "ale-prepare-openhands-canary":
        from integrations.ale import prepare_openhands_canary

        try:
            manifest = prepare_openhands_canary(
                config,
                args.output.resolve(),
                model_ids=args.model,
                concurrency=args.concurrency,
                task_ids=args.task,
            )
        except (FileExistsError, ValueError, KeyError, OSError) as exc:
            print(f"ERROR  {exc}", file=sys.stderr)
            return 2
        print(f"Campaign      {manifest['campaign_id']}")
        print(f"Output        {args.output.resolve()}")
        print(f"Manifest SHA  {manifest['manifest_sha256']}")
        print(
            f"Trials        {manifest['scope']['planned_trials']}; "
            f"concurrency={manifest['controls']['concurrency']}; "
            f"models={','.join(manifest['scope']['models'])}; "
            f"tasks={','.join(manifest['scope']['tasks'])}"
        )
        print("Safety        manifest preparation only; no model API call")
        return 0
    if args.command == "ale-prepare-openjiuwen-canary":
        from integrations.ale import prepare_openjiuwen_canary

        try:
            replacement_values = (
                args.replacement_of_campaign,
                args.replacement_of_manifest_sha256,
                args.replacement_audit_sha256,
            )
            if any(replacement_values) and not all(replacement_values):
                raise ValueError("all replacement provenance fields are required together")
            estimate_values = (
                args.estimated_cost_low_usd,
                args.estimated_cost_high_usd,
                args.estimated_cost_basis,
            )
            if any(value is not None for value in estimate_values) and not all(
                value is not None for value in estimate_values
            ):
                raise ValueError("all estimated cost fields are required together")
            manifest = prepare_openjiuwen_canary(
                config,
                args.output.resolve(),
                model_ids=args.model,
                concurrency=args.concurrency,
                task_ids=args.task,
                replacement_of=(
                    {
                        "campaign_id": args.replacement_of_campaign,
                        "manifest_sha256": args.replacement_of_manifest_sha256,
                        "infrastructure_audit_sha256": args.replacement_audit_sha256,
                    }
                    if all(replacement_values) else None
                ),
                estimated_total_usd=(
                    {
                        "low": args.estimated_cost_low_usd,
                        "high": args.estimated_cost_high_usd,
                        "basis": args.estimated_cost_basis,
                    }
                    if all(value is not None for value in estimate_values) else None
                ),
            )
        except (FileExistsError, ValueError, KeyError, OSError) as exc:
            print(f"ERROR  {exc}", file=sys.stderr)
            return 2
        print(f"Campaign      {manifest['campaign_id']}")
        print(f"Output        {args.output.resolve()}")
        print(f"Manifest SHA  {manifest['manifest_sha256']}")
        print(
            f"Trials        {manifest['scope']['planned_trials']}; "
            f"concurrency={manifest['controls']['concurrency']}; "
            f"models={','.join(manifest['scope']['models'])}; "
            f"tasks={','.join(manifest['scope']['tasks'])}"
        )
        print("Safety        manifest preparation only; no model API call")
        return 0
    if args.command == "ale-canary-doctor":
        from integrations.ale import canary_doctor

        return canary_doctor(config, args.campaign_dir.resolve())
    if args.command == "ale-run-canary":
        from integrations.ale import run_canary

        return run_canary(
            config,
            args.campaign_dir.resolve(),
            args.approved_manifest_sha256,
        )
    if args.command == "ale-audit-pi-canary":
        from integrations.ale import audit_pi_canary

        try:
            audit = audit_pi_canary(args.campaign_dir.resolve())
        except (ValueError, KeyError, OSError) as exc:
            print(f"ERROR  {exc}", file=sys.stderr)
            return 2
        print(f"Campaign      {audit['campaign_id']}")
        print(f"Decision      {audit['decision']}")
        print(f"Provider cost ${audit['total_provider_cost_usd']:.8f}")
        for cell in audit["cells"]:
            print(
                f"{cell['agent']}: score={cell['score']} "
                f"provider={','.join(cell['actual_provider'])} "
                f"requests={cell['provider_generation_count']} "
                f"cost=${cell['provider_total_cost_usd']:.8f} "
                f"decision={cell['decision']}"
            )
        return 0 if audit["decision"] == "accept-all-four-B-text" else 2
    if args.command == "ale-audit-codex-canary":
        from integrations.ale import audit_codex_canary

        try:
            audit = audit_codex_canary(args.campaign_dir.resolve())
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            print(f"ERROR  {exc}")
            return 2
        print(f"Campaign      {audit['campaign_id']}")
        print(f"Decision      {audit['decision']}")
        print(f"Score         {audit['score']}")
        print(f"Provider cost ${audit['provider_total_cost_usd']:.8f}")
        print(f"Responses     {audit['response_count']}")
        print(f"Tool decisions {audit['tools']['decision_count']}")
        return 0 if audit["decision"] == "accept-B-text" else 2
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
