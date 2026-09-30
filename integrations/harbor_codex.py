"""Thin Harbor v0.22.0 compatibility overlay for Codex on OpenRouter.

Harbor's built-in Codex agent still owns installation, prompting, tools, the
agent loop, and trajectory conversion.  This overlay only materializes one
shared model-catalog alias from the pinned Codex binary so every study model
uses the same GPT-5.6-Sol-derived public Responses metadata.  The proxy outside
the task container maps that alias to the approved OpenRouter model/provider.
"""

from copy import deepcopy
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import shlex
from typing import Any, Iterator, override

from harbor.agents.installed.codex import Codex
from harbor.environments.base import BaseEnvironment
from harbor.models.trial.paths import EnvironmentPaths


COMPAT_MODEL = "openrouter-eval"
CATALOG_PATH = "/tmp/codex-home/openrouter-models.json"
CATALOG_AUDIT_PATH = (
    EnvironmentPaths.agent_dir / "resolved-openrouter-model-catalog.json"
).as_posix()


@contextmanager
def _optional_install_lock() -> Iterator[None]:
    """Serialize only runtime installation across independent Harbor jobs.

    Each Harbor worker is a separate host process, so a normal asyncio lock
    would not prevent five task containers from hitting the package mirror at
    once.  The lock is opt-in and ends before the agent/model phase; inference
    remains parallel and the harness behavior is unchanged.
    """
    lock_path = os.environ.get("HARNESS_CODEX_INSTALL_LOCK")
    if not lock_path:
        yield
        return
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


_BUILD_CATALOG_JS = r"""
const cp = require("child_process");
const fs = require("fs");
const catalog = JSON.parse(cp.execFileSync(
  "codex", ["debug", "models", "--bundled"], {encoding: "utf8"}
));
const source = catalog.models.find((entry) => entry.slug === "gpt-6-astra");
if (!source) throw new Error("pinned Codex catalog lacks gpt-6-astra");
const model = JSON.parse(JSON.stringify(source));
model.slug = "openrouter-eval";
model.display_name = "OpenRouter evaluation compatibility model";
model.context_window = 1000000;
model.max_context_window = 1000000;
model.effective_context_window_percent = 95;
model.use_responses_lite = false;
model.tool_mode = null;
const output = JSON.stringify({models: [model]});
fs.mkdirSync("/tmp/codex-home", {recursive: true});
fs.writeFileSync("/tmp/codex-home/openrouter-models.json", output + "\n", {mode: 0o600});
""".strip()


class ControlledCodex(Codex):
    """Pinned upstream Codex plus one audited, shared compatibility catalog."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        model_name = kwargs.get("model_name")
        if model_name not in {None, COMPAT_MODEL, f"openai/{COMPAT_MODEL}"}:
            raise ValueError(
                f"ControlledCodex requires model_name={COMPAT_MODEL!r}; got {model_name!r}"
            )
        super().__init__(*args, **kwargs)

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        """Install upstream Codex, then qualify the exact catalog command.

        Harbor installs Codex under the agent user's nvm tree on glibc images.
        Its normal ``run`` command sources nvm before invoking Codex, but our
        pre-run catalog materialization must do the same.  Keeping this check
        in ``install`` makes Harbor's zero-cost ``install_only`` mode exercise
        the compatibility step as well as the package installation.
        """
        with _optional_install_lock():
            await super().install(environment)
            await self.exec_as_agent(
                environment,
                command=(
                    "if [ -s ~/.nvm/nvm.sh ]; then . ~/.nvm/nvm.sh; fi; "
                    f"node -e {shlex.quote(_BUILD_CATALOG_JS)}"
                ),
            )

    @override
    async def _upload_effective_config(
        self,
        environment: BaseEnvironment,
        config: dict[str, Any],
        remote_path: str,
    ) -> None:
        await self.exec_as_agent(
            environment,
            command=(
                "if [ -s ~/.nvm/nvm.sh ]; then . ~/.nvm/nvm.sh; fi; "
                f"node -e {shlex.quote(_BUILD_CATALOG_JS)}"
            ),
        )
        await self.exec_as_agent(
            environment,
            command=(
                f"cp {shlex.quote(CATALOG_PATH)} "
                f"{shlex.quote(CATALOG_AUDIT_PATH)}"
            ),
        )
        controlled = deepcopy(config)
        existing = controlled.get("model_catalog_json")
        if existing not in (None, CATALOG_PATH):
            raise ValueError(
                "ControlledCodex refuses a second model_catalog_json override"
            )
        controlled["model_catalog_json"] = CATALOG_PATH
        await super()._upload_effective_config(environment, controlled, remote_path)


class NativeGPTCodex(Codex):
    """Pinned upstream Codex using its bundled GPT-5.6-Sol metadata unchanged.

    This class adds no prompt, catalog, tool, or model control.  It exists only
    to retain the cross-process package-install lock used by shared Harbor
    workers while rejecting accidental use with a non-home-field model.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        model_name = kwargs.get("model_name")
        if model_name not in {None, "gpt-6-astra", "openai/gpt-6-astra"}:
            raise ValueError(
                "NativeGPTCodex requires model_name='gpt-6-astra'; "
                f"got {model_name!r}"
            )
        super().__init__(*args, **kwargs)

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        with _optional_install_lock():
            await super().install(environment)
