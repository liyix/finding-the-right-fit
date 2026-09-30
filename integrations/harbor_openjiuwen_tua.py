"""TUA lifecycle binding for the study OpenJiuwen Coding Agent.

TUA system-administration tasks intentionally operate outside ``/app``.  The
Docker task container is therefore the filesystem security boundary, matching
the existing ALE lifecycle binding, while the TB4 adapter remains unchanged at
``/app``.
"""

from __future__ import annotations

import json
import shlex
from typing import override

from harbor.agents.installed.base import with_prompt_template
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from integrations.harbor_openjiuwen import (
    OpenJiuwenCodingAgent,
    _OUTPUT_FILENAME,
    _REMOTE_CONFIG,
    _REMOTE_INSTRUCTION,
    _REMOTE_LOGS,
    _REMOTE_PYTHON,
    _REMOTE_RUNNER,
    _REMOTE_RUNTIME,
    _REMOTE_SITE_PACKAGES,
)


class TUAOpenJiuwenCodingAgent(OpenJiuwenCodingAgent):
    """Run the unchanged candidate composition against the full TUA container."""

    @override
    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        config, api_key = self._resolved_config()
        config["project_root"] = "/"
        await self._upload_config_text(
            environment,
            content=json.dumps(config, indent=2, sort_keys=True) + "\n",
            remote_path=_REMOTE_CONFIG.as_posix(),
            filename=_REMOTE_CONFIG.name,
        )
        await self._upload_config_text(
            environment,
            content=instruction,
            remote_path=_REMOTE_INSTRUCTION.as_posix(),
            filename=_REMOTE_INSTRUCTION.name,
        )
        await self._upload_root_secret(environment, api_key)
        command = (
            f"cd {_REMOTE_LOGS}; "
            f"export PYTHONPATH={_REMOTE_SITE_PACKAGES}; "
            f"export PATH={_REMOTE_RUNTIME}/bin:$PATH; "
            "env -u OPENROUTER_API_KEY -u USE_RL_ONLINE_RAIL "
            "-u DEFAULT_TOOL_CALL_TIMEOUT "
            "-u BASH_TOOL_MAX_TIMEOUT_SECONDS -u BASH_TOOL_MAX_OUTPUT_CHARS "
            "-u OPENJIUWEN_BASH_STRICT "
            f"{_REMOTE_PYTHON} {_REMOTE_RUNNER} "
            f"--config {shlex.quote(_REMOTE_CONFIG.as_posix())} "
            f"--instruction-file {shlex.quote(_REMOTE_INSTRUCTION.as_posix())} "
            "--workspace / "
            f"--log-dir {_REMOTE_LOGS} "
            "--api-key-stdin < {API_KEY_FIFO} "
            f"2>&1 | stdbuf -oL tee {_REMOTE_LOGS.parent / _OUTPUT_FILENAME}"
        )
        await self._run_with_secret_pipe(
            environment,
            command=command,
            cwd="/",
            timeout_sec=self._runtime_budget_seconds,
        )


__all__ = ["TUAOpenJiuwenCodingAgent"]
