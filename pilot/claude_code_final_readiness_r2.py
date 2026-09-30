#!/usr/bin/env python3
"""Qualification-only repair for Claude Code final-readiness r1.

R1's real Claude/TB4 wave remains valid, but its three generated datasets were
not discoverable because Harbor requires an ``environment/`` directory even
when ``task.toml`` pins a prebuilt image.  This thin repair reuses the frozen
runner and removes the already-running TB4 wave; no model or harness controls
change.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import claude_code_final_readiness as base


ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN_ID = "pilot-claude-code-final-readiness-20260901-r2"
RUN_DIR = ROOT / "runs" / CAMPAIGN_ID
JOBS_DIR = RUN_DIR / "harbor"

base.CAMPAIGN_ID = CAMPAIGN_ID
base.RUN_DIR = RUN_DIR
base.JOBS_DIR = JOBS_DIR
base.INJECTOR_BASE_PORT = 4310

_materialize = base.materialize_qualification_dataset
_wave_configs = base.wave_configs
_build_manifest = base.build_manifest


def materialize_qualification_dataset(path: Path) -> None:
    _materialize(path)
    for task_name in (
        "compaction-recovery",
        "image-tool-canary",
        "subagent-burst-canary",
    ):
        # Harbor 0.22's Task.is_valid_dir requires this directory even when the
        # environment is a pinned prebuilt docker_image and needs no Dockerfile.
        (path / task_name / "environment").mkdir(parents=True, exist_ok=True)


base.materialize_qualification_dataset = materialize_qualification_dataset


def wave_configs(
    *, overlay_path: Path, dataset_path: Path, history_paths: dict[str, Path]
) -> list[tuple[str, dict[str, Any]]]:
    return [
        (name, config)
        for name, config in _wave_configs(
            overlay_path=overlay_path,
            dataset_path=dataset_path,
            history_paths=history_paths,
        )
        if name != "claude-tb4"
    ]


base.wave_configs = wave_configs


def build_manifest() -> dict[str, Any]:
    manifest = _build_manifest()
    manifest["campaign_id"] = CAMPAIGN_ID
    manifest["kind"] = "paid-claude-code-final-readiness-qualification-repair"
    manifest["scope"].update(
        {
            "benchmark": "qualification fixtures only; no benchmark scores",
            "planned_trials": 8,
            "waves": [
                {"name": "compaction", "trials": 4, "concurrency": 4},
                {"name": "image", "trials": 3, "concurrency": 3},
                {"name": "request-gate", "trials": 1, "concurrency": 1},
            ],
            "repair_of": {
                "campaign_id": "pilot-claude-code-final-readiness-20260901-r1",
                "failed_before_model_calls": True,
                "root_cause": (
                    "generated task directories omitted Harbor's mandatory empty "
                    "environment/ directory"
                ),
                "change": (
                    "add only the required empty directories and omit the already-run "
                    "Claude/TB4 wave"
                ),
            },
        }
    )
    manifest["runner"]["entry"] = (
        "python pilot/claude_code_final_readiness_r2.py run "
        "--approved-manifest-sha256 <sha256>"
    )
    manifest["runner"]["resolved_wave_configs"] = {
        key: value
        for key, value in manifest["runner"]["resolved_wave_configs"].items()
        if key != "claude-tb4"
    }
    manifest["qualification_criteria"].pop("claude_tb4", None)
    manifest["source_sha256"]["pilot/claude_code_final_readiness_r2.py"] = (
        base.sha256_file(Path(__file__))
    )
    manifest.pop("manifest_sha256", None)
    manifest["manifest_sha256"] = hashlib.sha256(
        base.canonical_json(manifest)
    ).hexdigest()
    return manifest


base.build_manifest = build_manifest


def validate_dataset_resolution() -> None:
    with tempfile.TemporaryDirectory(prefix="cc-final-r2-dataset-", dir="/tmp") as raw:
        dataset = Path(raw)
        materialize_qualification_dataset(dataset)
        harbor_python = Path(
            "/tmp/harness-test-uv-cache/archive-v0/HxKgPvImD13HvOTygvTz3/bin/python"
        )
        code = (
            "import asyncio,json,sys; from pathlib import Path; "
            "from harbor.models.job.config import DatasetConfig; "
            "xs=asyncio.run(DatasetConfig(path=Path(sys.argv[1])).get_task_configs()); "
            "print(json.dumps(sorted(x.path.name for x in xs)))"
        )
        completed = subprocess.run(
            [str(harbor_python), "-c", code, str(dataset)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        names = json.loads(completed.stdout)
        expected = [
            "compaction-recovery",
            "image-tool-canary",
            "subagent-burst-canary",
        ]
        if names != expected:
            raise RuntimeError(
                f"Harbor dataset resolution mismatch: expected {expected}, got {names}"
            )


_zero_cost_preflight = base.zero_cost_preflight


def zero_cost_preflight() -> int:
    result = _zero_cost_preflight()
    if result != 0:
        return result
    validate_dataset_resolution()
    print("Harbor local-dataset resolution passed: 3/3 qualification tasks discovered")
    return 0


base.zero_cost_preflight = zero_cost_preflight


def main() -> int:
    return base.main()


if __name__ == "__main__":
    raise SystemExit(main())
