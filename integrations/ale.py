"""Zero-cost source checks and structural dry-run for the ALE-CLI benchmark.

This module deliberately does not launch sandboxes or model requests.  ALE
remains the benchmark runner; this repository only pins its source and checks
that our registered slice can be expanded by the upstream loader.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
ALE_AGENT_OVERLAYS = ROOT / "integrations" / "ale_agents"
ALE_AGENT_CLASSES = {
    "claude-code": (
        "study_claude_code",
        "ale_run.agents.study_claude_code.deployer.StudyClaudeCodeDeployer",
    ),
    "pi": (
        "study_pi",
        "ale_run.agents.study_pi.deployer.StudyPiDeployer",
    ),
    "codex": (
        "study_codex",
        "ale_run.agents.study_codex.deployer.StudyCodexDeployer",
    ),
    "openhands": (
        "study_openhands",
        "ale_run.agents.study_openhands.deployer.StudyOpenHandsDeployer",
    ),
    "deepseek-harness": (
        "study_deepseek",
        "ale_run.agents.study_deepseek.deployer.StudyDeepSeekDeployer",
    ),
    "openjiuwen": (
        "study_openjiuwen",
        "ale_run.agents.study_openjiuwen.deployer.StudyOpenJiuwenDeployer",
    ),
}
OPENHANDS_REASONING_PATCH = ROOT / "integrations" / "openhands_reasoning_details_patch.py"
DSH_STANDARD_PATCH = (
    ROOT / "integrations" / "patches" / "dsh-0.1.1-rc.2-headless-standard.patch"
)
OPENROUTER_BODY_INJECTOR = ROOT / "integrations" / "openrouter_body_injector.mjs"
ALE_EVALUATOR_INJECTOR = (
    ROOT / "integrations" / "ale_agents" / "study_codex" / "injector.py"
)
OPENJIUWEN_RUNNER = ROOT / "integrations" / "openjiuwen_agent.py"
OPENJIUWEN_RUNTIME_LOCK = ROOT / "integrations" / "openjiuwen-runtime.lock"
OPENJIUWEN_EXTRA_MODELS: tuple[dict[str, Any], ...] = ({
    "id": "gpt-6-astra",
    "family": "openai",
    "availability": "strict-openrouter-terminal-qualified",
    "openrouter": {
        "model_id": "openai/gpt-6-astra",
        "provider_only": ["openai"],
        "endpoint_context_tokens": 1_050_000,
        "endpoint_max_output_tokens": 128_000,
        "supports_vision": True,
    },
},)
ALE_DOCKER_IMAGE = (
    "docker.io/agentslastexam/ale-ubuntu22-docker@"
    "sha256:78ec11afeb0008ed8bc2b59cf9c90c05e63d1ac66b9d3e7cb0fada10695fca6f"
)
ALE_CANARY_TASK = "computing_math/os_log_permission_guard_v1"
ALE_CANARY_CAMPAIGN = "pilot-ale-cli-cc-pi-readiness-20260902-r6"
ALE_CODEX_CAMPAIGN = "pilot-ale-cli-codex-gpt-readiness-20260902-r2"
ALE_PI_FOUR_MODEL_CAMPAIGN = "pilot-ale-cli-pi-four-model-readiness-20260902-r1"
ALE_PI_MODAL_CAMPAIGN = "pilot-ale-cli-pi-cua-multimodal-20260902-r1"
ALE_PI_MODAL_TASK = "computing_math/go_game_reconstruction_1"
ALE_PI_REMAINING_MODELS = (
    "claude-opus-5",
    "gpt-6-astra",
    "kimi-k3",
    "deepseek-v4-pro",
)
ALE_PI_ALL_MODELS = (
    "claude-opus-5",
    "gpt-6-astra",
    "glm-5.3",
    "kimi-k3",
    "deepseek-v4-pro",
)


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


# This table is intentionally a handoff surface, not a claim of readiness.
# Any custom deployer must use the canonical versions in experiment.yaml.
DEPLOYER_HANDOFF: tuple[tuple[str, str, str], ...] = (
    (
        "claude-code",
        "zero-cost-study-candidate",
        "derived overlay inherits the official deployer and freezes CLI 2.1.251, "
        "1M/64K/high, strict route injection, OTel, and native transcript",
    ),
    (
        "codex",
        "zero-cost-study-candidate",
        "derived overlay replaces the old cua-verse fork with stock Codex "
        "0.150.1, bundled GPT metadata, high reasoning, strict OpenAI route "
        "injection, CUA MCP, OTel, and native transcript",
    ),
    (
        "openhands",
        "zero-cost-study-candidate",
        "custom SDK deployer freezes SDK/tools 1.44.1, 1M/128K/high, five "
        "default tools, strict routes, reasoning replay, and ALE CUA MCP",
    ),
    (
        "pi",
        "zero-cost-study-candidate",
        "thin PI 0.84.4 deployer preserves all five strict routes, high thinking, "
        "complete stdout/session JSONL, source-qualified usage, and exposes ALE's "
        "runner-owned CUA action space through a thin PI extension",
    ),
    (
        "deepseek-harness",
        "zero-cost-study-candidate",
        "thin rc.2 deployer preserves the standard preset, strict OpenRouter "
        "routes, full native session JSONL, and adds only ALE's runner-owned CUA MCP",
    ),
    (
        "openjiuwen",
        "candidate-extension-not-in-17-cell-matrix",
        "same openJiuwen 0.1.18 Rails composition as the TB4 candidate, mounted "
        "frozen runtime, exact ALE wall-budget Rail, native logs, and ALE CUA MCP",
    ),
)

# ALE's SandboxExecutor ships only the checkout's ``ale_run/**/*.py`` plus
# per-agent pyproject files.  Custom deployers therefore cannot live only in
# this repository's top-level ``integrations`` package at execution time.  The
# eventual manifest builder must create an immutable derived ALE source tree,
# place each approved package under ``ale_run/agents/<study_agent>/``, hash that
# tree, and launch from it.  Never edit the canonical vendor checkout in place.


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(
        item
        for item in root.rglob("*")
        if item.is_file()
        and "__pycache__" not in item.parts
        and item.suffix not in {".pyc", ".pyo"}
    ):
        relative = path.relative_to(root).as_posix()
        digest.update(f"{_sha256(path)}  {relative}\n".encode())
    return digest.hexdigest()


def _patch_sandbox_shipping(sandbox_path: Path) -> dict[str, str]:
    """Bind sandbox source shipping to the selected deployer's source tree.

    ALE uses namespace packages, so the runner and a study deployer can be
    imported from different ``ale_run`` roots.  Upstream ships relative to the
    runner module; a derived deployer must instead make its own complete source
    tree authoritative.  This patch changes only host-side source selection.
    """
    before_sha = _sha256(sandbox_path)
    source = sandbox_path.read_text(encoding="utf-8")
    replacements = (
        (
            "import asyncio\n",
            "import asyncio\nimport inspect\n",
        ),
        (
            "await self._ship_ale_subtree(ale_src_root)",
            "await self._ship_ale_subtree(\n"
            "                ale_src_root, deployer_cls=deployer_cls\n"
            "            )",
        ),
        (
            "async def _ship_ale_subtree(self, ale_src_root: str) -> None:",
            "async def _ship_ale_subtree(\n"
            "        self, ale_src_root: str, *, deployer_cls: type[Any]\n"
            "    ) -> None:",
        ),
        (
            "host_root = _host_ale_root()",
            "host_root = _host_ale_root_for_deployer(deployer_cls)",
        ),
        (
            "            \"agents/*/pyproject.toml\",\n",
            "            \"agents/*/pyproject.toml\",\n"
            "            # Study deployer compatibility assets. Keep this scoped\n"
            "            # to each agent package root; vendored upstream trees are\n"
            "            # still excluded below.\n"
            "            \"agents/*/*.patch\",\n"
            "            \"agents/*/*.mjs\",\n",
        ),
        (
            "            await sandbox.write_file(remote_path, data)\n\n"
            "    async def _read_pid",
            "            await sandbox.write_file(remote_path, data)\n\n"
            "        # Fail before install/launch if namespace-package resolution or\n"
            "        # transport drift caused the selected deployer itself not to be\n"
            "        # shipped. This check cannot trigger a model request.\n"
            "        deployer_source = Path(inspect.getfile(deployer_cls)).resolve()\n"
            "        deployer_rel = deployer_source.relative_to(host_root)\n"
            "        remote_deployer = (\n"
            "            ale_src_root.rstrip(sep) + sep + \"ale_run\" + sep\n"
            "            + deployer_rel.as_posix().replace(\"/\", sep)\n"
            "        )\n"
            "        try:\n"
            "            shipped_deployer = await sandbox.read_file(remote_deployer)\n"
            "        except Exception as exc:\n"
            "            raise RuntimeError(\n"
            "                f\"selected deployer missing after source ship: {remote_deployer}\"\n"
            "            ) from exc\n"
            "        if shipped_deployer != deployer_source.read_bytes():\n"
            "            raise RuntimeError(\n"
            "                f\"selected deployer differs after source ship: {remote_deployer}\"\n"
            "            )\n\n"
            "    async def _read_pid",
        ),
        (
            "def _host_ale_root() -> Path:\n"
            "    \"\"\"Host's ``ale_run/`` package root.\"\"\"\n"
            "    return Path(__file__).resolve().parents[1]\n",
            "def _host_ale_root_for_deployer(deployer_cls: type[Any]) -> Path:\n"
            "    \"\"\"Return the exact ``ale_run`` root containing a deployer.\"\"\"\n"
            "    module_path = Path(inspect.getfile(deployer_cls)).resolve()\n"
            "    for parent in module_path.parents:\n"
            "        if parent.name == \"ale_run\":\n"
            "            return parent\n"
            "    raise RuntimeError(\n"
            "        f\"deployer is not inside an ale_run tree: {module_path}\"\n"
            "    )\n",
        ),
    )
    for old, new in replacements:
        if source.count(old) != 1:
            raise RuntimeError(
                f"ALE sandbox compatibility patch anchor count != 1: {old!r}"
            )
        source = source.replace(old, new, 1)
    sandbox_path.write_text(source, encoding="utf-8")
    return {
        "name": "sandbox-ship-selected-deployer-tree",
        "purpose": (
            "runner compatibility, scoped agent-asset shipping and pre-launch "
            "source-integrity check only; "
            "no prompt/tool/model behavior change"
        ),
        "upstream_sha256": before_sha,
        "patched_sha256": _sha256(sandbox_path),
    }


def _patch_local_task_data_ownership(ale_root: Path) -> dict[str, str]:
    """Make Docker-copied local task data writable by the sandbox user.

    ``docker cp`` creates destination files as root. ALE's local-data backend
    then asks the unprivileged CUA user to chmod them and some stock setup hooks
    rewrite task-local executable wrappers. Restore the ownership of the
    already user-owned task base after each copy, and fail if the executable
    permission pass does not succeed. This only repairs local Docker staging;
    it does not change task contents or expose references before evaluation.
    """
    path = ale_root / "environments" / "task_data" / "local_host.py"
    before_sha = _sha256(path)
    source = path.read_text(encoding="utf-8")

    helper_anchor = '''async def stage_input(
    sandbox: SandboxHandle, task_data: TaskDataSpec, *, source: str,
) -> dict[str, Any]:
'''
    helper = '''async def _docker_chown_like_parent(
    container: str, path: str, parent: str,
) -> None:
    # docker cp writes as root. Match the task base directory that cua-server
    # created as the runtime user, without hard-coding an image-specific UID.
    script = (
        f'owner="$(stat -c %u:%g {shell_q_linux(parent)})"; '
        f'chown -R "$owner" {shell_q_linux(path)}'
    )
    proc = await asyncio.create_subprocess_exec(
        "docker", "exec", "-u", "root", container, "sh", "-c", script,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"docker ownership restore failed for {container}:{path} "
            f"(rc={proc.returncode}): {err.decode(errors='replace')[:300]}"
        )


def shell_q_linux(value: str) -> str:
    import shlex
    return shlex.quote(value)


async def stage_input(
    sandbox: SandboxHandle, task_data: TaskDataSpec, *, source: str,
) -> dict[str, Any]:
'''
    if source.count(helper_anchor) != 1:
        raise RuntimeError("ALE local ownership helper anchor count != 1")
    source = source.replace(helper_anchor, helper, 1)

    input_old = '''    await _docker_cp(os.path.join(host, "input") + "/.", sandbox.id, in_dst)
    staged = ["input"]
'''
    input_new = '''    await _docker_cp(os.path.join(host, "input") + "/.", sandbox.id, in_dst)
    await _docker_chown_like_parent(sandbox.id, in_dst, base)
    staged = ["input"]
'''
    if source.count(input_old) != 1:
        raise RuntimeError("ALE local input ownership anchor count != 1")
    source = source.replace(input_old, input_new, 1)

    software_old = '''        await _docker_cp(sw + "/.", sandbox.id, sw_dst)
        # mirror baked_in_sandbox: make software wrappers/binaries executable.
        await sandbox.run_command(
            f"find {shell_q(sandbox, join(sandbox, base, 'software'))} "
            f"-type f -exec chmod +x {{}} +",
            timeout=60,
        )
        staged.append("software")
'''
    software_new = '''        await _docker_cp(sw + "/.", sandbox.id, sw_dst)
        await _docker_chown_like_parent(sandbox.id, sw_dst, base)
        # mirror baked_in_sandbox: make software wrappers/binaries executable.
        chmod = await sandbox.run_command(
            f"find {shell_q(sandbox, join(sandbox, base, 'software'))} "
            f"-type f -exec chmod +x {{}} +",
            timeout=60,
        )
        if chmod.returncode != 0:
            raise RuntimeError(
                f"task software chmod failed (rc={chmod.returncode}): "
                f"{(chmod.stderr or '')[:300]}"
            )
        staged.append("software")
'''
    if source.count(software_old) != 1:
        raise RuntimeError("ALE local software ownership anchor count != 1")
    source = source.replace(software_old, software_new, 1)

    reference_old = '''    await _docker_cp(ref, sandbox.id, target)
    logger.info("local: staged reference %s -> %s:%s", ref, sandbox.id, target)
'''
    reference_new = '''    await _docker_cp(ref, sandbox.id, target)
    # References are copied only after the agent exits, immediately before the
    # verifier. They need the same ownership repair as input/software because
    # stock evaluators may chmod or execute files inside the reference tree.
    await _docker_chown_like_parent(sandbox.id, target, base)
    logger.info("local: staged reference %s -> %s:%s", ref, sandbox.id, target)
'''
    if source.count(reference_old) != 1:
        raise RuntimeError("ALE local reference ownership anchor count != 1")
    source = source.replace(reference_old, reference_new, 1)
    path.write_text(source, encoding="utf-8")
    return {
        "name": "local-docker-task-data-ownership",
        "purpose": (
            "benchmark-runner staging compatibility only; preserve task data "
            "contents while restoring the sandbox user's ownership"
        ),
        "upstream_sha256": before_sha,
        "patched_sha256": _sha256(path),
    }


def _patch_openjiuwen_ale_runtime(ale_root: Path) -> list[dict[str, str]]:
    """Add a read-only runtime mount and expose ALE's exact launch deadline.

    Both changes are scoped to an openJiuwen-derived ALE tree.  The mount only
    makes the pinned interpreter/dependencies available; the timeout variable
    lets the deployer use the same per-task execution deadline ALE enforces.
    """
    docker_path = ale_root / "environments" / "providers" / "docker.py"
    before_docker = _sha256(docker_path)
    source = docker_path.read_text(encoding="utf-8")
    replacements = (
        (
            "from dataclasses import dataclass\n",
            "from dataclasses import dataclass\nfrom pathlib import Path\n",
        ),
        (
            "    enable_dind: bool = False\n",
            "    enable_dind: bool = False\n"
            "    # Compatibility-only, read-only runtime mount used by the\n"
            "    # openJiuwen study deployer. Empty for every other path.\n"
            "    openjiuwen_runtime_host_path: str = \"\"\n",
        ),
        (
            "        enable_dind=bool(raw.get(\"enable_dind\") or False),\n",
            "        enable_dind=bool(raw.get(\"enable_dind\") or False),\n"
            "        openjiuwen_runtime_host_path=str(\n"
            "            Path(raw[\"openjiuwen_runtime_host_path\"]).expanduser().resolve()\n"
            "        ) if raw.get(\"openjiuwen_runtime_host_path\") else \"\",\n",
        ),
        (
            "        if self._cfg.privileged:\n",
            "        if self._cfg.openjiuwen_runtime_host_path:\n"
            "            runtime = Path(self._cfg.openjiuwen_runtime_host_path)\n"
            "            if not runtime.is_dir() or not (runtime / \"runtime.json\").is_file():\n"
            "                raise RuntimeError(\n"
            "                    f\"openJiuwen runtime mount is invalid: {runtime}\"\n"
            "                )\n"
            "            if ',' in str(runtime):\n"
            "                raise RuntimeError(\"openJiuwen runtime path cannot contain a comma\")\n"
            "            run_args.extend([\n"
            "                \"--mount\",\n"
            "                f\"type=bind,src={runtime},dst=/opt/openjiuwen-runtime,readonly\",\n"
            "            ])\n"
            "        if self._cfg.privileged:\n",
        ),
    )
    for old, new in replacements:
        if source.count(old) != 1:
            raise RuntimeError(f"openJiuwen Docker mount patch anchor count != 1: {old!r}")
        source = source.replace(old, new, 1)
    docker_path.write_text(source, encoding="utf-8")

    entry_path = ale_root / "executors" / "_sandbox_entry.py"
    before_entry = _sha256(entry_path)
    source = entry_path.read_text(encoding="utf-8")
    old = "    for k, v in env.items():\n        os.environ[str(k)] = str(v)\n\n    try:\n"
    new = (
        "    for k, v in env.items():\n"
        "        os.environ[str(k)] = str(v)\n"
        "    # Compatibility-only input for openJiuwen's runner deadline.\n"
        "    # The prompt-level RuntimeBudgetRail is disabled. This is the same\n"
        "    # launch timeout enforced below by wait_for.\n"
        "    os.environ[\"ALE_EPISODE_TIMEOUT_SECONDS\"] = str(\n"
        "        float(spec.get(\"timeout_s\") or 1800.0)\n"
        "    )\n\n"
        "    try:\n"
    )
    if source.count(old) != 1:
        raise RuntimeError("openJiuwen ALE timeout patch anchor count != 1")
    entry_path.write_text(source.replace(old, new, 1), encoding="utf-8")
    sandbox_path = ale_root / "executors" / "sandbox.py"
    before_sandbox = _sha256(sandbox_path)
    source = sandbox_path.read_text(encoding="utf-8")
    old = '            "agents/*/*.mjs",\n'
    new = (
        '            "agents/*/*.mjs",\n'
        '            "agents/*/*.lock",\n'
    )
    if source.count(old) != 1:
        raise RuntimeError("openJiuwen lock shipping patch anchor count != 1")
    sandbox_path.write_text(source.replace(old, new, 1), encoding="utf-8")
    return [
        {
            "name": "openjiuwen-readonly-runtime-mount",
            "purpose": "dependency staging only; no prompt/tool/model/agent-loop change",
            "upstream_sha256": before_docker,
            "patched_sha256": _sha256(docker_path),
        },
        {
            "name": "openjiuwen-exact-ale-deadline-export",
            "purpose": "feed ALE's enforced per-task launch timeout to the runner",
            "upstream_sha256": before_entry,
            "patched_sha256": _sha256(entry_path),
        },
        {
            "name": "openjiuwen-runtime-lock-source-shipping",
            "purpose": "ship the frozen dependency lock for in-sandbox integrity verification",
            "upstream_sha256": before_sandbox,
            "patched_sha256": _sha256(sandbox_path),
        },
    ]


def _patch_openjiuwen_docker_infrastructure(ale_root: Path) -> dict[str, str]:
    """Harden ALE's local Docker lifecycle without changing the agent.

    The pinned Ubuntu export contains ``/home/user/.cache`` as root:root 0700,
    although the benchmark user is UID 1000.  At least one stock ALE setup
    invokes ``uv`` before the agent starts, so the malformed HOME cache turns a
    valid trial into a pre-model infrastructure failure.  Restore ownership to
    the owner of ``/home/user`` after CUA becomes ready.

    Large local batches also showed that the stock 120 second CUA cold-start
    deadline is too short under CPU contention.  Extending readiness waiting
    to 300 seconds changes only provisioning tolerance; it does not change the
    task deadline, prompt, model calls, tools, or agent loop.
    """
    docker_path = ale_root / "environments" / "providers" / "docker.py"
    before_sha = _sha256(docker_path)
    source = docker_path.read_text(encoding="utf-8")

    timeout_old = "_CUA_READY_TIMEOUT = 120\n"
    timeout_new = "_CUA_READY_TIMEOUT = 300\n"
    if source.count(timeout_old) != 1:
        raise RuntimeError("openJiuwen CUA readiness timeout anchor count != 1")
    source = source.replace(timeout_old, timeout_new, 1)

    ready_old = '''        gcs_user_project = ""
        if self._cfg.gcs_sa_key:
'''
    ready_new = '''        # The pinned Ubuntu export ships /home/user/.cache as root:root
        # 0700. Repair this image-packaging defect before stock task setup or
        # the evaluated agent runs. Derive the owner from the image home rather
        # than assuming a UID/GID.
        home_dir = str(Path(image.work_dir_base).parent)
        cache_dir = str(Path(home_dir) / ".cache")
        cache_script = (
            f'mkdir -p "{cache_dir}/uv" && '
            f'owner="$(stat -c %u:%g "{home_dir}")" && '
            f'chown -R "$owner" "{cache_dir}"'
        )
        rc, _, stderr = await _run_docker(
            "exec", "-u", "root", name, "sh", "-c", cache_script,
        )
        if rc != 0:
            await _run_docker("rm", "-f", name)
            raise RuntimeError(
                f"failed to repair sandbox user cache in {name}: {stderr}"
            )

        gcs_user_project = ""
        if self._cfg.gcs_sa_key:
'''
    if source.count(ready_old) != 1:
        raise RuntimeError("openJiuwen Docker cache repair anchor count != 1")
    docker_path.write_text(source.replace(ready_old, ready_new, 1), encoding="utf-8")
    return {
        "name": "openjiuwen-local-docker-readiness-and-home-cache",
        "purpose": (
            "sandbox infrastructure compatibility only; extend CUA cold-start "
            "readiness and restore the benchmark user's HOME cache ownership; "
            "no prompt/tool/model/agent-loop change"
        ),
        "upstream_sha256": before_sha,
        "patched_sha256": _sha256(docker_path),
    }


def _patch_cua_bench_task_session_compatibility(ale_root: Path) -> dict[str, str]:
    """Restore the command contract promised by cua-bench's public protocol.

    cua-bench 0.2.7 declares ``DesktopSession.run_command(..., timeout=...)``
    returning a ``CommandResult``, but its concrete ``RemoteDesktopSession``
    omits the keyword and returns a plain dictionary. Stock ALE tasks use both
    the documented attribute form and the concrete mapping form. Adapt the
    concrete session at the ALE task boundary, preserve both access forms, and
    enforce the requested timeout with ``asyncio.wait_for``. The ALE detached
    evaluator also returns ``computer.interface.models.CommandResult``, whose
    exit-code attribute is named ``returncode`` while RemoteDesktopSession
    looks for ``return_code``; publish both aliases so failures are not silently
    reported as exit code zero. None of this touches the evaluated agent's
    tools, prompt, model requests, or loop.
    """
    driver_path = ale_root / "tasks" / "driver.py"
    before_sha = _sha256(driver_path)
    source = driver_path.read_text(encoding="utf-8")
    old = """    def _install_resilient(self, session: RemoteDesktopSession) -> None:\n        try:\n"""
    new = """    def _install_resilient(self, session: RemoteDesktopSession) -> None:\n        # cua-bench 0.2.7's DesktopSession protocol advertises ``timeout`` and\n        # a CommandResult, but RemoteDesktopSession omits the keyword and returns\n        # a dict. Stock ALE tasks use both attribute and mapping access.\n        current_run_command = session.run_command\n        if not getattr(current_run_command, \"_ale_command_compatible\", False):\n            accepts_timeout = (\n                \"timeout\" in inspect.signature(current_run_command).parameters\n            )\n\n            async def compatible_run_command(\n                command: str, *, timeout: float | None = None, check: bool = True\n            ):\n                if accepts_timeout:\n                    pending = current_run_command(\n                        command, timeout=timeout, check=check\n                    )\n                else:\n                    pending = current_run_command(command, check=check)\n                if timeout is None or accepts_timeout:\n                    result = await pending\n                else:\n                    result = await asyncio.wait_for(pending, timeout=float(timeout))\n                return _TaskCommandResult.from_result(result)\n\n            compatible_run_command._ale_command_compatible = True\n            session.run_command = compatible_run_command\n        try:\n"""
    if source.count(old) != 1:
        raise RuntimeError("ALE cua-bench task-session patch anchor count != 1")
    import_old = "import asyncio\nimport logging\n"
    import_new = "import asyncio\nimport inspect\nimport logging\n"
    if source.count(import_old) != 1:
        raise RuntimeError("ALE cua-bench inspect import patch anchor count != 1")
    result_class_anchor = "logger = logging.getLogger(__name__)\n"
    result_class = '''class _TaskCommandResult(dict):
    """Mapping-compatible command result with the documented attributes."""

    @classmethod
    def from_result(cls, result):
        if isinstance(result, dict):
            values = dict(result)
            stdout = values.get("stdout", values.get("output", ""))
            stderr = values.get("stderr", "")
            returncode = values.get(
                "return_code",
                values.get("returncode", values.get("exit_code", values.get("code", 0))),
            )
        else:
            values = {}
            stdout = getattr(result, "stdout", getattr(result, "output", ""))
            stderr = getattr(result, "stderr", "")
            returncode = getattr(
                result,
                "return_code",
                getattr(result, "returncode", getattr(result, "exit_code", 0)),
            )
        values.update(
            stdout="" if stdout is None else stdout,
            stderr="" if stderr is None else stderr,
            return_code=int(returncode),
            returncode=int(returncode),
        )
        values.setdefault("success", int(returncode) == 0)
        return cls(values)

    @property
    def stdout(self):
        return self["stdout"]

    @property
    def stderr(self):
        return self["stderr"]

    @property
    def returncode(self):
        return self["returncode"]

    @property
    def return_code(self):
        return self["return_code"]


logger = logging.getLogger(__name__)
'''
    if source.count(result_class_anchor) != 1:
        raise RuntimeError("ALE cua-bench result-class patch anchor count != 1")
    detached_old = "    return CommandResult(stdout=out, stderr=err, returncode=rc)\n"
    detached_new = (
        "    result = CommandResult(stdout=out, stderr=err, returncode=rc)\n"
        "    # RemoteDesktopSession looks for return_code, whereas the pinned\n"
        "    # computer package names the dataclass field returncode.\n"
        "    result.return_code = rc\n"
        "    return result\n"
    )
    if source.count(detached_old) != 1:
        raise RuntimeError("ALE detached exit-code alias patch anchor count != 1")
    source = (
        source.replace(import_old, import_new, 1)
        .replace(result_class_anchor, result_class, 1)
        .replace(detached_old, detached_new, 1)
        .replace(old, new, 1)
    )
    driver_path.write_text(source, encoding="utf-8")
    return {
        "name": "cua-bench-run-command-contract-compatibility",
        "purpose": (
            "benchmark-runner API compatibility only; restores the public "
            "DesktopSession timeout and CommandResult contract without "
            "changing agent behavior"
        ),
        "upstream_sha256": before_sha,
        "patched_sha256": _sha256(driver_path),
    }


def prepare_agent_source(
    config: dict[str, Any], output: Path, harness_ids: list[str]
) -> dict[str, Any]:
    """Create one immutable ALE source tree with requested thin deployers."""
    unknown = sorted(set(harness_ids) - set(ALE_AGENT_CLASSES))
    if unknown:
        raise ValueError(f"ALE study deployer not implemented: {', '.join(unknown)}")
    if output.exists():
        raise FileExistsError(f"refusing to overwrite derived source: {output}")
    repository = ROOT / config["runners"]["ale"]["repository_path"]
    output.mkdir(parents=True)
    ignored = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".pytest_cache")
    shutil.copytree(repository / "ale_run", output / "ale_run", ignore=ignored)
    sandbox_patch = _patch_sandbox_shipping(
        output / "ale_run" / "executors" / "sandbox.py"
    )
    compatibility_patches = [sandbox_patch]
    compatibility_patches.append(
        _patch_local_task_data_ownership(output / "ale_run")
    )
    compatibility_patches.append(
        _patch_cua_bench_task_session_compatibility(output / "ale_run")
    )
    if "openjiuwen" in harness_ids:
        compatibility_patches.extend(
            _patch_openjiuwen_ale_runtime(output / "ale_run")
        )
        compatibility_patches.append(
            _patch_openjiuwen_docker_infrastructure(output / "ale_run")
        )
    overlays: dict[str, Any] = {}
    for harness_id in harness_ids:
        package, class_name = ALE_AGENT_CLASSES[harness_id]
        source = ALE_AGENT_OVERLAYS / package
        destination = output / "ale_run" / "agents" / package
        shutil.copytree(source, destination, ignore=ignored)
        materialized_files: dict[str, str] = {}
        if harness_id == "openhands":
            patch_destination = destination / "reasoning_patch.py"
            shutil.copy2(OPENHANDS_REASONING_PATCH, patch_destination)
            materialized_files["reasoning_patch.py"] = _sha256(
                OPENHANDS_REASONING_PATCH
            )
        if harness_id == "deepseek-harness":
            dsh_patch_destination = destination / "dsh-headless-standard.patch"
            shutil.copy2(DSH_STANDARD_PATCH, dsh_patch_destination)
            materialized_files["dsh-headless-standard.patch"] = _sha256(
                DSH_STANDARD_PATCH
            )
            injector_destination = destination / "openrouter-body-injector.mjs"
            shutil.copy2(OPENROUTER_BODY_INJECTOR, injector_destination)
            materialized_files["openrouter-body-injector.mjs"] = _sha256(
                OPENROUTER_BODY_INJECTOR
            )
        if harness_id == "openjiuwen":
            runner_destination = destination / "openjiuwen_agent.py"
            shutil.copy2(OPENJIUWEN_RUNNER, runner_destination)
            materialized_files["openjiuwen_agent.py"] = _sha256(OPENJIUWEN_RUNNER)
            evaluator_injector = destination / "evaluator-openrouter-injector.py"
            shutil.copy2(ALE_EVALUATOR_INJECTOR, evaluator_injector)
            materialized_files["evaluator-openrouter-injector.py"] = _sha256(
                ALE_EVALUATOR_INJECTOR
            )
            lock_destination = destination / "openjiuwen-runtime.lock"
            shutil.copy2(OPENJIUWEN_RUNTIME_LOCK, lock_destination)
            materialized_files["openjiuwen-runtime.lock"] = _sha256(
                OPENJIUWEN_RUNTIME_LOCK
            )
        overlays[harness_id] = {
            "package": package,
            "class": class_name,
            "source_sha256": _tree_sha256(source),
            "materialized_files": materialized_files,
        }
    manifest = {
        "schema_version": 1,
        "upstream_repository": str(repository),
        "upstream_commit": config["runners"]["ale"]["commit"],
        "harnesses": harness_ids,
        "overlays": overlays,
        "compatibility_patches": compatibility_patches,
        "ale_run_tree_sha256": _tree_sha256(output / "ale_run"),
    }
    manifest_path = output / "derived-source-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    # Freeze files against accidental in-place edits while leaving directories
    # removable by cleanup tools.  Immutability is ultimately enforced by the
    # no-overwrite rule plus the recorded tree hash.
    for path in output.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o444)
    return manifest


def _write_yaml(path: Path, payload: Any) -> None:
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def prepare_canary(
    config: dict[str, Any],
    output: Path,
    *,
    pi_remaining: bool = False,
    codex_only: bool = False,
    pi_modal: bool = False,
) -> dict[str, Any]:
    """Freeze the first paid ALE lifecycle candidate without launching it."""
    if sum(bool(item) for item in (pi_remaining, codex_only, pi_modal)) > 1:
        raise ValueError("pi_remaining, codex_only and pi_modal are mutually exclusive")
    if output.exists():
        raise FileExistsError(f"refusing to overwrite canary directory: {output}")
    repository = ROOT / config["runners"]["ale"]["repository_path"]
    task_data_root = repository / "task-data"
    selected_task = ALE_PI_MODAL_TASK if pi_modal else ALE_CANARY_TASK
    task_variant_root = task_data_root / selected_task / "base"
    for required_dir in ("input", "reference"):
        candidate = task_variant_root / required_dir
        if not candidate.is_dir():
            raise FileNotFoundError(
                f"ALE Docker task data missing: {candidate}; request gated "
                "Hugging Face access and run scripts/fetch_task_data.sh first"
            )
    output.mkdir(parents=True)
    configs_dir = output / "configs"
    configs_dir.mkdir()
    harnesses = (
        ["codex"]
        if codex_only
        else (["pi"] if pi_remaining or pi_modal else ["claude-code", "pi"])
    )
    source_manifest = prepare_agent_source(config, output / "source", harnesses)
    root_env = ROOT / ".env"
    secret_reference = os.path.relpath(root_env, configs_dir)
    task_list = configs_dir / "tasks.txt"
    task_list.write_text(selected_task + "\n", encoding="utf-8")

    cc_agent = {
        "class": ALE_AGENT_CLASSES["claude-code"][1],
        "id": "claude-code--claude-opus-5",
        "model": "anthropic/claude-opus-5[1m]",
        "executor": "sandbox",
        "config": {
            "provider": "openrouter",
            "api_key": None,
            "cli_version": "@anthropic-ai/claude-code@2.1.251",
            "effort_level": "high",
            "max_thinking_tokens": None,
            "context_tokens": 1_000_000,
            "max_output_tokens": 64_000,
            "max_turns": -1,
            "max_budget_usd": None,
            "dangerously_skip_permissions": True,
            "otel_enabled": True,
        },
    }
    pi_agent = {
        "class": ALE_AGENT_CLASSES["pi"][1],
        "id": "pi--glm-5.3",
        "model": "z-ai/glm-5.3",
        "executor": "sandbox",
        "config": {
            "provider": "openrouter",
            "api_key": None,
            "cli_version": "0.84.4",
            "thinking": "high",
        },
    }
    codex_agent = {
        "class": ALE_AGENT_CLASSES["codex"][1],
        "id": "codex--gpt-6-astra",
        "model": "gpt-6-astra",
        "executor": "sandbox",
        "config": {
            "provider": "openrouter",
            "base_url": "http://127.0.0.1:4170/v1",
            "reasoning_effort": "high",
            "codex_version": "0.150.1",
            "patched_binary_url": "",
            "patched_binary_url_windows": "",
            "fork_version": "0.150.1",
            "model_catalog_path": "",
            "model_catalog_content": "",
            "feature_overrides": {},
            "otel_enabled": True,
            "injector_port": 4170,
            "injector_upstream": "https://openrouter.ai/api",
            "upstream_model": "openai/gpt-6-astra",
            "provider_only": ["openai"],
            "allow_fallbacks": False,
            "require_parameters": False,
        },
    }
    campaign_id = (
        ALE_CODEX_CAMPAIGN
        if codex_only
        else (
            ALE_PI_MODAL_CAMPAIGN
            if pi_modal
            else (ALE_PI_FOUR_MODEL_CAMPAIGN if pi_remaining else ALE_CANARY_CAMPAIGN)
        )
    )
    model_configs = {item["id"]: item for item in config["models"]}
    if codex_only:
        agent_payloads = [("codex.yaml", codex_agent)]
    elif pi_remaining or pi_modal:
        agent_payloads = []
        selected_models = ALE_PI_ALL_MODELS if pi_modal else ALE_PI_REMAINING_MODELS
        for model_id in selected_models:
            model = model_configs[model_id]
            slug = model["openrouter"]["model_id"]
            agent_payloads.append((
                f"pi--{model_id}.yaml",
                {
                    "class": ALE_AGENT_CLASSES["pi"][1],
                    "id": f"pi--{model_id}",
                    "model": slug,
                    "executor": "sandbox",
                    "config": {
                        "provider": "openrouter",
                        "api_key": None,
                        "cli_version": "0.84.4",
                        "thinking": "high",
                    },
                },
            ))
    else:
        agent_payloads = [
            ("claude-code.yaml", cc_agent),
            ("pi.yaml", pi_agent),
        ]
    environment = {
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
        "task_data_source": f"local:{task_data_root}",
        "output_path": "local",
    }
    experiment = {
        "name": campaign_id,
        "secret_file": secret_reference,
        "agents": [str((configs_dir / name).resolve()) for name, _ in agent_payloads],
        "environment": str((configs_dir / "environment.yaml").resolve()),
        "tasks": str(task_list.resolve()),
        "output": {"root": str((output / "raw").resolve())},
        "concurrency": 3 if pi_modal else 1,
        "wall_time_s": None,
        "auto_resume": False,
        "max_attempts": 1,
        "cleanup_mode": "delete",
        "prompt_suffix": "",
    }
    for name, payload in (
        *agent_payloads,
        ("environment.yaml", environment),
        ("experiment.yaml", experiment),
    ):
        _write_yaml(configs_dir / name, payload)
    config_hashes = {
        path.name: _sha256(path)
        for path in sorted(configs_dir.iterdir())
        if path.is_file()
    }

    card = json.loads(
        (repository / "tasks" / selected_task / "task_card.json").read_text()
    )
    vm = card["vm"]
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "campaign_id": campaign_id,
        "kind": "paid-real-ale-lifecycle-qualification-pilot-not-for-primary-scores",
        "approval": {
            "required_before_launch": True,
            "approved_manifest_sha256": None,
        },
        "scope": {
            "benchmark": "Agents' Last Exam / ALE-CLI",
            "dataset_commit": config["runners"]["ale"]["commit"],
            "task": selected_task,
            "task_title": card["title"],
            "variant": 0,
            "replicate": 1,
            "planned_trials": (
                1 if codex_only else (5 if pi_modal else (4 if pi_remaining else 2))
            ),
            "primary_scores": False,
        },
        "runner": {
            "name": "ALE",
            "commit": config["runners"]["ale"]["commit"],
            "entry": (
                "cwd=<pinned ALE checkout> "
                "PYTHONPATH=<campaign>/source:<pinned ALE checkout> "
                "<repo>/.venv/bin/python -P -m ale_run run "
                "<campaign>/configs/experiment.yaml --disable-resume"
            ),
            "working_directory": str(repository),
            "host_runtime": {
                "python": "3.13.11",
                "safe_path": (
                    "-P with explicit derived source first and pinned ALE checkout "
                    "second (required for task-local imports)"
                ),
                "sync": "uv sync --frozen",
                "pyproject_sha256": _sha256(ROOT / "pyproject.toml"),
                "uv_lock_sha256": _sha256(ROOT / "uv.lock"),
            },
            "derived_source": source_manifest,
            "experiment_config_sha256": _sha256(configs_dir / "experiment.yaml"),
            "config_files_sha256": config_hashes,
            "study_config_sha256": hashlib.sha256(
                json.dumps(
                    config, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ).encode()
            ).hexdigest(),
            "outer_attempts": 1,
            "auto_resume": False,
            "prompt_suffix": "",
        },
        "cells": {
            "claude-code--claude-opus-5": {
                "harness": "Claude Code",
                "version": "2.1.251",
                "install": "@anthropic-ai/claude-code@2.1.251",
                "model": "anthropic/claude-opus-5[1m]",
                "provider_receives_model": "anthropic/claude-opus-5",
                "provider": "OpenRouter pinned to Anthropic",
                "protocol": "Anthropic Messages; transparent provider-body injector",
                "route": {
                    "only": ["anthropic"],
                    "allow_fallbacks": False,
                    "require_parameters": True,
                },
                "reasoning": "--effort high -> output_config.effort=high",
                "context_tokens": 1_000_000,
                "max_output_tokens": 64_000,
                "max_turns": "unlimited (-1 omits flag)",
                "harness_usd_budget": None,
                "tools": (
                    "Claude Code headless defaults + ALE CUA MCP; ALE upstream "
                    "disallows 8 interactive/session tools"
                ),
                "logs": "native stream-json + OTel WAL/derived telemetry + injector audit",
            },
            "pi--glm-5.3": {
                "harness": "PI",
                "version": "0.84.4",
                "install": "@earendil-works/pi-coding-agent@0.84.4",
                "model": "z-ai/glm-5.3",
                "provider": "OpenRouter pinned to Z.AI fp8",
                "protocol": "PI-native OpenRouter Chat Completions",
                "route": {
                    "only": ["z-ai/fp8"],
                    "quantizations": ["fp8"],
                    "allow_fallbacks": False,
                    "require_parameters": True,
                },
                "reasoning": "--thinking high",
                "catalog_context_tokens": 1_048_576,
                "catalog_max_output_tokens": 131_072,
                "wire_output_field": "max_tokens with PI-native numeric catalog value",
                "max_turns": "PI default",
                "harness_usd_budget": None,
                "tools": "PI native read/bash/edit/write defaults; no skills/extensions/MCP",
                "logs": "complete stdout JSONL + native session JSONL",
            },
        },
        "sandbox": {
            "provider": "ALE DockerProvider",
            "image_ref": ALE_DOCKER_IMAGE,
            "image_manifest_digest": ALE_DOCKER_IMAGE.rsplit("@", 1)[1],
            "snapshot": "cpu-free-ubuntu",
            "machine_type": vm.get("machineType"),
            "cpu": 4,
            "memory_gb": 15,
            "disk": "host-local Docker storage; no per-container disk quota",
            "shm": "2g",
            "resolution": [1024, 768],
            "network": "enabled; required for npm and model API",
            "privileged": False,
            "nested_runtime": False,
            "task_data_source": f"local:{task_data_root}",
            "task_data_variant_sha256": _tree_sha256(task_variant_root),
            "task_data_archive_sha256": _sha256(
                task_data_root / "ale-tasks-data.tar.gz"
            ),
            "task_data_permissions": (
                "local staging normalized: input readable, software executable, "
                "reference grader-readable; file contents unchanged"
            ),
            "output_path": "local",
            "cleanup": "delete",
            "host_telemetry": (
                "cua-computer upstream default PostHog telemetry enabled; "
                "exclude from model/provider request and cost accounting"
            ),
        },
        "timeouts_and_retries": {
            "task_agent_seconds": vm.get("timeout") or 7200,
            "experiment_wall_override": None,
            "evaluation_seconds": 7200,
            "cua_command_attempts": 8,
            "task_session_attempts": 4,
            "claude_code_retries": "native default; audit actual API request count",
            "pi_retries": "native maximum 3 with 2/4/8-second backoff",
            "injector_retries": 0,
            "whole_trial_retries": 0,
        },
        "controls": {
            "concurrency": 3 if pi_modal else 1,
            "temperature": "provider/harness default (field absence must be captured)",
            "top_p": "provider/harness default (field absence must be captured)",
            "seed": None,
            "study_injected_skills": [],
            "harness_default_skills": (
                "preserved; record the runtime inventory and invocation count per trial"
            ),
            "extra_prompts": [],
            "memory": "fresh harness home/session",
            "benchmark_specific_augmentation": False,
            "compaction": "harness native; capture every trigger/outcome",
        },
        "accounting": {
            "primary": "OpenRouter per-generation settled usage/cost/route metadata",
            "secondary": "native harness logs",
            "runner": "ALE normalized trajectory; never add duplicate counters",
            "estimated_total_usd": {"low": 0.05, "high": 5.0},
            "hard_dollar_stop": None,
            "note": "two sequential short-task trials; no safe mid-trial dollar kill",
        },
        "required_post_run_audit": [
            "actual model/provider/quantization/fallback and every generation ID",
            "tool call/result closure and grader validity",
            "reasoning continuity, retries, 429/5xx, timeout and output-cap events",
            "compaction/truncation and recovery",
            "secret absence plus native/runner/provider token-cost reconciliation",
        ],
        "known_difference": (
            "Claude Code inherits ALE's eight-item headless disallowed-tools list; "
            "the first wire capture must determine the effective tool-surface delta"
        ),
    }
    if pi_remaining or pi_modal:
        pi_cells: dict[str, dict[str, Any]] = {}
        provider_labels = {
            "claude-opus-5": "Anthropic",
            "gpt-6-astra": "OpenAI",
            "glm-5.3": "Z.AI fp8",
            "kimi-k3": "Moonshot mxfp4",
            "deepseek-v4-pro": "DeepSeek",
        }
        selected_models = ALE_PI_ALL_MODELS if pi_modal else ALE_PI_REMAINING_MODELS
        for model_id in selected_models:
            model = model_configs[model_id]
            openrouter = model["openrouter"]
            route = {
                "only": openrouter["provider_only"],
                "allow_fallbacks": False,
                "require_parameters": True,
            }
            if quantizations := openrouter.get("quantizations"):
                route["quantizations"] = quantizations
            pi_cells[f"pi--{model_id}"] = {
                "harness": "PI",
                "version": "0.84.4",
                "install": "@earendil-works/pi-coding-agent@0.84.4",
                "model": openrouter["model_id"],
                "provider": f"OpenRouter pinned to {provider_labels[model_id]}",
                "protocol": "PI-native OpenRouter Chat Completions",
                "route": route,
                "reasoning": "--thinking high",
                "catalog_context_tokens": openrouter["endpoint_context_tokens"],
                "catalog_max_output_tokens": openrouter["pi_catalog_max_output_tokens"],
                "wire_output_field": "max_tokens with PI-native numeric catalog value",
                "max_turns": "PI default",
                "harness_usd_budget": None,
                "tools": (
                    "PI native read/bash/edit/write plus the ALE runner-owned 14-tool "
                    "CUA extension; no study skill, memory, MCP, or task prompt"
                ),
                "endpoint_supports_vision": bool(openrouter["supports_vision"]),
                "expected_modality_outcome": (
                    "screenshot image delivery expected"
                    if openrouter["supports_vision"]
                    else "PI omits image for declared text-only model; record modality failure"
                ),
                "logs": "complete stdout JSONL + native session JSONL",
            }
        manifest["cells"] = pi_cells
        if pi_modal:
            manifest["timeouts_and_retries"] = {
                "task_agent_seconds": vm.get("timeout") or 7200,
                "experiment_wall_override": None,
                "evaluation_seconds": 7200,
                "cua_command_attempts": 8,
                "task_session_attempts": 4,
                "pi_model_retries": "native maximum 3 with 2/4/8-second backoff",
                "pi_cua_extension_retries": 0,
                "compatibility_proxy_retries": 0,
                "whole_trial_retries": 0,
            }
            manifest["controls"].update({
                "benchmark_specific_augmentation": (
                    "ALE runner-owned CUA action space only; implemented as a PI "
                    "extension because PI 0.84.4 intentionally has no MCP client"
                ),
                "pi_native_tools": ["read", "bash", "edit", "write"],
                "ale_cua_tools": [
                    "key", "key_down", "key_up", "type", "hold_key",
                    "mouse_move", "click", "drag", "mouse_down", "mouse_up",
                    "scroll", "wait", "screenshot", "cursor_position",
                ],
                "ale_cua_extension_sha256": _sha256(
                    ALE_AGENT_OVERLAYS / "study_pi" / "cua-extension.mjs"
                ),
                "ale_cua_backend": "ALE CUA HTTP POST /cmd with SSE response",
                "study_injected_skills": [],
                "harness_default_skills": [],
                "mcp_servers": [],
                "subagents": False,
                "extra_prompts": [],
            })
            manifest["accounting"]["estimated_total_usd"] = {
                "low": 0.5, "high": 50.0
            }
            manifest["accounting"]["note"] = (
                "five real GUI-task trials at concurrency 3 and provider concurrency 1; "
                "no safe mid-trial dollar kill"
            )
            manifest["required_post_run_audit"].extend([
                "first-request PI tool inventory/schema hash includes exactly 4 native + 14 ALE CUA tools",
                "every screenshot result is image/png and is persisted in native and normalized logs",
                "vision routes receive image blocks after screenshots; text-only routes expose explicit omission/failure",
                "mouse/keyboard commands use normalized [0,1000] coordinates and reach the ALE CUA backend",
            ])
            manifest["known_difference"] = (
                "PI has no native MCP client, so ALE's benchmark-owned CUA action space "
                "is exposed through a thin PI extension using the same backend and schemas; "
                "tool names are PI extension names rather than MCP-prefixed names. The task "
                "requires 168 GUI moves, so score is not a qualification criterion; modality, "
                "tool closure, routing, artifacts and failure class are."
            )
        else:
            manifest["accounting"]["estimated_total_usd"] = {"low": 0.05, "high": 5.0}
            manifest["accounting"]["note"] = (
                "four sequential short-task trials; no safe mid-trial dollar kill"
            )
            manifest["known_difference"] = (
                "single text Docker task qualifies the exact PI model route and terminal "
                "tool loop only; it does not qualify multimodal/CUA or compaction recovery"
            )
    elif codex_only:
        manifest["cells"] = {
            "codex--gpt-6-astra": {
                "harness": "Codex CLI",
                "version": "0.150.1",
                "install": "@openai/codex@0.150.1",
                "model": "gpt-6-astra",
                "provider_receives_model": "openai/gpt-6-astra",
                "provider": "OpenRouter pinned to OpenAI official endpoint",
                "protocol": "OpenAI Responses end to end; no protocol translation",
                "route": {
                    "only": ["openai"],
                    "allow_fallbacks": False,
                    "require_parameters": False,
                },
                "reasoning": "model_reasoning_effort=high; wire reasoning.effort=high required",
                "reasoning_context": "bundled Responses Lite all_turns",
                "catalog_context_tokens": 272_000,
                "catalog_max_context_tokens": 872_000,
                "effective_context_percent": 95,
                "context_override": None,
                "provider_advertised_context_tokens": 1_050_000,
                "provider_advertised_max_output_tokens": 128_000,
                "configured_output_tokens": None,
                "wire_output_field": "max_output_tokens absent",
                "max_turns": "Codex default",
                "harness_usd_budget": None,
                "tools": "stock Codex tools plus ALE runner-native CUA MCP",
                "skills": "stock Codex defaults; no project/study skill injection",
                "logs": "native Codex JSONL + ALE OTel/trajectory + injector audit",
            }
        }
        manifest["timeouts_and_retries"] = {
            "task_agent_seconds": vm.get("timeout") or 7200,
            "experiment_wall_override": None,
            "evaluation_seconds": 7200,
            "cua_command_attempts": 8,
            "task_session_attempts": 4,
            "codex_request_retries": 4,
            "codex_stream_reconnects": 5,
            "injector_retries": 0,
            "whole_trial_retries": 0,
        }
        manifest["controls"].update({
            "benchmark_specific_augmentation": "ALE runner-native CUA MCP only",
            "reasoning_effort": "high",
            "context_override": None,
            "output_override": None,
        })
        manifest["accounting"]["estimated_total_usd"] = {"low": 0.02, "high": 3.0}
        manifest["accounting"]["note"] = (
            "one short-task trial; no safe mid-trial dollar kill; provider generation "
            "metadata is the authoritative cost ledger"
        )
        manifest["known_difference"] = (
            "ALE adds its benchmark-native CUA MCP to stock Codex; this text canary "
            "qualifies discovery and lifecycle but not multimodal/CUA invocation, "
            "long-context compaction recovery, or the six non-Docker ALE tasks"
        )
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    manifest_sha = hashlib.sha256(payload.encode()).hexdigest()
    manifest["manifest_sha256"] = manifest_sha
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    for path in configs_dir.iterdir():
        path.chmod(0o444)
    manifest_path.chmod(0o444)
    return manifest


def prepare_openhands_canary(
    config: dict[str, Any],
    output: Path,
    model_ids: list[str] | None = None,
    concurrency: int = 1,
    task_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Freeze one or more OpenHands real ALE lifecycle candidates."""
    if output.exists():
        raise FileExistsError(f"refusing to overwrite canary directory: {output}")
    selected_model_ids = model_ids or ["glm-5.3"]
    selected_task_ids = task_ids or [ALE_CANARY_TASK]
    if len(set(selected_model_ids)) != len(selected_model_ids):
        raise ValueError("OpenHands ALE model IDs must be unique")
    if len(set(selected_task_ids)) != len(selected_task_ids):
        raise ValueError("OpenHands ALE task IDs must be unique")
    models_by_id = {item["id"]: item for item in config["models"]}
    unknown = sorted(set(selected_model_ids) - set(models_by_id))
    if unknown:
        raise ValueError(f"unknown OpenHands ALE model IDs: {', '.join(unknown)}")
    planned_trials = len(selected_model_ids) * len(selected_task_ids)
    if concurrency < 1 or concurrency > planned_trials:
        raise ValueError("concurrency must be between 1 and the planned trial count")
    repository = ROOT / config["runners"]["ale"]["repository_path"]
    output.mkdir(parents=True)
    canonical_task_data_root = repository / "task-data"
    if len(selected_task_ids) == 1 and selected_task_ids[0] != "demo/seecheck":
        task_data_root = canonical_task_data_root
    else:
        task_data_root = output / "task-data"
        for task_id in selected_task_ids:
            source = canonical_task_data_root / task_id / "base"
            destination = task_data_root / task_id / "base"
            if source.is_dir():
                shutil.copytree(source, destination, copy_function=shutil.copy2)
            elif task_id == "demo/seecheck":
                # Upstream's self-contained vision probe stages no external data,
                # while local_host requires an input directory before setup.
                (destination / "input").mkdir(parents=True)
            else:
                raise FileNotFoundError(f"ALE Docker task data missing: {source}")
    task_variant_roots = {
        task_id: task_data_root / task_id / "base" for task_id in selected_task_ids
    }
    for task_id, task_variant_root in task_variant_roots.items():
        if not (task_variant_root / "input").is_dir():
            raise FileNotFoundError(
                f"ALE Docker task input missing: {task_variant_root / 'input'}"
            )
        if task_id != "demo/seecheck" and not (task_variant_root / "reference").is_dir():
            raise FileNotFoundError(
                f"ALE Docker task reference missing: {task_variant_root / 'reference'}"
            )
    configs_dir = output / "configs"
    configs_dir.mkdir()
    source_manifest = prepare_agent_source(
        config, output / "source", ["openhands"]
    )
    secret_reference = os.path.relpath(ROOT / ".env", configs_dir)
    task_list = configs_dir / "tasks.txt"
    task_list.write_text("\n".join(selected_task_ids) + "\n", encoding="utf-8")

    agent_payloads: list[tuple[str, dict[str, Any]]] = []
    cells: dict[str, dict[str, Any]] = {}
    for model_id in selected_model_ids:
        openrouter = models_by_id[model_id]["openrouter"]
        route = {
            "only": openrouter["provider_only"],
            "allow_fallbacks": False,
            "require_parameters": True,
        }
        if quantizations := openrouter.get("quantizations"):
            route["quantizations"] = quantizations
        agent_payloads.append((
            f"openhands--{model_id}.yaml",
            {
                "class": ALE_AGENT_CLASSES["openhands"][1],
                "id": f"openhands--{model_id}",
                "model": openrouter["model_id"],
                "executor": "sandbox",
                "config": {
                    "provider": "openrouter",
                    "base_url": "https://openrouter.ai/api/v1",
                    "sdk_version": "1.44.1",
                    "tools_version": "1.44.1",
                    "reasoning_effort": "high",
                    "max_input_tokens": 1_000_000,
                    "max_output_tokens": 128_000,
                    "api_mode": "auto",
                    "max_iterations": 500,
                    "temperature": None,
                    "top_p": None,
                    "seed": None,
                    "load_skills": False,
                    "condenser": None,
                    "mcp_policy": "ale-cua-only",
                },
            },
        ))
        cells[model_id] = {
            "harness": "OpenHands Software Agent SDK",
            "version": "1.44.1",
            "install": "openhands-sdk==1.44.1 openhands-tools==1.44.1",
            "install_runtime": (
                "per-trial <work_dir>/.venv with local uv/pip caches; compatibility-only "
                "because the pinned image has /home/user/.cache root:root/0700"
            ),
            "model": openrouter["model_id"],
            "provider": f"OpenRouter pinned to {','.join(openrouter['provider_only'])}",
            "base_url": "https://openrouter.ai/api/v1",
            "protocol": "OpenHands SDK -> LiteLLM -> OpenRouter Chat/Responses",
            "path_kind": "provider-compatible with qualified reasoning replay patch",
            "route": route,
            "reasoning_effort": "high",
            "reasoning_replay": (
                "exclusive: unsigned reasoning.text via reasoning_content; "
                "signed/encrypted/summarized/structured blocks via exact reasoning_details"
            ),
            "context_tokens": 1_000_000,
            "max_output_tokens": 128_000,
            "max_iterations": 500,
            "temperature": None,
            "top_p": None,
            "seed": None,
            "tools": ["terminal", "file_editor", "task_tracker", "finish", "think"],
            "benchmark_mcp": ["cua"],
            "skills": [],
            "memory": False,
            "condenser": None,
            "subagents": False,
            "endpoint_supports_vision": bool(openrouter["supports_vision"]),
            "expected_modality_outcome": (
                "image delivery expected"
                if openrouter["supports_vision"]
                else "declared text-only; image-path failure is modality incompatibility"
            ),
        }
    environment = {
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
        "task_data_source": f"local:{task_data_root}",
        "output_path": "local",
    }
    experiment = {
        "name": output.name,
        "secret_file": secret_reference,
        "agents": [
            str((configs_dir / name).resolve()) for name, _ in agent_payloads
        ],
        "environment": str((configs_dir / "environment.yaml").resolve()),
        "tasks": str(task_list.resolve()),
        "output": {"root": str((output / "raw").resolve())},
        "concurrency": concurrency,
        "wall_time_s": None,
        "auto_resume": False,
        "max_attempts": 1,
        "cleanup_mode": "delete",
        "prompt_suffix": "",
    }
    for name, payload in (
        *agent_payloads,
        ("environment.yaml", environment),
        ("experiment.yaml", experiment),
    ):
        _write_yaml(configs_dir / name, payload)
    config_hashes = {
        path.name: _sha256(path)
        for path in sorted(configs_dir.iterdir())
        if path.is_file()
    }

    cards = {
        task_id: json.loads(
            (repository / "tasks" / task_id / "task_card.json").read_text()
        )
        for task_id in selected_task_ids
    }
    task_controls = {
        task_id: {
            "title": card["title"],
            "snapshot": card["vm"].get("snapshot"),
            "machine_type": card["vm"].get("machineType"),
            "timeout_seconds": card["vm"].get("timeout")
            or card["vm"].get("timeout_s")
            or 7200,
            "task_data_sha256": _tree_sha256(task_variant_roots[task_id]),
        }
        for task_id, card in cards.items()
    }
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "campaign_id": output.name,
        "kind": "paid-real-ale-lifecycle-qualification-pilot-not-for-primary-scores",
        "approval": {"required_before_launch": True},
        "scope": {
            "benchmark": "Agents' Last Exam / ALE-CLI",
            "dataset_commit": config["runners"]["ale"]["commit"],
            "local_scope": (
                "one upstream self-contained CUA/vision probe plus one real task "
                "from the formal 99-task local Docker score set"
                if "demo/seecheck" in selected_task_ids
                else "formal 99-task local Docker score set"
            ),
            "tasks": selected_task_ids,
            "task_controls": task_controls,
            "variant": 0,
            "replicate": 1,
            "planned_agents": len(selected_model_ids),
            "planned_trials": planned_trials,
            "models": selected_model_ids,
            "primary_scores": False,
        },
        "runner": {
            "name": "ALE",
            "commit": config["runners"]["ale"]["commit"],
            "entry": (
                "cwd=<pinned ALE checkout> "
                "PYTHONPATH=<campaign>/source:<pinned ALE checkout> "
                "<repo>/.venv/bin/python -P -m ale_run run "
                "<campaign>/configs/experiment.yaml --disable-resume"
            ),
            "working_directory": str(repository),
            "host_runtime": {
                "python": "3.13.11",
                "safe_path": (
                    "-P with explicit derived source first and pinned ALE checkout "
                    "second (required for task-local imports)"
                ),
                "sync": "uv sync --frozen",
                "pyproject_sha256": _sha256(ROOT / "pyproject.toml"),
                "uv_lock_sha256": _sha256(ROOT / "uv.lock"),
            },
            "derived_source": source_manifest,
            "experiment_config_sha256": _sha256(configs_dir / "experiment.yaml"),
            "config_files_sha256": config_hashes,
            "study_config_sha256": hashlib.sha256(
                json.dumps(
                    config, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ).encode()
            ).hexdigest(),
            "outer_attempts": 1,
            "auto_resume": False,
            "prompt_suffix": "",
        },
        "cells": cells,
        "sandbox": {
            "provider": "ALE DockerProvider",
            "image_ref": ALE_DOCKER_IMAGE,
            "image_manifest_digest": ALE_DOCKER_IMAGE.rsplit("@", 1)[1],
            "snapshot": "cpu-free-ubuntu",
            "machine_type": "per-task; see scope.task_controls",
            "cpu": 4,
            "memory_gb": 15,
            "shm": "2g",
            "resolution": [1024, 768],
            "network": "enabled; required for package install and model API",
            "privileged": False,
            "nested_runtime": False,
            "task_data_source": f"local:{task_data_root}",
            "task_data_variants_sha256": {
                task_id: values["task_data_sha256"]
                for task_id, values in task_controls.items()
            },
            "task_data_archive_sha256": _sha256(
                canonical_task_data_root / "ale-tasks-data.tar.gz"
            ),
            "task_data_permissions": (
                "local staging normalized: input readable, software executable, "
                "reference grader-readable; file contents unchanged"
            ),
            "output_path": "local",
            "cleanup": "delete",
        },
        "timeouts_and_retries": {
            "task_agent_seconds": {
                task_id: values["timeout_seconds"]
                for task_id, values in task_controls.items()
            },
            "experiment_wall_override": None,
            "evaluation_seconds": 7200,
            "cua_command_attempts": 8,
            "task_session_attempts": 4,
            "openhands_llm_timeout_seconds": 300,
            "openhands_llm_attempt_limit": 5,
            "whole_trial_retries": 0,
        },
        "controls": {
            "concurrency": concurrency,
            "benchmark_specific_augmentation": "ALE runner-native CUA MCP only",
            "extra_prompts": [],
            "compaction": "none (SDK condenser=None)",
        },
        "accounting": {
            "primary": "OpenRouter per-generation settled usage/cost/route metadata",
            "secondary": "OpenHands native events/completion logs",
            "runner": "ALE normalized trajectory; never add duplicate counters",
            "estimated_total_usd": {
                "low": round(0.05 * len(selected_model_ids), 2),
                "high": 3.0 * planned_trials,
            },
            "hard_dollar_stop": None,
        },
        "required_post_run_audit": [
            "actual model/provider/quantization/fallback and every generation ID",
            "five SDK tools plus ALE CUA discovery and tool call/result closure",
            "screenshot image block reaches vision-capable models; text-only paths fail visibly",
            "reasoning continuity, retries, 429/5xx, timeout and output-cap events",
            "artifact/grader validity and secret absence",
            "native/runner/provider token-cost reconciliation",
        ],
        "known_limits": [
            "demo/seecheck is an upstream probe outside the formal 99-task score set",
            "cost_optimization_1 qualifies one real ALE image-input shape, not every modality",
            f"{planned_trials} trials at concurrency {concurrency} only qualify this pilot load",
            "the study excludes six upstream tasks unsupported by local Docker",
        ],
    }
    payload = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    manifest["manifest_sha256"] = hashlib.sha256(payload.encode()).hexdigest()
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    for path in configs_dir.iterdir():
        path.chmod(0o444)
    manifest_path.chmod(0o444)
    return manifest


def _ale_variant_zero_metadata(
    repository: Path, task_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """Resolve the exact variant selected by ALE's ``.txt`` task syntax.

    ALE treats every line in a text task list as variant index zero.  The
    corresponding data-directory name is not uniformly ``base``; ask the
    pinned upstream loader instead of guessing from host directory names.
    """
    script = r'''
import json
import os
from ale_run.tasks.loader import TaskLoader

task_id = os.environ["ALE_TASK_ID"]
loaded = TaskLoader(f"tasks/{task_id}").load(0)
task_data = loaded["task_data"]
print(json.dumps({
    "variant_index": 0,
    "domain_name": task_data.domain_name,
    "task_name": task_data.task_name,
    "variant_name": task_data.variant_name,
    "requires_task_data": bool(task_data.requires_task_data),
}, sort_keys=True))
'''
    base_env = os.environ.copy()
    current = base_env.get("PYTHONPATH", "")
    base_env["PYTHONPATH"] = str(repository) + (
        os.pathsep + current if current else ""
    )
    resolved: dict[str, dict[str, Any]] = {}
    # Task modules reuse generic names such as ``score_outputs`` and
    # ``variant_specs``.  Resolve each task in a fresh interpreter so Python's
    # module cache cannot silently bind a later task to an earlier grader.
    for task_id in task_ids:
        env = base_env.copy()
        env["ALE_TASK_ID"] = task_id
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=repository,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"ALE variant-zero resolution failed for {task_id}: "
                + (result.stderr or result.stdout)[-1600:]
            )
        try:
            item = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"ALE variant-zero resolution returned invalid JSON for {task_id}"
            ) from exc
        if not isinstance(item, dict):
            raise RuntimeError(
                f"ALE variant-zero resolution returned invalid metadata for {task_id}"
            )
        resolved[task_id] = item
    return resolved


def prepare_openjiuwen_canary(
    config: dict[str, Any],
    output: Path,
    model_ids: list[str] | None = None,
    concurrency: int = 1,
    task_ids: list[str] | None = None,
    replacement_of: dict[str, str] | None = None,
    estimated_total_usd: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Freeze a real ALE text qualification for the openJiuwen candidate."""
    if output.exists():
        raise FileExistsError(f"refusing to overwrite canary directory: {output}")
    selected_model_ids = model_ids or ["deepseek-v4-pro"]
    selected_task_ids = task_ids or [ALE_CANARY_TASK]
    if len(set(selected_model_ids)) != len(selected_model_ids):
        raise ValueError("openJiuwen ALE model IDs must be unique")
    if len(set(selected_task_ids)) != len(selected_task_ids):
        raise ValueError("openJiuwen ALE task IDs must be unique")
    models_by_id = {item["id"]: item for item in config["models"]}
    models_by_id.update({item["id"]: item for item in OPENJIUWEN_EXTRA_MODELS})
    unknown = sorted(set(selected_model_ids) - set(models_by_id))
    if unknown:
        raise ValueError(f"unknown openJiuwen ALE model IDs: {', '.join(unknown)}")
    planned_trials = len(selected_model_ids) * len(selected_task_ids)
    if concurrency < 1 or concurrency > planned_trials:
        raise ValueError("concurrency must be between 1 and the planned trial count")
    if replacement_of is not None:
        required = {
            "campaign_id", "manifest_sha256", "infrastructure_audit_sha256"
        }
        if set(replacement_of) != required:
            raise ValueError(
                "replacement_of must contain campaign_id, manifest_sha256, "
                "and infrastructure_audit_sha256"
            )
        for field in ("manifest_sha256", "infrastructure_audit_sha256"):
            value = replacement_of[field]
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ValueError(f"replacement_of.{field} must be a lowercase SHA-256")
    if estimated_total_usd is not None:
        required = {"low", "high", "basis"}
        if set(estimated_total_usd) != required:
            raise ValueError("estimated_total_usd must contain low, high, and basis")
        low = float(estimated_total_usd["low"])
        high = float(estimated_total_usd["high"])
        if low < 0 or high < low or not str(estimated_total_usd["basis"]).strip():
            raise ValueError("invalid estimated_total_usd range or basis")

    repository = ROOT / config["runners"]["ale"]["repository_path"]
    task_data_root = repository / "task-data"
    variant_metadata = _ale_variant_zero_metadata(repository, selected_task_ids)
    task_variant_roots: dict[str, Path] = {}
    for task_id in selected_task_ids:
        identity = variant_metadata[task_id]
        variant_name = str(identity.get("variant_name") or "")
        root = task_data_root / task_id / variant_name
        task_variant_roots[task_id] = root
        if not root.is_dir():
            raise FileNotFoundError(f"ALE Docker task data missing: {root}")
        if identity.get("requires_task_data") and not (root / "input").is_dir():
            raise FileNotFoundError(f"ALE Docker task input missing: {root / 'input'}")

    runtime = ROOT / ".cache" / "openjiuwen" / "runtime-0.1.18-r1"
    try:
        runtime_metadata = json.loads(
            (runtime / "runtime.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        runtime_metadata = None
    if not isinstance(runtime_metadata, dict):
        raise FileNotFoundError(
            "frozen openJiuwen runtime is missing; run the zero-cost setup first"
        )
    if runtime_metadata.get("dependency_lock_sha256") != _sha256(
        OPENJIUWEN_RUNTIME_LOCK
    ):
        raise RuntimeError("openJiuwen runtime dependency lock has drifted")

    output.mkdir(parents=True)
    configs_dir = output / "configs"
    configs_dir.mkdir()
    source_manifest = prepare_agent_source(config, output / "source", ["openjiuwen"])
    evaluator_route: dict[str, Any] | None = None
    if "business_finance/pe_screening_memo_1" in selected_task_ids:
        evaluator_route = {
            "model": "openai/gpt-4o-mini",
            "incoming_model": "gpt-4o-mini",
            "provider": {
                "only": ["openai"],
                "allow_fallbacks": False,
                "require_parameters": True,
            },
        }
        (configs_dir / "evaluator-openrouter.json").write_text(
            json.dumps(evaluator_route, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    task_list = configs_dir / "tasks.txt"
    task_list.write_text("\n".join(selected_task_ids) + "\n", encoding="utf-8")

    agent_payloads: list[tuple[str, dict[str, Any]]] = []
    cells: dict[str, dict[str, Any]] = {}
    for model_id in selected_model_ids:
        openrouter = models_by_id[model_id]["openrouter"]
        route = {
            "only": openrouter["provider_only"],
            "allow_fallbacks": False,
            "require_parameters": True,
        }
        if quantizations := openrouter.get("quantizations"):
            route["quantizations"] = quantizations
        agent_payloads.append((
            f"openjiuwen--{model_id}.yaml",
            {
                "class": ALE_AGENT_CLASSES["openjiuwen"][1],
                "id": f"openjiuwen--{model_id}",
                "model": openrouter["model_id"],
                "executor": "sandbox",
                "config": {
                    "provider": "openrouter",
                    "api_key": None,
                    "version": "0.1.18",
                    "runtime_version": "0.1.18-r1",
                    "reasoning_effort": "high",
                    "runtime_budget_rail_enabled": False,
                    "context_compression_enabled": True,
                    "max_outer_rounds": 8,
                    "prompt_language": "en",
                    "llm_request_timeout_seconds": 360,
                    "llm_stream_first_chunk_timeout_seconds": 300,
                    "llm_stream_idle_timeout_seconds": 300,
                    "llm_http_max_retries": 5,
                    "mcp_policy": "ale-cua-only",
                },
            },
        ))
        cells[model_id] = {
            "harness": "openJiuwen Coding Agent (study Rails composition)",
            "version": "0.1.18",
            "model": openrouter["model_id"],
            "provider": f"OpenRouter pinned to {','.join(openrouter['provider_only'])}",
            "protocol": "native openJiuwen OpenAI Chat Completions client",
            "route": route,
            "reasoning_effort": "high",
            "context_tokens": openrouter["endpoint_context_tokens"],
            "max_output_tokens_wire": None,
            "temperature": None,
            "top_p": None,
            "seed": None,
            "tools": [
                "read_file", "write_file", "edit_file", "glob",
                "list_files", "grep", "bash",
            ],
            "workspace_root": "/",
            "workspace_boundary": "the isolated ALE task container",
            "benchmark_mcp": ["cua"],
            "rails": [
                "SysOperationRail", "ContextProcessorRail(preset=True)",
                "ConfirmedCompletionRail", "AuditRail",
                "SecurityRail", "ModelAnomalyDetectionRail",
            ],
            "skills": [],
            "memory": False,
            "subagents": False,
            "task_planning": False,
            "parallel_tool_calls": True,
            "max_inner_turns": None,
            "max_inner_turns_source": "openJiuwen 0.1.18 native task loop; no study override",
            "context_compression": {
                "enabled": True,
                "preset": "openjiuwen-0.1.18-native",
                "whole_context_trigger_ratio": 0.8,
                "single_tool_result_offload_ratio": 0.1,
                "session_memory_enabled": False,
                "compression_recall_enabled": False,
                "context_debug_enabled": True,
            },
        }

    environment = {
        "snapshots": {"cpu-free-ubuntu": {
            "provider": "docker",
            "image": "ale-ubuntu22-docker",
            "docker": {
                "image_ref": ALE_DOCKER_IMAGE,
                "shm_size": "2g",
                "resolution": [1024, 768],
                "privileged": False,
                "enable_dind": False,
                "openjiuwen_runtime_host_path": str(runtime.resolve()),
            },
        }},
        "task_data_source": f"local:{task_data_root}",
        "output_path": "local",
    }
    experiment = {
        "name": output.name,
        "secret_file": os.path.relpath(ROOT / ".env", configs_dir),
        "agents": [str((configs_dir / name).resolve()) for name, _ in agent_payloads],
        "environment": str((configs_dir / "environment.yaml").resolve()),
        "tasks": str(task_list.resolve()),
        "output": {"root": str((output / "raw").resolve())},
        "concurrency": concurrency,
        "wall_time_s": None,
        "auto_resume": False,
        "max_attempts": 1,
        "cleanup_mode": "delete",
        "prompt_suffix": "",
    }
    for name, payload in (
        *agent_payloads,
        ("environment.yaml", environment),
        ("experiment.yaml", experiment),
    ):
        _write_yaml(configs_dir / name, payload)
    config_hashes = {
        path.name: _sha256(path)
        for path in sorted(configs_dir.iterdir()) if path.is_file()
    }
    cards = {
        task_id: json.loads(
            (repository / "tasks" / task_id / "task_card.json").read_text()
        )
        for task_id in selected_task_ids
    }
    task_controls = {
        task_id: {
            "title": (
                card.get("title")
                or card.get("task_name")
                or card.get("taskName")
                or card.get("summary")
                or task_id
            ),
            **variant_metadata[task_id],
            "snapshot": card["vm"].get("snapshot"),
            "machine_type": card["vm"].get("machineType"),
            "timeout_seconds": card["vm"].get("timeout")
            or card["vm"].get("timeout_s") or 7200,
            "task_data_sha256": _tree_sha256(task_variant_roots[task_id]),
        }
        for task_id, card in cards.items()
    }
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "campaign_id": output.name,
        "kind": "paid-real-ale-openjiuwen-qualification-pilot-not-for-primary-scores",
        "approval": {"required_before_launch": True},
        "replacement_of": replacement_of,
        "scope": {
            "benchmark": "Agents' Last Exam / ALE-CLI",
            "dataset_commit": config["runners"]["ale"]["commit"],
            "local_scope": "formal 99-task local Docker score set",
            "tasks": selected_task_ids,
            "task_controls": task_controls,
            "variant": 0,
            "replicate": 1,
            "planned_agents": len(selected_model_ids),
            "planned_trials": planned_trials,
            "models": selected_model_ids,
            "primary_scores": False,
        },
        "runner": {
            "name": "ALE",
            "commit": config["runners"]["ale"]["commit"],
            "entry": "PYTHONPATH=<derived>:<ALE> .venv/bin/python -P -m ale_run run <experiment> --disable-resume",
            "working_directory": str(repository),
            "host_runtime": {
                "python": "3.13.11",
                "safe_path": "-P with derived source then pinned ALE checkout",
                "sync": "uv sync --frozen",
                "pyproject_sha256": _sha256(ROOT / "pyproject.toml"),
                "uv_lock_sha256": _sha256(ROOT / "uv.lock"),
            },
            "derived_source": source_manifest,
            "experiment_config_sha256": _sha256(configs_dir / "experiment.yaml"),
            "config_files_sha256": config_hashes,
            "study_config_sha256": hashlib.sha256(
                json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "outer_attempts": 1,
            "auto_resume": False,
            "prompt_suffix": "",
        },
        "cells": cells,
        "sandbox": {
            "provider": "ALE DockerProvider with compatibility-only read-only runtime bind",
            "image_ref": ALE_DOCKER_IMAGE,
            "image_manifest_digest": ALE_DOCKER_IMAGE.rsplit("@", 1)[1],
            "snapshot": "cpu-free-ubuntu",
            "machine_type": "per-task; see scope.task_controls",
            "cpu": 4,
            "memory_gb": 15,
            "shm": "2g",
            "resolution": [1024, 768],
            "network": (
                "enabled for model API; no OpenJiuwen/Python package installation; "
                "ALE may ensure its pinned CUA Node dependencies"
            ),
            "privileged": False,
            "nested_runtime": False,
            "cua_readiness_timeout_seconds": 300,
            "user_home_cache_ownership_repair": {
                "path": "/home/user/.cache",
                "when": "after CUA readiness and before task staging/agent launch",
                "owner_source": "actual UID:GID of /home/user inside the pinned image",
                "purpose": "repair pinned-image root:root 0700 packaging defect",
            },
            "openjiuwen_runtime": {
                "host_path": str(runtime.resolve()),
                "container_path": "/opt/openjiuwen-runtime",
                "mount": "read-only",
                "metadata": runtime_metadata,
            },
            "task_data_source": f"local:{task_data_root}",
            "task_data_variants_sha256": {
                task_id: values["task_data_sha256"]
                for task_id, values in task_controls.items()
            },
            "task_data_archive_sha256": _sha256(
                task_data_root / "ale-tasks-data.tar.gz"
            ),
            "task_data_permissions": "container-readable/executable; contents unchanged",
            "output_path": "local",
            "cleanup": "delete",
        },
        "timeouts_and_retries": {
            "task_agent_seconds": {
                task_id: values["timeout_seconds"]
                for task_id, values in task_controls.items()
            },
            "runtime_budget_source": "exact ALE enforced per-task launch timeout",
            "runtime_budget_prompt_rail_enabled": False,
            "experiment_wall_override": None,
            "evaluation_seconds": 7200,
            "openjiuwen_llm_total_seconds": 360,
            "openjiuwen_stream_first_chunk_seconds": 300,
            "openjiuwen_stream_idle_seconds": 300,
            "openjiuwen_http_max_retries": 5,
            "openjiuwen_model_anomaly_whole_call_max_retries": 2,
            "openjiuwen_model_anomaly_backoff_seconds": [0.5, 1.0],
            "native_tool_timeout_seconds": 300,
            "tool_resilience": (
                "up to 3 attempts for retry-safe transient tool errors; "
                "non-idempotent write/shell calls are not retried"
            ),
            "compatibility_proxy_retries": 0,
            "whole_trial_retries": 0,
        },
        "controls": {
            "concurrency": concurrency,
            "benchmark_specific_augmentation": "ALE runner-native CUA MCP only",
            "filesystem_boundary": (
                "OpenJiuwen restrict_to_work_dir remains enabled with workspace=/; "
                "the isolated ALE task container is the boundary because ALE prompts "
                "use absolute benchmark paths"
            ),
            "completion_confirmations": 2,
            "max_outer_rounds": 8,
            "runtime_budget_prompt_rail_enabled": False,
            "extra_prompts": [],
            "compaction": {
                "enabled": True,
                "preset": "openjiuwen-0.1.18-native",
                "whole_context_trigger_ratio": 0.8,
                "single_tool_result_offload_ratio": 0.1,
                "session_memory_enabled": False,
                "compression_recall_enabled": False,
                "context_debug_enabled": True,
            },
        },
        "accounting": {
            "primary": "OpenRouter per-generation settled usage/cost/route metadata",
            "secondary": "openJiuwen native usage and early generation IDs",
            "runner": "ALE normalized trajectory; never add duplicate counters",
            "evaluator": (
                {
                    "task": "business_finance/pe_screening_memo_1",
                    "model": "openai/gpt-4o-mini",
                    "provider": "OpenRouter pinned to OpenAI",
                    "protocol": "OpenAI Chat Completions through request-only injector",
                    "route": evaluator_route["provider"],
                    "incoming_model": evaluator_route["incoming_model"],
                    "config_sha256": _sha256(
                        configs_dir / "evaluator-openrouter.json"
                    ),
                    "injector_sha256": _sha256(ALE_EVALUATOR_INJECTOR),
                    "cost_scope": "evaluator-only; keep separate from agent cost",
                }
                if evaluator_route is not None else None
            ),
            "estimated_total_usd": estimated_total_usd or (
                {
                    "low": 3.0 * planned_trials,
                    "high": 10.0 * planned_trials,
                    "basis": (
                        "Claude recovery planning range from the settled prior "
                        "ALE openJiuwen campaign; task mix remains a major uncertainty"
                    ),
                }
                if selected_model_ids == ["claude-opus-5"]
                else {
                    "low": 0.02 * planned_trials,
                    "high": 3.0 * planned_trials,
                    "basis": "generic qualification-pilot planning range",
                }
            ),
            "hard_dollar_stop": None,
        },
        "required_post_run_audit": [
            "actual model/provider/quantization/fallback and every generation ID",
            "seven native tools plus fourteen ALE CUA tools and tool closure",
            "exclusive reasoning replay representation, retries, 429/5xx and timeout events",
            "runtime-budget prompt absent and runner deadline equals the enforced ALE deadline",
            "native/runner/provider token-cost reconciliation and secret absence",
        ],
        "known_limits": [
            "candidate extension is outside the registered 17-cell primary matrix",
            "text canary does not qualify CUA invocation, multimodality, compaction or full concurrency",
            "read-only runtime bind is local-Docker-specific and must be revalidated per host",
            "the study excludes six upstream tasks unsupported by local Docker",
        ],
    }
    payload = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    manifest["manifest_sha256"] = hashlib.sha256(payload.encode()).hexdigest()
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )
    for path in configs_dir.iterdir():
        path.chmod(0o444)
    (output / "manifest.json").chmod(0o444)
    return manifest


def canary_doctor(config: dict[str, Any], campaign_dir: Path) -> int:
    """Validate an already-frozen canary without loading or printing secrets."""
    checks: list[Check] = []
    manifest_path = campaign_dir / "manifest.json"
    if not manifest_path.is_file():
        checks.append(Check("manifest", False, str(manifest_path)))
        _print_report(ROOT / "experiment.yaml", checks)
        return 2
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    recorded_sha = manifest.pop("manifest_sha256", None)
    payload = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    actual_sha = hashlib.sha256(payload.encode()).hexdigest()
    checks.append(Check("manifest-hash", actual_sha == recorded_sha, actual_sha))
    source_root = campaign_dir / "source" / "ale_run"
    source_sha = _tree_sha256(source_root) if source_root.is_dir() else "missing"
    expected_source_sha = manifest["runner"]["derived_source"]["ale_run_tree_sha256"]
    checks.append(Check("source-hash", source_sha == expected_source_sha, source_sha))
    experiment_path = campaign_dir / "configs" / "experiment.yaml"
    experiment_sha = _sha256(experiment_path) if experiment_path.is_file() else "missing"
    checks.append(Check(
        "experiment-hash",
        experiment_sha == manifest["runner"]["experiment_config_sha256"],
        experiment_sha,
    ))
    actual_config_hashes = {
        path.name: _sha256(path)
        for path in sorted((campaign_dir / "configs").iterdir())
        if path.is_file()
    }
    checks.append(Check(
        "config-files",
        actual_config_hashes == manifest["runner"]["config_files_sha256"],
        f"{len(actual_config_hashes)} files",
    ))
    evaluator = (manifest.get("accounting") or {}).get("evaluator")
    if evaluator:
        evaluator_config = campaign_dir / "configs" / "evaluator-openrouter.json"
        evaluator_injector = (
            campaign_dir / "source" / "ale_run" / "agents" /
            "study_openjiuwen" / "evaluator-openrouter-injector.py"
        )
        try:
            evaluator_route = json.loads(evaluator_config.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            evaluator_route = None
        checks.append(Check(
            "evaluator-route",
            evaluator_config.is_file()
            and evaluator_injector.is_file()
            and evaluator_route == {
                "model": evaluator.get("model"),
                "incoming_model": evaluator.get("incoming_model"),
                "provider": evaluator.get("route"),
            }
            and _sha256(evaluator_config) == evaluator.get("config_sha256")
            and _sha256(evaluator_injector) == evaluator.get("injector_sha256"),
            "gpt-4o-mini via OpenRouter pinned OpenAI; values not loaded",
        ))
    frozen_experiment = (
        yaml.safe_load(experiment_path.read_text(encoding="utf-8"))
        if experiment_path.is_file() else {}
    )
    agent_refs = list(frozen_experiment.get("agents") or [])
    if frozen_experiment.get("agent"):
        agent_refs.append(frozen_experiment["agent"])
    agent_docs = [
        yaml.safe_load(Path(reference).read_text(encoding="utf-8"))
        for reference in agent_refs
        if Path(reference).is_file()
    ]
    expected_models = {
        cell["model"] for cell in (manifest.get("cells") or {}).values()
    }
    if not expected_models and manifest.get("cell"):
        expected_models = {manifest["cell"]["model"]}
    actual_models = {item.get("model") for item in agent_docs}
    planned_trials = int(manifest["scope"]["planned_trials"])
    planned_agents = int(
        manifest["scope"].get("planned_agents")
        or len((manifest.get("cells") or {}))
        or planned_trials
    )
    checks.append(Check(
        "agent-matrix",
        len(agent_refs) == planned_agents
        and len(agent_docs) == planned_agents
        and actual_models == expected_models,
        f"{len(agent_docs)}/{planned_agents} agents; models={sorted(actual_models)}",
    ))
    checks.append(Check(
        "concurrency",
        frozen_experiment.get("concurrency")
        == manifest.get("controls", {}).get("concurrency"),
        str(frozen_experiment.get("concurrency")),
    ))
    runtime = manifest["runner"].get("host_runtime", {})
    checks.append(Check(
        "host-pyproject",
        _sha256(ROOT / "pyproject.toml") == runtime.get("pyproject_sha256"),
        _sha256(ROOT / "pyproject.toml"),
    ))
    checks.append(Check(
        "host-uv-lock",
        _sha256(ROOT / "uv.lock") == runtime.get("uv_lock_sha256"),
        _sha256(ROOT / "uv.lock"),
    ))
    host_python = ROOT / ".venv" / "bin" / "python"
    host_import = subprocess.run(
        [
            str(host_python),
            "-c",
            "import openenv, cua_bench, docker, pydantic; print('imports-ok')",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    ) if host_python.is_file() else None
    checks.append(Check(
        "host-runtime",
        host_import is not None and host_import.returncode == 0,
        host_import.stdout.strip() if host_import and host_import.returncode == 0
        else "run uv sync --frozen",
    ))
    repository = ROOT / config["runners"]["ale"]["repository_path"]
    derived_env = os.environ.copy()
    derived_env["PYTHONPATH"] = os.pathsep.join(
        (str(campaign_dir / "source"), str(repository))
    )
    derived_import = subprocess.run(
        [
            str(host_python),
            "-P",
            "-c",
            (
                "from pathlib import Path; "
                "import ale_run.executors.sandbox as module; "
                "print(Path(module.__file__).resolve())"
            ),
        ],
        cwd=repository,
        env=derived_env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    ) if host_python.is_file() else None
    expected_import = (
        campaign_dir / "source" / "ale_run" / "executors" / "sandbox.py"
    ).resolve()
    actual_import = (
        Path(derived_import.stdout.strip()).resolve()
        if derived_import and derived_import.returncode == 0
        and derived_import.stdout.strip()
        else None
    )
    checks.append(Check(
        "derived-import",
        actual_import == expected_import,
        str(actual_import) if actual_import else (
            derived_import.stderr.strip() if derived_import else "host python missing"
        ),
    ))
    scope_task_ids = list(
        manifest["scope"].get("tasks")
        or [manifest["scope"].get("task") or ALE_CANARY_TASK]
    )
    task_metadata: dict[str, dict[str, Any]] | None = None
    task_metadata_error = "host python missing"
    if host_python.is_file():
        try:
            task_metadata = _ale_variant_zero_metadata(repository, scope_task_ids)
            task_metadata_error = ""
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            task_metadata_error = str(exc)
    expected_task_metadata = {
        task_id: {
            key: manifest["scope"]["task_controls"][task_id].get(key)
            for key in (
                "variant_index", "domain_name", "task_name", "variant_name",
                "requires_task_data",
            )
        }
        for task_id in scope_task_ids
    }
    checks.append(Check(
        "tasks-package-import",
        task_metadata == expected_task_metadata,
        f"{len(task_metadata)}/{len(scope_task_ids)} task variants resolved"
        if task_metadata is not None else task_metadata_error,
    ))
    task_import = subprocess.run(
        [
            str(host_python),
            "-P",
            "-c",
            "from tasks.common_setup import BaseTaskSetup; print('tasks-import-ok')",
        ],
        cwd=repository,
        env=derived_env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    ) if host_python.is_file() else None
    checks.append(Check(
        "task-import",
        task_import is not None and task_import.returncode == 0,
        task_import.stdout.strip() if task_import and task_import.returncode == 0
        else (task_import.stderr.strip() if task_import else "host python missing"),
    ))
    head = _git_output(repository, "rev-parse", "HEAD")
    checks.append(Check(
        "dataset-commit", head == manifest["scope"]["dataset_commit"], head
    ))
    task_list = campaign_dir / "configs" / "tasks.txt"
    task_ids = _selected_ids(task_list) if task_list.is_file() else []
    checks.append(Check("task", task_ids == scope_task_ids, repr(task_ids)))
    task_data_source = str(manifest["sandbox"]["task_data_source"])
    task_data_root = Path(task_data_source.removeprefix("local:"))
    task_controls = manifest["scope"].get("task_controls") or {}
    task_variant_roots = {
        task_id: task_data_root / task_id / str(
            (task_controls.get(task_id) or {}).get("variant_name") or "base"
        )
        for task_id in scope_task_ids
    }
    task_variant_hashes = {
        task_id: _tree_sha256(path) if path.is_dir() else "missing"
        for task_id, path in task_variant_roots.items()
    }
    expected_variant_hashes = manifest["sandbox"].get("task_data_variants_sha256")
    if expected_variant_hashes is None:
        expected_variant_hashes = {
            scope_task_ids[0]: manifest["sandbox"]["task_data_variant_sha256"]
        }
    checks.append(Check(
        "task-data-hash",
        task_variant_hashes == expected_variant_hashes,
        json.dumps(task_variant_hashes, sort_keys=True),
    ))
    unreadable: list[str] = []
    for task_id, task_variant_root in task_variant_roots.items():
        for path in task_variant_root.rglob("*") if task_variant_root.is_dir() else []:
            mode = stat.S_IMODE(path.stat().st_mode)
            relative = str(path.relative_to(task_variant_root))
            label = f"{task_id}/{relative}"
            if path.is_dir() and mode & 0o005 != 0o005:
                unreadable.append(f"{label}:{mode:o}")
            elif path.is_file():
                if mode & 0o004 == 0:
                    unreadable.append(f"{label}:{mode:o}")
                if relative.startswith("software/") and mode & 0o001 == 0:
                    unreadable.append(f"{label}:{mode:o}:not-executable")
    checks.append(Check(
        "task-data-permissions",
        all(path.is_dir() for path in task_variant_roots.values()) and not unreadable,
        "container-readable/executable" if not unreadable else ", ".join(unreadable[:5]),
    ))
    openjiuwen_runtime = manifest.get("sandbox", {}).get("openjiuwen_runtime")
    if isinstance(openjiuwen_runtime, dict):
        runtime_path = Path(str(openjiuwen_runtime.get("host_path") or ""))
        try:
            runtime_metadata = json.loads(
                (runtime_path / "runtime.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            runtime_metadata = None
        checks.append(Check(
            "openjiuwen-runtime",
            runtime_path.is_dir()
            and runtime_metadata == openjiuwen_runtime.get("metadata"),
            str(runtime_path),
        ))
        environment_ref = frozen_experiment.get("environment")
        frozen_environment = (
            yaml.safe_load(Path(environment_ref).read_text(encoding="utf-8"))
            if environment_ref and Path(environment_ref).is_file() else {}
        )
        mount_paths = {
            str((entry.get("docker") or {}).get("openjiuwen_runtime_host_path") or "")
            for entry in (frozen_environment.get("snapshots") or {}).values()
            if isinstance(entry, dict)
        }
        checks.append(Check(
            "openjiuwen-runtime-mount",
            mount_paths == {str(runtime_path)},
            repr(sorted(mount_paths)),
        ))
    env_path = ROOT / ".env"
    env_mode = stat.S_IMODE(env_path.stat().st_mode) if env_path.is_file() else None
    declared_keys = {
        raw.split("=", 1)[0].removeprefix("export ").strip()
        for raw in env_path.read_text(encoding="utf-8").splitlines()
        if raw.strip() and not raw.lstrip().startswith("#") and "=" in raw
    } if env_path.is_file() else set()
    checks.append(Check(
        "openrouter-key", "OPENROUTER_API_KEY" in declared_keys, "declared/not printed"
    ))
    checks.append(Check("env-mode", env_mode == 0o600, oct(env_mode) if env_mode else "missing"))
    image = subprocess.run(
        ["docker", "image", "inspect", ALE_DOCKER_IMAGE, "--format", "{{.Id}} {{.Size}}"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    checks.append(Check(
        "image",
        image.returncode == 0,
        image.stdout.strip() if image.returncode == 0 else "not present or daemon unavailable",
    ))
    raw_root = campaign_dir / "raw"
    raw_entries = list(raw_root.rglob("*")) if raw_root.exists() else []
    checks.append(Check("fresh-output", not raw_entries, f"{len(raw_entries)} entries"))
    evaluator_entries = list((campaign_dir / "evaluator").rglob("*")) \
        if (campaign_dir / "evaluator").exists() else []
    checks.append(Check(
        "fresh-evaluator-output", not evaluator_entries,
        f"{len(evaluator_entries)} entries",
    ))
    _print_report(ROOT / "experiment.yaml", checks)
    print("Boundary       local frozen-state checks only; .env values were not loaded or printed")
    print("Approval       exact manifest hash is required before any paid launch")
    return 0 if all(check.ok for check in checks) else 2


def _start_ale_evaluator_injector(
    campaign_dir: Path,
) -> tuple[subprocess.Popen[Any], Any, int]:
    """Start the frozen evaluator-only OpenRouter route injector."""
    route_path = campaign_dir / "configs" / "evaluator-openrouter.json"
    injector = (
        campaign_dir / "source" / "ale_run" / "agents" /
        "study_openjiuwen" / "evaluator-openrouter-injector.py"
    )
    if not route_path.is_file() or not injector.is_file():
        raise FileNotFoundError("frozen ALE evaluator route/injector is missing")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = int(reservation.getsockname()[1])
    evidence = campaign_dir / "evaluator"
    evidence.mkdir(mode=0o700)
    stderr_handle = (evidence / "injector.stderr.log").open("wb")
    process = subprocess.Popen(
        [
            str(ROOT / ".venv" / "bin" / "python"),
            str(injector),
            "--config", str(route_path),
            "--upstream", "https://openrouter.ai/api",
            "--log", str(evidence / "injector-audit.jsonl"),
            "--port", str(port),
        ],
        stdout=stderr_handle,
        stderr=subprocess.STDOUT,
        cwd=campaign_dir,
        start_new_session=True,
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr_handle.close()
            raise RuntimeError("ALE evaluator route injector exited during startup")
        try:
            response = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=1
            )
            status = response.status
            response.close()
            if status in (200, 204):
                return process, stderr_handle, port
        except Exception:  # noqa: BLE001 - bounded local health polling
            time.sleep(0.2)
    process.terminate()
    process.wait(timeout=5)
    stderr_handle.close()
    raise TimeoutError("ALE evaluator route injector health check timed out")


def run_canary(
    config: dict[str, Any], campaign_dir: Path, approved_manifest_sha256: str
) -> int:
    """Run only an explicitly approved, fresh, fully validated canary."""
    manifest_path = campaign_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    frozen_sha = str(manifest.get("manifest_sha256") or "")
    if approved_manifest_sha256 != frozen_sha:
        print(
            "ERROR  approval hash does not match frozen manifest",
            file=sys.stderr,
        )
        return 2
    if canary_doctor(config, campaign_dir) != 0:
        print("ERROR  canary doctor failed; refusing paid launch", file=sys.stderr)
        return 2
    experiment_path = campaign_dir / "configs" / "experiment.yaml"
    repository = ROOT / config["runners"]["ale"]["repository_path"]
    env = os.environ.copy()
    current = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        (str(campaign_dir / "source"), str(repository))
    ) + (
        os.pathsep + current if current else ""
    )
    command = [
        str(ROOT / ".venv" / "bin" / "python"),
        "-P",
        "-m",
        "ale_run",
        "run",
        str(experiment_path),
        "--disable-resume",
    ]
    print(f"Launching      {manifest['campaign_id']}")
    print(f"Manifest SHA  {frozen_sha}")
    planned = manifest["scope"]["planned_trials"]
    concurrency = manifest.get("controls", {}).get("concurrency", 1)
    print(
        f"Trials        {planned}; concurrency={concurrency}; "
        "fresh output; no whole-trial retry"
    )
    evaluator = (manifest.get("accounting") or {}).get("evaluator")
    injector_process: subprocess.Popen[Any] | None = None
    injector_stderr = None
    if evaluator:
        openrouter_key = env.get("OPENROUTER_API_KEY", "").strip()
        if not openrouter_key:
            print("ERROR  OPENROUTER_API_KEY is required by evaluator", file=sys.stderr)
            return 2
        injector_process, injector_stderr, evaluator_port = (
            _start_ale_evaluator_injector(campaign_dir)
        )
        # The stock evaluator uses the OpenAI SDK and the bare gpt-4o-mini
        # name. Route that evaluator-only traffic through the frozen injector,
        # which maps the model slug and pins OpenRouter to OpenAI. The agent's
        # own OpenRouter route remains unchanged.
        env["OPENAI_API_KEY"] = openrouter_key
        env["OPENAI_BASE_URL"] = f"http://127.0.0.1:{evaluator_port}/v1"
        print(
            "Evaluator     gpt-4o-mini via OpenRouter; OpenAI provider pinned; "
            "cost accounted separately"
        )
    try:
        result = subprocess.run(command, cwd=repository, env=env, check=False)
        return result.returncode
    finally:
        if injector_process is not None:
            if injector_process.poll() is None:
                injector_process.terminate()
                try:
                    injector_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    injector_process.kill()
                    injector_process.wait(timeout=5)
            if injector_stderr is not None:
                injector_stderr.close()


def audit_pi_canary(campaign_dir: Path) -> dict[str, Any]:
    """Audit a completed PI ALE qualification campaign and preserve evidence."""
    from pilot.provider_smoke import (
        fetch_openrouter_generation_records,
        parse_json_lines,
    )

    manifest = json.loads((campaign_dir / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("campaign_id") != ALE_PI_FOUR_MODEL_CAMPAIGN:
        raise ValueError("not the frozen PI four-model ALE campaign")
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise ValueError("OPENROUTER_API_KEY must be loaded for generation metadata")
    raw_root = campaign_dir / "raw" / manifest["campaign_id"]
    trajectory_paths = sorted(raw_root.rglob("trajectory.json"))
    if len(trajectory_paths) != manifest["scope"]["planned_trials"]:
        raise ValueError(
            f"expected {manifest['scope']['planned_trials']} trajectories, "
            f"found {len(trajectory_paths)}"
        )

    provider = fetch_openrouter_generation_records(raw_root, api_key)
    provider_path = campaign_dir / "provider-generations.json"
    provider_path.write_text(
        json.dumps(provider, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    records_by_id = {
        str(record.get("id")): record for record in provider["records"] if record.get("id")
    }
    expected = {
        "anthropic/claude-opus-5": (
            "Anthropic", "anthropic/claude-opus-5-20260723"
        ),
        "openai/gpt-6-astra": ("OpenAI", "openai/gpt-6-astra-20260903"),
        "moonshotai/kimi-k3": ("Moonshot AI", "moonshotai/kimi-k3-20260715"),
        "deepseek/deepseek-v4-pro-0813": (
            "DeepSeek", "deepseek/deepseek-v4-pro-20260813"
        ),
    }
    cells: list[dict[str, Any]] = []
    for trajectory_path in trajectory_paths:
        trial_dir = trajectory_path.parent
        transcript_path = trial_dir / "origin_log" / "pi" / "transcript.jsonl"
        transcript = parse_json_lines(transcript_path)
        message_ends = [item for item in transcript if item.get("type") == "message_end"]
        assistant = [
            item.get("message") or {}
            for item in message_ends
            if (item.get("message") or {}).get("role") == "assistant"
        ]
        tool_results = [
            item.get("message") or {}
            for item in message_ends
            if (item.get("message") or {}).get("role") == "toolResult"
        ]
        response_ids = [
            str(item.get("responseId")) for item in assistant if item.get("responseId")
        ]
        generation_records = [
            records_by_id[item] for item in response_ids if item in records_by_id
        ]
        call_ids = {
            str(block.get("id"))
            for item in assistant
            for block in (item.get("content") or [])
            if isinstance(block, dict) and block.get("type") == "toolCall"
        }
        result_ids = {
            str(block.get("toolCallId") or item.get("toolCallId"))
            for item in tool_results
            for block in (item.get("content") or [])
            if isinstance(block, dict)
        }
        usage = {
            key: sum(int((item.get("usage") or {}).get(key) or 0) for item in assistant)
            for key in ("input", "output", "cacheRead", "cacheWrite", "reasoning")
        }
        usage["cost"] = sum(
            float(((item.get("usage") or {}).get("cost") or {}).get("total") or 0)
            for item in assistant
        )
        session_paths = sorted((trial_dir / "origin_log" / "pi" / "sessions").glob("*.jsonl"))
        sessions = [item for path in session_paths for item in parse_json_lines(path)]
        model_changes = [item for item in sessions if item.get("type") == "model_change"]
        thinking_changes = [
            item for item in sessions if item.get("type") == "thinking_level_change"
        ]
        retry_events = [
            item for item in sessions
            if "retry" in str(item.get("type") or "").lower()
        ]
        compaction_events = [
            item for item in sessions
            if "compact" in str(item.get("type") or "").lower()
        ]
        eval_result = json.loads((trial_dir / "eval_result.json").read_text())
        run = json.loads((trial_dir / "run.json").read_text())
        model = str(run["agent"]["model"])
        expected_provider, expected_snapshot = expected[model]
        runtime_models = json.loads(
            (trial_dir / "origin_log" / "pi" / "pi-config" / "models.json").read_text()
        )
        runtime_route = runtime_models["providers"]["openrouter"]["modelOverrides"][model][
            "compat"
        ]["openRouterRouting"]
        agent_id = str(run["agent"]["id"])
        manifest_route = manifest["cells"][agent_id]["route"]
        stop_reasons = [str(item.get("stopReason")) for item in assistant]
        stderr_path = trial_dir / "origin_log" / "pi" / "stderr.log"
        providers = sorted(
            {str(item.get("provider_name")) for item in generation_records}
        )
        actual_models = sorted({str(item.get("model")) for item in generation_records})
        reasoning_messages = sum(
            any(
                isinstance(block, dict) and block.get("type") == "thinking"
                for block in (item.get("content") or [])
            )
            for item in assistant
        )
        accepted = all((
            run.get("status") == "completed",
            eval_result.get("score") == 1.0,
            providers == [expected_provider],
            actual_models == [expected_snapshot],
            len(generation_records) == len(response_ids) == len(set(response_ids)),
            runtime_route == manifest_route,
            bool(model_changes) and model_changes[-1].get("modelId") == model,
            bool(thinking_changes) and thinking_changes[-1].get("thinkingLevel") == "high",
            bool(call_ids) and call_ids == result_ids,
            reasoning_messages > 0,
            not retry_events,
            not any(item in {"length", "max_tokens"} for item in stop_reasons),
            not stderr_path.read_text(encoding="utf-8", errors="replace"),
        ))
        cells.append({
            "agent": agent_id,
            "model": model,
            "status": run["status"],
            "score": eval_result.get("score"),
            "duration_s": (run.get("timings") or {}).get("duration_s"),
            "response_count": len(response_ids),
            "provider_generation_count": len(generation_records),
            "actual_provider": providers,
            "actual_model": actual_models,
            "strict_route": runtime_route,
            "fallback_allowed": runtime_route.get("allow_fallbacks"),
            "thinking_level": thinking_changes[-1].get("thinkingLevel") if thinking_changes else None,
            "reasoning_messages": reasoning_messages,
            "tool_call_count": len(call_ids),
            "tool_result_count": len(result_ids),
            "tool_closure": call_ids == result_ids,
            "retry_event_count": len(retry_events),
            "compaction_event_count": len(compaction_events),
            "stop_reasons": stop_reasons,
            "harness_usage": usage,
            "provider_native_prompt_tokens": sum(
                int(item.get("native_tokens_prompt") or 0) for item in generation_records
            ),
            "provider_native_completion_tokens": sum(
                int(item.get("native_tokens_completion") or 0) for item in generation_records
            ),
            "provider_total_cost_usd": sum(
                float(item.get("total_cost") or 0) for item in generation_records
            ),
            "stderr_bytes": stderr_path.stat().st_size,
            "decision": "accept-B-text" if accepted else "reject-needs-review",
        })

    env_values = [
        raw.split("=", 1)[1].strip().strip("'\"")
        for raw in (ROOT / ".env").read_text(encoding="utf-8").splitlines()
        if raw.strip() and not raw.lstrip().startswith("#") and "=" in raw
        and len(raw.split("=", 1)[1].strip().strip("'\"")) >= 12
    ]
    secret_hits: list[str] = []
    for path in campaign_dir.rglob("*"):
        if not path.is_file():
            continue
        content = path.read_bytes()
        if any(value.encode() in content for value in env_values):
            secret_hits.append(str(path.relative_to(campaign_dir)))
    audit = {
        "campaign_id": manifest["campaign_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "sample": "all four planned trials",
        "cells": cells,
        "provider_generation_lookup_errors": provider["errors"],
        "provider_generations_sha256": _sha256(provider_path),
        "total_provider_cost_usd": sum(
            float(cell["provider_total_cost_usd"]) for cell in cells
        ),
        "secret_scan": {
            "values_checked": len(env_values),
            "literal_hit_paths": secret_hits,
        },
        "qualification_boundary": (
            "B(text) for this exact PI/ALE Docker terminal lifecycle; no claim for "
            "multimodal/CUA, excluded non-Docker tasks, compaction recovery, or formal concurrency"
        ),
        "decision": (
            "accept-all-four-B-text"
            if all(cell["decision"] == "accept-B-text" for cell in cells)
            and not provider["errors"] and not secret_hits
            else "reject-needs-review"
        ),
    }
    audit_path = campaign_dir / "audit-final.json"
    audit_path.write_text(
        json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return audit


def audit_codex_canary(campaign_dir: Path) -> dict[str, Any]:
    """Audit the completed stock-Codex GPT ALE qualification canary."""
    from pilot.provider_smoke import (
        fetch_openrouter_generation_records,
        parse_json_lines,
    )

    manifest = json.loads((campaign_dir / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("campaign_id") != ALE_CODEX_CAMPAIGN:
        raise ValueError("not the frozen Codex GPT ALE campaign")
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise ValueError("OPENROUTER_API_KEY must be loaded for generation metadata")
    raw_root = campaign_dir / "raw" / manifest["campaign_id"]
    trajectory_paths = sorted(raw_root.rglob("trajectory.json"))
    if len(trajectory_paths) != 1:
        raise ValueError(f"expected one trajectory, found {len(trajectory_paths)}")

    provider_raw = fetch_openrouter_generation_records(raw_root, api_key)
    generation_fields = (
        "id", "model", "provider_name", "total_cost", "tokens_prompt",
        "tokens_completion", "native_tokens_prompt", "native_tokens_cached",
        "native_tokens_completion", "native_tokens_reasoning", "finish_reason",
        "native_finish_reason", "generation_time", "latency", "streamed",
        "cancelled", "service_tier", "created_at", "api_type",
    )
    provider = {
        "source": provider_raw["source"],
        "generation_ids_found": provider_raw["generation_ids_found"],
        "records": [
            {field: record.get(field) for field in generation_fields}
            for record in provider_raw["records"]
        ],
        "errors": provider_raw["errors"],
    }
    provider_path = campaign_dir / "provider-generations.json"
    provider_path.write_text(
        json.dumps(provider, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    provider_path.chmod(0o600)
    records_by_id = {
        str(record.get("id")): record
        for record in provider["records"]
        if record.get("id")
    }

    trial_dir = trajectory_paths[0].parent
    origin = trial_dir / "origin_log" / "study_codex"
    run = json.loads((trial_dir / "run.json").read_text(encoding="utf-8"))
    evaluation = json.loads(
        (trial_dir / "eval_result.json").read_text(encoding="utf-8")
    )
    trajectory = json.loads(
        (trial_dir / "trajectory.json").read_text(encoding="utf-8")
    )
    result = json.loads((origin / "_result.json").read_text(encoding="utf-8"))
    spec = json.loads((origin / "_spec.json").read_text(encoding="utf-8"))
    summary = json.loads(
        (origin / "telemetry_summary.json").read_text(encoding="utf-8")
    )
    telemetry = parse_json_lines(origin / "telemetry.jsonl")
    transcript = parse_json_lines(origin / "transcript.jsonl")
    injector = parse_json_lines(origin / "openrouter-injector.jsonl")
    responses = [item for item in injector if item.get("path") == "/v1/responses"]
    auxiliary = [item for item in injector if item.get("path") != "/v1/responses"]
    generation_ids = [
        str(item.get("openrouter_generation_id"))
        for item in responses
        if item.get("openrouter_generation_id")
    ]
    generations = [records_by_id[item] for item in generation_ids if item in records_by_id]

    tool_starts = {
        str((item.get("item") or {}).get("id"))
        for item in transcript
        if item.get("type") == "item.started"
        and (item.get("item") or {}).get("type") in {"command_execution", "file_change"}
    }
    tool_completions = [
        item.get("item") or {}
        for item in transcript
        if item.get("type") == "item.completed"
        and (item.get("item") or {}).get("type") in {"command_execution", "file_change"}
    ]
    tool_completion_ids = {str(item.get("id")) for item in tool_completions}
    tool_statuses = [str(item.get("status")) for item in tool_completions]
    api_calls = summary.get("api_calls") or []
    completion_events = [
        (item.get("response_completed_event") or {}).get("attributes") or {}
        for item in api_calls
    ]
    reasoning_tokens = sum(
        int(item.get("reasoning_token_count") or 0) for item in completion_events
    )
    input_tokens = sum(
        int(item.get("input_token_count") or 0) for item in completion_events
    )
    cached_tokens = sum(
        int(item.get("cached_token_count") or 0) for item in completion_events
    )
    output_tokens = sum(
        int(item.get("output_token_count") or 0) for item in completion_events
    )
    replay_reasoning_counts = [
        (item.get("input_types") or []).count("reasoning") for item in responses
    ]
    replay_call_counts = [
        (item.get("input_types") or []).count("custom_tool_call")
        for item in responses
    ]
    replay_result_counts = [
        (item.get("input_types") or []).count("custom_tool_call_output")
        for item in responses
    ]
    providers = sorted({str(item.get("provider_name")) for item in generations})
    actual_models = sorted({str(item.get("model")) for item in generations})
    finish_reasons = sorted({str(item.get("finish_reason")) for item in generations})
    native_finish_reasons = sorted(
        {str(item.get("native_finish_reason")) for item in generations}
    )
    entry_log = (origin / "_entry.log").read_text(encoding="utf-8", errors="replace")
    stderr = (origin / "stderr.log").read_text(encoding="utf-8", errors="replace")
    compaction_events = [
        item for item in transcript
        if "compact" in str(item.get("type") or "").lower()
        or "compact" in str((item.get("item") or {}).get("type") or "").lower()
    ]
    expected_route = manifest["cells"]["codex--gpt-6-astra"]["route"]
    conversation_starts = [
        item.get("attributes") or {}
        for item in telemetry
        if (item.get("attributes") or {}).get("event.name")
        == "codex.conversation_starts"
    ]
    cua_configured = (
        len(conversation_starts) == 1
        and conversation_starts[0].get("mcp_servers") == "cua"
        and "ensure_cua_mcp_server: bridge already present" in entry_log
    )
    exact_requests = all(
        item.get("status") == 200
        and item.get("model") == "openai/gpt-6-astra"
        and item.get("provider") == expected_route
        and item.get("reasoning") == {"context": "all_turns", "effort": "high"}
        and item.get("max_output_tokens_present") is False
        and "additional_tools" in (item.get("input_types") or [])
        for item in responses
    )
    accepted = all((
        run.get("status") == "completed",
        evaluation.get("eval_status") == "success",
        evaluation.get("score") == 1.0,
        result.get("status") == "completed" and result.get("exit_code") == 0,
        len(responses) == len(api_calls) == len(generation_ids) == len(generations) == 7,
        len(generation_ids) == len(set(generation_ids)),
        exact_requests,
        providers == ["OpenAI"],
        actual_models == ["openai/gpt-6-astra-20260903"],
        all(item.get("status_code") == 200 and item.get("attempt") == 0 for item in api_calls),
        all(item.get("model_reasoning_effort") == "high" for item in completion_events),
        tool_starts == tool_completion_ids and len(tool_completion_ids) == 6,
        replay_call_counts == replay_result_counts,
        replay_reasoning_counts[-1] == 5,
        not compaction_events,
        not provider["errors"],
        finish_reasons == ["stop", "tool_calls"],
        native_finish_reasons == ["completed"],
        cua_configured,
    ))

    env_values = [
        raw.split("=", 1)[1].strip().strip("'\"")
        for raw in (ROOT / ".env").read_text(encoding="utf-8").splitlines()
        if raw.strip() and not raw.lstrip().startswith("#") and "=" in raw
        and len(raw.split("=", 1)[1].strip().strip("'\"")) >= 12
    ]
    secret_hits: list[str] = []
    for path in campaign_dir.rglob("*"):
        if not path.is_file() or path == provider_path:
            continue
        content = path.read_bytes()
        if any(value.encode() in content for value in env_values):
            secret_hits.append(str(path.relative_to(campaign_dir)))

    audit = {
        "campaign_id": manifest["campaign_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "sample": "the single planned real ALE text trial (full trajectory)",
        "task": run["task"]["path"],
        "status": run["status"],
        "score": evaluation.get("score"),
        "runner_duration_s": (run.get("timings") or {}).get("duration_s"),
        "agent_duration_s": result.get("duration_s"),
        "response_count": len(responses),
        "provider_generation_count": len(generations),
        "provider_generation_lookup_errors": provider["errors"],
        "provider_generations_sha256": _sha256(provider_path),
        "actual_provider": providers,
        "actual_model": actual_models,
        "strict_route_on_every_request": exact_requests,
        "reasoning": {
            "wire_effort": sorted({str(item.get("reasoning")) for item in responses}),
            "context": "all_turns on all seven requests",
            "native_reasoning_tokens": reasoning_tokens,
            "replayed_reasoning_item_counts": replay_reasoning_counts,
            "continuity_evidence": (
                "prior reasoning plus every custom tool call/result were replayed in "
                "later Responses inputs; no translation layer"
            ),
        },
        "tools": {
            "additional_tools_item_present_every_request": all(
                "additional_tools" in (item.get("input_types") or []) for item in responses
            ),
            "decision_count": len(tool_completion_ids),
            "started_ids_equal_completed_ids": tool_starts == tool_completion_ids,
            "statuses": tool_statuses,
            "failed_then_recovered": "failed" in tool_statuses and tool_statuses[-1] == "completed",
            "replayed_call_counts": replay_call_counts,
            "replayed_result_counts": replay_result_counts,
            "otel_tool_result_count": (summary.get("events_by_name") or {}).get("codex.tool_result"),
            "otel_result_count_note": (
                "Codex emits one result for the Responses-Lite exec wrapper and one for "
                "the nested concrete tool; transcript item IDs are the independent-decision truth"
            ),
            "cua_mcp_configured_and_started": cua_configured,
            "cua_invoked": False,
        },
        "request_health": {
            "http_200_attempt_zero": len(api_calls),
            "model_retry_count": sum(int(item.get("attempt") or 0) for item in api_calls),
            "auxiliary_requests": [
                {"path": item.get("path"), "status": item.get("status")}
                for item in auxiliary
            ],
            "rate_limit_or_5xx_count": sum(
                int(int(item.get("status") or 0) == 429 or int(item.get("status") or 0) >= 500)
                for item in injector
            ),
            "stderr": stderr.strip(),
        },
        "context_and_output": {
            "compaction_event_count": len(compaction_events),
            "output_cap_wire_field_present_count": sum(
                bool(item.get("max_output_tokens_present")) for item in responses
            ),
            "finish_reasons": finish_reasons,
            "native_finish_reasons": native_finish_reasons,
            "prompt_too_long_or_incomplete": False,
        },
        "tokens": {
            "codex_input": input_tokens,
            "codex_cached_input": cached_tokens,
            "codex_output": output_tokens,
            "codex_reasoning": reasoning_tokens,
            "ale_normalized": run.get("usage"),
            "note": "ALE omits reasoning and reports zero cost; provider metadata is authoritative",
        },
        "provider_total_cost_usd": sum(
            float(item.get("total_cost") or 0) for item in generations
        ),
        "secret_scan": {
            "values_checked": len(env_values),
            "literal_hit_paths": secret_hits,
        },
        "qualification_boundary": (
            "B(text) for this exact Codex 0.150.1 × GPT-5.6-Sol OpenAI-pinned "
            "ALE Docker terminal lifecycle. CUA was configured but not invoked; no "
            "claim for multimodal/CUA execution, long-context compaction recovery, "
            "the six non-Docker tasks, concurrency, or model quality."
        ),
        "decision": (
            "accept-B-text" if accepted and not secret_hits else "reject-needs-review"
        ),
    }
    audit_path = campaign_dir / "audit-final.json"
    audit_path.write_text(
        json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    audit_path.chmod(0o600)
    return audit


def agent_doctor(config: dict[str, Any], harness_ids: list[str]) -> int:
    """Offline import/config/proxy/parser checks for implemented ALE agents."""
    checks: list[Check] = []
    with tempfile.TemporaryDirectory(prefix="ale-study-agents-", dir="/tmp") as tmp:
        derived = Path(tmp) / "source"
        try:
            manifest = prepare_agent_source(config, derived, harness_ids)
            checks.append(Check("derived-source", True, manifest["ale_run_tree_sha256"]))
        except Exception as exc:
            checks.append(Check("derived-source", False, f"{type(exc).__name__}: {exc}"))
            _print_report(ROOT / "experiment.yaml", checks)
            return 2

        script = r'''
import json
import tempfile
from pathlib import Path
from ale_run.base_interface import AgentRunResult, TrajectoryBuilder

requested = json.loads(__import__("os").environ["ALE_STUDY_HARNESSES"])
expected_pi_specs = json.loads(__import__("os").environ["ALE_EXPECTED_PI_SPECS"])
expected_cc = json.loads(__import__("os").environ["ALE_EXPECTED_CC"])
expected_openhands_specs = json.loads(
    __import__("os").environ["ALE_EXPECTED_OPENHANDS_SPECS"]
)
expected_dsh_specs = json.loads(__import__("os").environ["ALE_EXPECTED_DSH_SPECS"])
expected_openjiuwen_specs = json.loads(
    __import__("os").environ["ALE_EXPECTED_OPENJIUWEN_SPECS"]
)
result = {}
if "claude-code" in requested:
    from ale_run.executors.sandbox import _host_ale_root_for_deployer
    from ale_run.agents.study_claude_code.config import StudyClaudeCodeConfig
    from ale_run.agents.study_claude_code.deployer import StudyClaudeCodeDeployer
    from ale_run.agents.study_claude_code.proxy import offline_self_test
    cfg = StudyClaudeCodeConfig()
    assert {
        "model": cfg.model.removesuffix("[1m]"),
        "version": cfg.cli_version.rsplit("@", 1)[-1],
        "context": cfg.context_tokens,
        "max_output": cfg.max_output_tokens,
        "effort": cfg.effort_level,
    } == expected_cc
    argv = StudyClaudeCodeDeployer._build_argv(
        claude_path="claude", cfg=cfg, mcp_config="mcp.json"
    )
    assert argv[:12] == [
        "claude", "-p", "-", "--output-format", "stream-json", "--verbose",
        "--mcp-config", "mcp.json", "--model", "anthropic/claude-opus-5[1m]",
        "--effort", "high",
    ]
    assert argv.count("--disallowedTools") == 8
    cc_root = _host_ale_root_for_deployer(StudyClaudeCodeDeployer)
    assert (cc_root / "agents" / "study_claude_code" / "deployer.py").is_file()
    offline_self_test()
    result["claude-code"] = "config/argv/proxy/shipping-root passed"
if "pi" in requested:
    import re
    from ale_run.executors.sandbox import _host_ale_root_for_deployer
    from ale_run.agents.study_pi.config import PI_MODEL_SPECS, StudyPiConfig
    from ale_run.agents.study_pi.deployer import _CUA_TOOLS, StudyPiDeployer
    assert PI_MODEL_SPECS == expected_pi_specs
    expected_cua_tools = (
        "key", "key_down", "key_up", "type", "hold_key", "mouse_move",
        "click", "drag", "mouse_down", "mouse_up", "scroll", "wait",
        "screenshot", "cursor_position",
    )
    assert _CUA_TOOLS == expected_cua_tools
    extension_source = Path(__import__(
        "ale_run.agents.study_pi.deployer", fromlist=["__file__"]
    ).__file__).with_name("cua-extension.mjs").read_text()
    public_extension_tools = tuple(re.findall(
        r'register\(pi,\s*["\']([^"\']+)', extension_source
    ))
    assert public_extension_tools == expected_cua_tools
    assert 'register(pi, "get_screen_size"' not in extension_source
    assert "const Coordinate = Type.Array(Type.Number()," in extension_source
    assert "Type.Tuple" not in extension_source
    for model in PI_MODEL_SPECS:
        cfg = StudyPiConfig(model=model)
        compat = cfg.models_json()["providers"]["openrouter"]["modelOverrides"][model]["compat"]
        assert compat["openRouterRouting"] == cfg.model_spec["route"]
    pi_root = _host_ale_root_for_deployer(StudyPiDeployer)
    assert (pi_root / "agents" / "study_pi" / "deployer.py").is_file()
    with tempfile.TemporaryDirectory(prefix="pi-parse-") as work:
        wd = Path(work)
        (wd / "transcript.jsonl").write_text(json.dumps({
            "type": "message_end", "message": {
                "role": "assistant", "content": [
                    {"type": "thinking", "thinking": "reason"},
                    {"type": "toolCall", "id": "call_1", "name": "bash",
                     "arguments": {"command": "echo ok"}},
                ], "provider": "openrouter", "model": "anthropic/claude-opus-5",
                "usage": {"input": 3, "output": 4, "cacheRead": 5, "cacheWrite": 0,
                          "reasoning": 1, "cost": {"total": 0.01}},
                "stopReason": "toolUse"
            }
        }) + "\n")
        builder = TrajectoryBuilder(
            agent_name="pi", agent_version="0.84.4",
            model="anthropic/claude-opus-5", task_path="test", variant_index=0,
        )
        StudyPiDeployer.parse_artifacts(
            work_dir=wd, config=StudyPiConfig(),
            run_result=AgentRunResult(status="completed", exit_code=0), builder=builder,
        )
        step = builder.trajectory.steps[0]
        assert step.reasoning == "reason" and step.tool_calls[0].name == "bash"
        assert step.metrics.cache_read_tokens == 5 and step.metrics.cost_usd == 0.01
    result["pi"] = "five configs/14-CUA-tools/parser/shipping-root passed"
if "codex" in requested:
    from ale_run.agents.study_codex.config import StudyCodexConfig
    from ale_run.agents.study_codex.deployer import StudyCodexDeployer
    from ale_run.agents.study_codex.injector import offline_self_test
    cfg = StudyCodexConfig()
    assert cfg.model == "gpt-6-astra"
    assert cfg.codex_version == "0.150.1"
    assert cfg.reasoning_effort == "high"
    assert cfg.model_catalog_content == "" and cfg.feature_overrides == {}
    assert cfg.upstream_model == "openai/gpt-6-astra"
    assert list(cfg.provider_only) == ["openai"]
    deployer = object.__new__(StudyCodexDeployer)
    deployer._codex_path = "codex"
    assert deployer._build_argv(cfg) == [
        "codex", "exec", "--model", "gpt-6-astra", "--json",
        "--dangerously-bypass-approvals-and-sandbox",
    ]
    offline_self_test()
    result["codex"] = "stock config/argv/injector passed"
if "openhands" in requested:
    from ale_run.agents.study_openhands.config import (
        OPENHANDS_MODEL_SPECS,
        StudyOpenHandsConfig,
    )
    from ale_run.agents.study_openhands.deployer import StudyOpenHandsDeployer
    assert OPENHANDS_MODEL_SPECS == expected_openhands_specs
    for model in OPENHANDS_MODEL_SPECS:
        cfg = StudyOpenHandsConfig(model=model)
        safe = cfg.safe_runner_config(
            mcp_command="node",
            mcp_args=["/cua/src/index.js"],
            cua_url="http://127.0.0.1:8000",
        )
        assert safe["model"] == f"openrouter/{model}"
        assert safe["litellm_extra_body"]["provider"] == cfg.model_spec["route"]
        assert safe["max_input_tokens"] == 1_000_000
        assert safe["max_output_tokens"] == 128_000
        assert safe["reasoning_effort"] == "high"
        assert safe["mcp_config"]["cua"]["transport"] == "stdio"
    package = Path(__import__(
        "ale_run.agents.study_openhands", fromlist=["x"]
    ).__file__).parent
    patch_text = (package / "reasoning_patch.py").read_text(encoding="utf-8")
    assert "openhands-1.44.1-openrouter-reasoning-roundtrip-v2" in patch_text
    compile((package / "runner.py").read_text(encoding="utf-8"), "runner.py", "exec")
    with tempfile.TemporaryDirectory(prefix="openhands-parse-") as work:
        wd = Path(work)
        synthetic = [
            {
                "kind": "MessageEvent", "source": "user",
                "llm_message": {"role": "user", "content": [{"type": "text", "text": "prompt"}]},
            },
            {
                "kind": "ActionEvent", "source": "agent",
                "reasoning_content": "reason", "reasoning_details": [{"type": "reasoning.text"}],
                "responses_reasoning_items": [{"id": "rs_1"}],
                "tool_name": "terminal", "tool_call_id": "call_1",
                "tool_call": {"id": "call_1", "name": "terminal", "arguments": "{\"command\":\"echo ok\"}"},
            },
            {
                "kind": "ObservationEvent", "source": "environment",
                "tool_name": "terminal", "tool_call_id": "call_1",
                "observation": {"kind": "TerminalObservation", "is_error": False,
                                "content": [{"type": "text", "text": "ok"}]},
            },
            {
                "kind": "MessageEvent", "source": "agent",
                "llm_message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
            },
        ]
        (wd / "openhands-events.jsonl").write_text(
            "".join(json.dumps(event) + "\n" for event in synthetic)
        )
        (wd / "openhands-metrics.json").write_text(json.dumps({
            "prompt_tokens": 13, "completion_tokens": 5,
            "cache_read_tokens": 3, "cache_write_tokens": 2, "cost_usd": 0.01,
        }))
        (wd / "resolved-openhands.json").write_text("{}")
        (wd / "resolved-install.json").write_text("{}")
        builder = TrajectoryBuilder(
            agent_name="openhands-sdk", agent_version="1.44.1",
            model="anthropic/claude-opus-5", task_path="test", variant_index=0,
        )
        StudyOpenHandsDeployer.parse_artifacts(
            work_dir=wd, config=StudyOpenHandsConfig(),
            run_result=AgentRunResult(status="completed", exit_code=0), builder=builder,
        )
        trajectory = builder.finalize(reward=None)
        assert len(trajectory.steps) == 3
        assert trajectory.steps[0].reasoning == "reason"
        assert trajectory.steps[0].tool_calls[0].name == "terminal"
        assert trajectory.steps[1].observation.results[0].tool_call_id == "call_1"
        assert trajectory.steps[2].message == "done"
        assert trajectory.final_metrics.total_input_tokens == 10
        assert trajectory.final_metrics.total_cache_read_tokens == 3
        assert trajectory.final_metrics.total_cost_usd == 0.01
    result["openhands"] = "five configs/runner/patch/parser passed"
if "deepseek-harness" in requested:
    from ale_run.agents.study_deepseek.config import (
        DSH_MODEL_SPECS,
        StudyDeepSeekConfig,
    )
    from ale_run.agents.study_deepseek.deployer import (
        StudyDeepSeekDeployer,
        build_dsh_patch,
    )
    assert DSH_MODEL_SPECS == expected_dsh_specs
    package = Path(__import__(
        "ale_run.agents.study_deepseek", fromlist=["x"]
    ).__file__).parent
    assert __import__("hashlib").sha256(
        (package / "dsh-headless-standard.patch").read_bytes()
    ).hexdigest() == "b072cb7582dcb1c6baa948f4a292d748eed75f349e878842c50791908a801622"
    assert __import__("hashlib").sha256(
        (package / "openrouter-body-injector.mjs").read_bytes()
    ).hexdigest() == "617726fc07c07f3098e0222110c29b2ab4da3f3b361a5a04603e81dfa11fc49c"
    for model in DSH_MODEL_SPECS:
        cfg = StudyDeepSeekConfig(model=model)
        patch = build_dsh_patch(
            cfg,
            sessions_root=Path("/tmp/sessions"),
            mcp_command="node",
            mcp_script="/cua/src/index.js",
            mcp_env={"CUA_SERVER_URL": "http://127.0.0.1:8000"},
            mcp_cwd="/tmp/work",
        )
        serialized = json.dumps(patch)
        assert '"default": "standard"' in serialized
        assert '"name": "@deepseek-ai/dsh-mcp-client"' in serialized
        assert cfg.model_spec["route"]["only"]
        provider = patch[1]["config"]["providers"]["ale-openrouter"]
        assert provider["reasoning"] == "high"
        assert provider["models"][0]["maxTokens"] == cfg.model_spec["max_output"]
        assert provider.get("cacheRetention") == cfg.model_spec["cache_retention"]
    with tempfile.TemporaryDirectory(prefix="dsh-parse-") as work:
        wd = Path(work)
        session = wd / "sessions" / "--work--" / "session-main" / "session.jsonl"
        session.parent.mkdir(parents=True)
        synthetic = [
            {"type": "session", "id": "session-main", "agentPreset": "standard"},
            {"type": "request/header", "data": {"header": {
                "config": {"provider": "ale-openrouter", "reasoningEffort": "high"},
                "tools": [{"name": "bash"}, {"name": "mcp__cua__screenshot"}],
            }}},
            {"type": "assistant/message", "data": {
                "turn": 1, "step": 1,
                "message": {"role": "assistant", "content": [
                    {"type": "reasoning", "text": "reason"},
                    {"type": "tool-call", "id": "call_1", "name": "bash",
                     "arguments": "{\"command\":\"echo ok\"}"},
                ]},
                "usage": {"inputTokens": 3, "outputTokens": 4,
                          "cacheReadTokens": 5, "cacheWriteTokens": 2,
                          "reasoningTokens": 1},
            }},
            {"type": "tool/result", "data": {
                "turn": 1, "step": 1,
                "message": {"role": "tool", "content": [{
                    "type": "tool-result", "toolCallId": "call_1",
                    "isError": False,
                    "content": [{"type": "text", "text": "ok"}],
                }]},
            }},
        ]
        session.write_text("".join(json.dumps(event) + "\n" for event in synthetic))
        builder = TrajectoryBuilder(
            agent_name="deepseek-harness", agent_version="0.1.1-rc.2",
            model="anthropic/claude-opus-5", task_path="test", variant_index=0,
        )
        StudyDeepSeekDeployer.parse_artifacts(
            work_dir=wd, config=StudyDeepSeekConfig(),
            run_result=AgentRunResult(status="completed", exit_code=0), builder=builder,
        )
        assert len(builder.trajectory.steps) == 2
        assert builder.trajectory.steps[0].reasoning == "reason"
        assert builder.trajectory.steps[0].tool_calls[0].name == "bash"
        assert builder.trajectory.steps[0].metrics.cache_read_tokens == 5
        assert builder.trajectory.steps[1].observation.results[0].tool_call_id == "call_1"
        assert builder.trajectory.extra["dsh"]["request_headers_primary"][0]["tool_count"] == 2
    result["deepseek-harness"] = "five configs/standard/CUA/parser passed"
if "openjiuwen" in requested:
    from ale_run.agents.study_openjiuwen.config import (
        OPENJIUWEN_MODEL_SPECS,
        StudyOpenJiuwenConfig,
    )
    from ale_run.agents.study_openjiuwen.deployer import StudyOpenJiuwenDeployer
    assert OPENJIUWEN_MODEL_SPECS == expected_openjiuwen_specs
    package = Path(__import__(
        "ale_run.agents.study_openjiuwen", fromlist=["x"]
    ).__file__).parent
    runner = package / "openjiuwen_agent.py"
    lock = package / "openjiuwen-runtime.lock"
    assert __import__("hashlib").sha256(runner.read_bytes()).hexdigest() == \
        "1895a50f963c5275259ade7dbb09b57e44d033c1cd041a7bcfc5d52789dc0339"
    assert __import__("hashlib").sha256(lock.read_bytes()).hexdigest() == \
        "0cf73ec722edae0c242a58d377dcdcd30185bd465bb8ad284efbe10a106d1d7a"
    compile(runner.read_text(encoding="utf-8"), "openjiuwen_agent.py", "exec")
    for model in OPENJIUWEN_MODEL_SPECS:
        cfg = StudyOpenJiuwenConfig(model=model)
        safe = cfg.safe_runner_config(
            runtime_budget_seconds=7200,
            mcp_command="node",
            mcp_script="/cua/src/index.js",
            cua_url="http://127.0.0.1:5000",
            work_dir="/work",
        )
        assert safe["model_id"] == model
        assert safe["provider_only"] == cfg.model_spec["route"]["only"]
        assert safe["reasoning_effort"] == "high"
        assert safe["runtime_budget_seconds"] == 7200
        assert safe["runtime_budget_rail_enabled"] is False
        assert safe["context_compression_enabled"] is True
        assert safe["completion_timeout_seconds"] == 7200
        assert safe["mcp_servers"][0]["server_name"] == "cua"
        assert safe["mcp_servers"][0]["include_image_content"] is True
    with tempfile.TemporaryDirectory(prefix="openjiuwen-parse-") as work:
        wd = Path(work)
        details = [{"type": "reasoning.text", "text": "r"}]
        transcript = [
            {"role": "assistant", "content": "", "reasoning_content": "reason",
             "finish_reason": "tool_calls",
             "metadata": {"_study_wire_response": {"id": "gen_1", "model": "m"},
                          "_openrouter_reasoning_details": details},
             "tool_calls": [{"id": "call_1", "function": {
                 "name": "bash", "arguments": "{\"command\":\"echo ok\"}"}}],
             "usage_metadata": {"input_tokens": 10, "output_tokens": 4,
                 "cache_read_tokens": 3, "cache_creation_input_tokens": 2,
                 "reasoning_tokens": 1, "total_cost": 0.01}},
            {"role": "tool", "tool_call_id": "call_1", "name": "bash",
             "content": "ok"},
            {"role": "assistant", "content": "done", "finish_reason": "stop",
             "usage_metadata": {"input_tokens": 12, "output_tokens": 2,
                                  "total_cost": 0.02}},
        ]
        (wd / "native-transcript.json").write_text(json.dumps(transcript))
        (wd / "native-events.jsonl").write_text(
            json.dumps({"event": "before_model_call"}) + "\n"
        )
        (wd / "resolved-install.json").write_text("{}")
        builder = TrajectoryBuilder(
            agent_name="openjiuwen-coding-agent", agent_version="0.1.18",
            model="deepseek/deepseek-v4-pro-0813", task_path="test", variant_index=0,
        )
        StudyOpenJiuwenDeployer.parse_artifacts(
            work_dir=wd, config=StudyOpenJiuwenConfig(),
            run_result=AgentRunResult(status="completed", exit_code=0), builder=builder,
        )
        trajectory = builder.finalize(reward=None)
        assert len(trajectory.steps) == 3
        assert trajectory.steps[0].reasoning == "reason"
        assert trajectory.steps[0].tool_calls[0].name == "bash"
        assert trajectory.steps[1].observation.results[0].tool_call_id == "call_1"
        assert trajectory.steps[2].message == "done"
        assert trajectory.final_metrics.total_input_tokens == 22
        assert trajectory.final_metrics.total_cache_read_tokens == 3
        assert trajectory.final_metrics.total_cost_usd == 0.03
    result["openjiuwen"] = "six configs/runtime/CUA/parser passed"
print(json.dumps(result, sort_keys=True))
'''
        env = os.environ.copy()
        env["PYTHONPATH"] = str(derived)
        env["ALE_STUDY_HARNESSES"] = json.dumps(harness_ids)
        expected_pi_specs: dict[str, Any] = {}
        for model in config["models"]:
            route = {
                "only": model["openrouter"]["provider_only"],
                "allow_fallbacks": False,
                "require_parameters": True,
            }
            if quantizations := model["openrouter"].get("quantizations"):
                route["quantizations"] = quantizations
            expected_pi_specs[model["openrouter"]["model_id"]] = {
                "route": route,
                "context": model["openrouter"]["endpoint_context_tokens"],
                "max_output": model["openrouter"]["pi_catalog_max_output_tokens"],
            }
        env["ALE_EXPECTED_PI_SPECS"] = json.dumps(expected_pi_specs)
        cc_harness = next(item for item in config["harnesses"] if item["id"] == "claude-code")
        cc_model = next(item for item in config["models"] if item["id"] == "claude-opus-5")
        env["ALE_EXPECTED_CC"] = json.dumps({
            "model": cc_model["openrouter"]["model_id"],
            "version": str(cc_harness["version"]),
            "context": cc_model["openrouter"]["endpoint_context_tokens"],
            "max_output": 64_000,
            "effort": "high",
        })
        expected_openhands_specs: dict[str, Any] = {}
        for model in config["models"]:
            openrouter = model["openrouter"]
            route = {
                "only": openrouter["provider_only"],
                "allow_fallbacks": False,
                "require_parameters": True,
            }
            if quantizations := openrouter.get("quantizations"):
                route["quantizations"] = quantizations
            expected_openhands_specs[openrouter["model_id"]] = {
                "route": route,
                "endpoint_context": openrouter["endpoint_context_tokens"],
                "endpoint_max_output": openrouter["endpoint_max_output_tokens"],
                "capability_overrides": openrouter.get(
                    "openhands_capability_overrides", {}
                ),
                "inline_image_urls": openrouter.get("openhands_inline_image_urls"),
            }
        env["ALE_EXPECTED_OPENHANDS_SPECS"] = json.dumps(
            expected_openhands_specs
        )
        expected_dsh_specs: dict[str, Any] = {}
        for model in config["models"]:
            openrouter = model["openrouter"]
            route = {
                "only": openrouter["provider_only"],
                "allow_fallbacks": False,
                "require_parameters": True,
            }
            if quantizations := openrouter.get("quantizations"):
                route["quantizations"] = quantizations
            compat = {
                "supportsDeveloperRole": False,
                "supportsReasoningEffort": True,
                "maxTokensField": "max_tokens",
                "thinkingFormat": "openrouter",
            }
            if model["id"] == "claude-opus-5":
                compat["cacheControlFormat"] = "anthropic"
            expected_dsh_specs[openrouter["model_id"]] = {
                "route": route,
                "context": openrouter["endpoint_context_tokens"],
                "max_output": openrouter["endpoint_max_output_tokens"],
                "input": ["text", "image"] if openrouter["supports_vision"] else ["text"],
                "compat": compat,
                "cache_retention": "short" if model["id"] == "claude-opus-5" else None,
            }
        env["ALE_EXPECTED_DSH_SPECS"] = json.dumps(expected_dsh_specs)
        expected_openjiuwen_specs = {
            model["openrouter"]["model_id"]: {
                "route": {
                    "only": model["openrouter"]["provider_only"],
                    **(
                        {"quantizations": model["openrouter"]["quantizations"]}
                        if model["openrouter"].get("quantizations") else {}
                    ),
                    "allow_fallbacks": False,
                    "require_parameters": True,
                },
                "context": model["openrouter"]["endpoint_context_tokens"],
            }
            for model in config["models"]
        }
        expected_openjiuwen_specs.update({
            model["openrouter"]["model_id"]: {
                "route": {
                    "only": model["openrouter"]["provider_only"],
                    **(
                        {"quantizations": model["openrouter"]["quantizations"]}
                        if model["openrouter"].get("quantizations") else {}
                    ),
                    "allow_fallbacks": False,
                    "require_parameters": True,
                },
                "context": model["openrouter"]["endpoint_context_tokens"],
            }
            for model in OPENJIUWEN_EXTRA_MODELS
        })
        env["ALE_EXPECTED_OPENJIUWEN_SPECS"] = json.dumps(
            expected_openjiuwen_specs
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=derived,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        detail = result.stdout.strip() if result.returncode == 0 else (
            (result.stderr or result.stdout).strip().splitlines()[-1]
        )
        checks.append(Check("offline-behavior", result.returncode == 0, detail))
        repository = ROOT / config["runners"]["ale"]["repository_path"]
        for harness_id in harness_ids:
            ok, detail = _study_upstream_dry_run(
                derived=derived,
                repository=repository,
                directory=Path(tmp) / f"dry-{harness_id}",
                harness_id=harness_id,
            )
            checks.append(Check(f"loader-{harness_id}", ok, detail))
    _print_report(ROOT / "experiment.yaml", checks)
    print("Boundary       no sandbox, image, benchmark task, network, or model API call")
    print("Next           use README prepare/run flow; current paid qualification is in the compatibility report")
    return 0 if all(check.ok for check in checks) else 2


def _selected_ids(path: Path) -> list[str]:
    return [
        line
        for raw in path.read_text(encoding="utf-8").splitlines()
        if (line := raw.split("#", 1)[0].strip())
    ]


def _ids_sha256(task_ids: list[str]) -> str:
    return hashlib.sha256(("\n".join(task_ids) + "\n").encode()).hexdigest()


def _git_output(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip() if result.returncode == 0 else result.stderr.strip()


def _write_structural_bundle(
    directory: Path,
    *,
    repository: Path,
    model_id: str,
    cli_version: str,
) -> Path:
    """Write a no-secret candidate solely for ALE's upstream ``--dry-run``."""
    agent = {
        "harness": "claude_code",
        "id": "claude-code--claude-opus-5--structure-only",
        "model": model_id,
        "config": {
            "provider": "openrouter",
            "base_url": None,
            "api_key": None,
            "max_turns": -1,
            "max_budget_usd": None,
            "dangerously_skip_permissions": True,
            "otel_enabled": True,
            "effort_level": "high",
            "max_thinking_tokens": None,
            # Keep ALE's explicit headless safety list visible.  This is not
            # yet accepted as equivalent to the Harbor/default tool surface.
            "disabled_tools": [
                "EnterPlanMode",
                "ExitPlanMode",
                "EnterWorktree",
                "ExitWorktree",
                "AskUserQuestion",
                "TaskOutput",
                "TaskStop",
                "RemoteTrigger",
            ],
            "cli_version": f"@anthropic-ai/claude-code@{cli_version}",
        },
    }
    environment = {
        "snapshots": {
            "cpu-free-ubuntu": {
                "provider": "docker",
                "image": "ale-ubuntu22-docker",
                "docker": {"shm_size": "2g", "resolution": [1024, 768]},
            }
        },
        "task_data_source": f"local:{repository / 'task-data'}",
        "output_path": "local",
    }
    experiment = {
        "name": "ale-cli-claude-code-structure-only",
        "agent": str(directory / "agent.yaml"),
        "environment": str(directory / "environment.yaml"),
        "tasks": str(repository / "selected_tasks" / "ale_cli.txt"),
        "output": {"root": str(directory / "output")},
        "concurrency": 1,
        "wall_time_s": None,
        "auto_resume": False,
        "max_attempts": 1,
        "cleanup_mode": "delete",
        "prompt_suffix": "",
    }
    for name, payload in (
        ("agent.yaml", agent),
        ("environment.yaml", environment),
        ("experiment.yaml", experiment),
    ):
        (directory / name).write_text(
            yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
        )
    return directory / "experiment.yaml"


def _upstream_dry_run(
    repository: Path, *, model_id: str, cli_version: str
) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory(prefix="ale-cli-structural-", dir="/tmp") as tmp:
        spec = _write_structural_bundle(
            Path(tmp),
            repository=repository,
            model_id=model_id,
            cli_version=cli_version,
        )
        env = os.environ.copy()
        current = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(repository) + (os.pathsep + current if current else "")
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "ale_run",
                "run",
                str(spec),
                "--dry-run",
                "--disable-resume",
            ],
            cwd=repository,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        combined = (result.stdout + "\n" + result.stderr).strip()
        ok = result.returncode == 0 and "units (105):" in result.stdout
        if ok:
            return True, "official loader expanded exactly 105 units"
        tail = " | ".join(combined.splitlines()[-3:]) if combined else "no output"
        return False, tail


def _study_upstream_dry_run(
    *,
    derived: Path,
    repository: Path,
    directory: Path,
    harness_id: str,
) -> tuple[bool, str]:
    """Expand 105 units through ALE with one fully-qualified study deployer."""
    directory.mkdir(parents=True)
    class_name = ALE_AGENT_CLASSES[harness_id][1]
    if harness_id == "claude-code":
        agent = {
            "class": class_name,
            "id": "study-claude-code",
            "model": "anthropic/claude-opus-5[1m]",
            "config": {
                "provider": "openrouter",
                "cli_version": "@anthropic-ai/claude-code@2.1.251",
                "effort_level": "high",
                "max_thinking_tokens": None,
                "context_tokens": 1_000_000,
                "max_output_tokens": 64_000,
            },
        }
    elif harness_id == "pi":
        agent = {
            "class": class_name,
            "id": "study-pi",
            "model": "anthropic/claude-opus-5",
            "config": {"provider": "openrouter", "cli_version": "0.84.4", "thinking": "high"},
        }
    elif harness_id == "openhands":
        agent = {
            "class": class_name,
            "id": "study-openhands",
            "model": "anthropic/claude-opus-5",
            "config": {
                "provider": "openrouter",
                "base_url": "https://openrouter.ai/api/v1",
                "sdk_version": "1.44.1",
                "tools_version": "1.44.1",
                "reasoning_effort": "high",
                "max_input_tokens": 1_000_000,
                "max_output_tokens": 128_000,
                "api_mode": "auto",
                "max_iterations": 500,
                "temperature": None,
                "top_p": None,
                "seed": None,
                "load_skills": False,
                "condenser": None,
                "mcp_policy": "ale-cua-only",
            },
        }
    elif harness_id == "deepseek-harness":
        agent = {
            "class": class_name,
            "id": "study-deepseek-harness",
            "model": "anthropic/claude-opus-5",
            "config": {
                "provider": "openrouter",
                "cli_version": "0.1.1-rc.2",
                "reasoning_effort": "high",
                "permission_mode": "danger-full-access",
                "base_url": "http://127.0.0.1:4010/v1",
                "injector_upstream": "https://openrouter.ai/api",
                "injector_port": 4010,
                "mcp_policy": "ale-cua-only",
            },
        }
    elif harness_id == "openjiuwen":
        agent = {
            "class": class_name,
            "id": "study-openjiuwen",
            "model": "deepseek/deepseek-v4-pro-0813",
            "config": {
                "provider": "openrouter",
                "version": "0.1.18",
                "runtime_version": "0.1.18-r1",
                "reasoning_effort": "high",
                "runtime_budget_rail_enabled": False,
                "context_compression_enabled": True,
                "max_outer_rounds": 8,
                "prompt_language": "en",
                "llm_request_timeout_seconds": 360,
                "llm_stream_first_chunk_timeout_seconds": 300,
                "llm_stream_idle_timeout_seconds": 300,
                "llm_http_max_retries": 5,
                "mcp_policy": "ale-cua-only",
            },
        }
    else:
        agent = {
            "class": class_name,
            "id": "study-codex",
            "model": "gpt-6-astra",
            "config": {
                "provider": "openrouter",
                "base_url": "http://127.0.0.1:4170/v1",
                "reasoning_effort": "high",
                "codex_version": "0.150.1",
                "patched_binary_url": "",
                "patched_binary_url_windows": "",
                "fork_version": "0.150.1",
                "model_catalog_path": "",
                "model_catalog_content": "",
                "feature_overrides": {},
            },
        }
    environment = {
        "snapshots": {
            "cpu-free-ubuntu": {
                "provider": "docker",
                "image": "ale-ubuntu22-docker",
                "docker": {"shm_size": "2g", "resolution": [1024, 768]},
            }
        },
        "task_data_source": f"local:{repository / 'task-data'}",
        "output_path": "local",
    }
    experiment = {
        "name": f"ale-cli-{harness_id}-study-structure-only",
        "agent": str(directory / "agent.yaml"),
        "environment": str(directory / "environment.yaml"),
        "tasks": str(repository / "selected_tasks" / "ale_cli.txt"),
        "output": {"root": str(directory / "output")},
        "concurrency": 1,
        "wall_time_s": None,
        "auto_resume": False,
        "max_attempts": 1,
        "cleanup_mode": "delete",
        "prompt_suffix": "",
    }
    for name, payload in (
        ("agent.yaml", agent),
        ("environment.yaml", environment),
        ("experiment.yaml", experiment),
    ):
        (directory / name).write_text(yaml.safe_dump(payload, sort_keys=False))
    env = os.environ.copy()
    env["PYTHONPATH"] = str(derived)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ale_run",
            "run",
            str(directory / "experiment.yaml"),
            "--dry-run",
            "--disable-resume",
        ],
        cwd=repository,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode == 0 and "units (105):" in result.stdout:
        return True, "fully-qualified deployer expanded exactly 105 units"
    combined = (result.stdout + "\n" + result.stderr).strip()
    return False, " | ".join(combined.splitlines()[-3:]) or "no output"


def benchmark_doctor(
    config: dict[str, Any], config_path: Path
) -> int:
    """Validate the pinned ALE checkout and its 105-task CLI selection."""
    runner = config["runners"]["ale"]
    benchmark = next(item for item in config["benchmarks"] if item["id"] == "ale-cli")
    repository = ROOT / runner["repository_path"]
    full_list = repository / benchmark["selected_tasks_path"]
    docker_list = repository / benchmark["local_docker"]["selected_tasks_path"]
    checks: list[Check] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append(Check(name, ok, detail))

    add("repository", repository.is_dir(), str(repository))
    if not repository.is_dir():
        _print_report(config_path, checks)
        return 2

    head = _git_output(repository, "rev-parse", "HEAD")
    add("source-commit", head == runner["commit"], head)
    # The official local-Docker workflow requires the gated, untracked
    # task-data/ tree inside the pinned checkout.  Exclude only that runtime
    # asset while continuing to reject every source/config modification.
    status = _git_output(
        repository,
        "status",
        "--short",
        "--untracked-files=all",
        "--",
        ".",
        ":(exclude)task-data",
    )
    add("source-clean", status == "", status or "clean (task-data excluded)")

    if not full_list.is_file() or not docker_list.is_file():
        add("selected-lists", False, "one or both selected task files are missing")
        _print_report(config_path, checks)
        return 2

    full_ids = _selected_ids(full_list)
    docker_ids = _selected_ids(docker_list)
    add(
        "cli-list-file",
        _sha256(full_list) == benchmark["selected_tasks_file_sha256"],
        f"{len(full_ids)} tasks; sha256={_sha256(full_list)}",
    )
    add(
        "cli-task-ids",
        len(full_ids) == benchmark["tasks"]
        and len(full_ids) == len(set(full_ids))
        and _ids_sha256(full_ids) == benchmark["task_id_sha256"],
        _ids_sha256(full_ids),
    )
    add(
        "docker-list-file",
        _sha256(docker_list)
        == benchmark["local_docker"]["selected_tasks_file_sha256"],
        f"{len(docker_ids)} tasks; sha256={_sha256(docker_list)}",
    )
    add(
        "docker-task-ids",
        len(docker_ids) == benchmark["local_docker"]["tasks"]
        and len(docker_ids) == len(set(docker_ids))
        and _ids_sha256(docker_ids)
        == benchmark["local_docker"]["task_id_sha256"],
        _ids_sha256(docker_ids),
    )

    excluded = sorted(set(full_ids) - set(docker_ids))
    add(
        "docker-is-subset",
        set(docker_ids) <= set(full_ids)
        and excluded == sorted(benchmark["local_docker"]["excluded_tasks"]),
        f"99 formal local-Docker / 105 upstream; excluded={','.join(excluded)}",
    )

    snapshots: Counter[str | None] = Counter()
    timeouts: Counter[int | None] = Counter()
    machines: Counter[str | None] = Counter()
    missing: list[str] = []
    for task_id in full_ids:
        card = repository / "tasks" / task_id / "task_card.json"
        if not card.is_file():
            missing.append(task_id)
            continue
        payload = json.loads(card.read_text(encoding="utf-8"))
        vm = payload.get("vm") or {}
        snapshots[vm.get("snapshot")] += 1
        timeouts[vm.get("timeout")] += 1
        machines[vm.get("machineType")] += 1
    add("task-cards", not missing, "all present" if not missing else ",".join(missing))
    add(
        "snapshot",
        snapshots == Counter({"cpu-free-ubuntu": 105}),
        repr(dict(snapshots)),
    )
    add(
        "task-timeouts",
        timeouts == Counter({7200: 99, 14400: 3, 28800: 2, None: 1}),
        f"{dict(timeouts)}; missing task timeout uses ALE 7200s default",
    )
    add(
        "machine-types",
        machines
        == Counter(
            {
                "c4-standard-4": 100,
                "c4-standard-16": 3,
                "c4-highmem-4": 1,
                "c4-standard-8": 1,
            }
        ),
        repr(dict(machines)),
    )

    harness = next(item for item in config["harnesses"] if item["id"] == "claude-code")
    model = next(item for item in config["models"] if item["id"] == "claude-opus-5")
    structural_model = model["openrouter"]["model_id"] + "[1m]"
    dry_ok, dry_detail = _upstream_dry_run(
        repository,
        model_id=structural_model,
        cli_version=str(harness["version"]),
    )
    add("upstream-dry-run", dry_ok, dry_detail)

    _print_report(config_path, checks)
    print("\nDeployer handoff (not benchmark qualification):")
    for harness_id, status_name, detail in DEPLOYER_HANDOFF:
        print(f"  {harness_id:18} {status_name:34} {detail}")
    print(
        "\nBoundary       source/list/loader only; no image pull, sandbox, task-data "
        "staging, grader, harness launch, or model API call"
    )
    print(
        "Qualification  structural doctor does not assign C/T/B; read "
        "reports/provider-compatibility.md for the current audited evidence"
    )
    return 0 if all(check.ok for check in checks) else 2


def _print_report(config_path: Path, checks: list[Check]) -> None:
    print(f"Config        {config_path}")
    print("Benchmark     ale-cli")
    for check in checks:
        print(f"{'OK' if check.ok else 'FAIL':4} {check.name:18} {check.detail}")
