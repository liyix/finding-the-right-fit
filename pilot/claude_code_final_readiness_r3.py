#!/usr/bin/env python3
"""Near-window compaction trigger and GPT image retry for Claude Code.

R2 proved that 940k deterministic ``" x"`` units produce about 956k native
prompt tokens but remain just below Claude Code's compaction trigger.  This
qualification changes only that fixture to 975k units and retries the GPT image
canary that failed during package installation before any model request.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import claude_code_final_readiness_r2 as repair


core = repair.base
ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN_ID = "pilot-claude-code-final-readiness-20260901-r3"
RUN_DIR = ROOT / "runs" / CAMPAIGN_ID
JOBS_DIR = RUN_DIR / "harbor"

core.CAMPAIGN_ID = CAMPAIGN_ID
core.RUN_DIR = RUN_DIR
core.JOBS_DIR = JOBS_DIR
core.INJECTOR_BASE_PORT = 4410
core.HISTORY_TOKEN_UNITS = 975_000

_wave_configs = repair.wave_configs
_build_manifest = repair.build_manifest


def wave_configs(
    *, overlay_path: Path, dataset_path: Path, history_paths: dict[str, Path]
) -> list[tuple[str, dict[str, Any]]]:
    result: list[tuple[str, dict[str, Any]]] = []
    for name, config in _wave_configs(
        overlay_path=overlay_path,
        dataset_path=dataset_path,
        history_paths=history_paths,
    ):
        if name == "compaction":
            result.append((name, config))
        elif name == "image":
            config["job_name"] = f"{CAMPAIGN_ID}--gpt-image"
            config["n_concurrent_trials"] = 1
            config["agents"] = [core.agent_config("gpt-6-astra")]
            result.append(("gpt-image", config))
    return result


core.wave_configs = wave_configs


def build_manifest() -> dict[str, Any]:
    manifest = _build_manifest()
    manifest["campaign_id"] = CAMPAIGN_ID
    manifest["kind"] = "paid-claude-code-compaction-trigger-and-gpt-image-repair"
    manifest["scope"].update(
        {
            "planned_trials": 5,
            "waves": [
                {"name": "compaction", "trials": 4, "concurrency": 4},
                {"name": "gpt-image", "trials": 1, "concurrency": 1},
            ],
            "repair_of": {
                "campaign_id": "pilot-claude-code-final-readiness-20260901-r2",
                "evidence": (
                    "940k deterministic units produced about 956k native prompt tokens "
                    "but zero compaction markers"
                ),
                "change": (
                    "increase only the qualification history to 975k units; retain the "
                    "exact 1M context/compaction controls; retry GPT image after its "
                    "model-free apt-get setup failure"
                ),
            },
        }
    )
    manifest["runner"]["entry"] = (
        "python pilot/claude_code_final_readiness_r3.py run "
        "--approved-manifest-sha256 <sha256>"
    )
    manifest["runner"]["resolved_wave_configs"] = {
        key: value
        for key, value in manifest["runner"]["resolved_wave_configs"].items()
        if key in {"compaction", "gpt-image"}
    }
    manifest["scope"]["fixtures"]["compaction_history"].update(
        {
            "repetitions": core.HISTORY_TOKEN_UNITS,
            "utf8_bytes": core.HISTORY_TOKEN_UNITS * 2,
            "purpose": (
                "cross the observed native compaction trigger without lowering or "
                "spoofing the fixed 1M context window"
            ),
        }
    )
    manifest["qualification_criteria"]["compaction"] = (
        "each unknown-model trajectory must contain structural native compaction "
        "markers, a post-compaction Messages request, recovered Bash call/result, "
        "marker file, and verifier result; a prompt-too-long response or marker-free "
        "success does not pass"
    )
    manifest["qualification_criteria"]["image"] = (
        "GPT shows a native image content block followed by Bash recovery and verifier"
    )
    manifest["source_sha256"]["pilot/claude_code_final_readiness_r3.py"] = (
        core.sha256_file(Path(__file__))
    )
    manifest.pop("manifest_sha256", None)
    manifest["manifest_sha256"] = hashlib.sha256(
        core.canonical_json(manifest)
    ).hexdigest()
    return manifest


core.build_manifest = build_manifest


def main() -> int:
    return core.main()


if __name__ == "__main__":
    raise SystemExit(main())
