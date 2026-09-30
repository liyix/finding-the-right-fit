"""Harbor v0.22.0 adapter for the study's openJiuwen Coding Agent candidate.

The agent composition lives in :mod:`integrations.openjiuwen_agent`.  This
module binds a pinned, host-prepared runtime into Harbor's task container and
exports native telemetry.  Preparing the runtime is the only networked
installation step; individual trials never run apt, curl, uv, or pip.  It does
not change the existing five-harness study matrix until qualification.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Literal, override

from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.agents.model_connection import ModelConnectionSpec, ResolvedModelConnection
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext


OPENJIUWEN_VERSION = "0.1.18"
OPENJIUWEN_TAG_COMMIT = "1d37ae3007f9df9a8489a7ab271141b03be08f66"
OPENJIUWEN_PYTHON_VERSION = "3.12.12"
_RUNNER = Path(__file__).with_name("openjiuwen_agent.py")
_RUNNER_SHA256 = "1895a50f963c5275259ade7dbb09b57e44d033c1cd041a7bcfc5d52789dc0339"
_RUNTIME_LOCK = Path(__file__).with_name("openjiuwen-runtime.lock")
_LOCAL_RUNTIME = (
    Path(__file__).resolve().parents[1]
    / ".cache"
    / "openjiuwen"
    / "runtime-0.1.18-r1"
)
_REMOTE_RUNTIME = PurePosixPath("/opt/openjiuwen-runtime")
_REMOTE_PYTHON = _REMOTE_RUNTIME / "python/bin/python3.12"
_REMOTE_SITE_PACKAGES = _REMOTE_RUNTIME / "site-packages"
_REMOTE_CA_CERT = _REMOTE_SITE_PACKAGES / "certifi/cacert.pem"
_REMOTE_CA_CERT_SHA256 = (
    "9cc2a774b5198dcff14d9be1e66091f538975d867ce029a96bce15a55dfd730f"
)
_REMOTE_RUNTIME_METADATA = _REMOTE_RUNTIME / "runtime.json"
_REMOTE_RUNNER = PurePosixPath("/installed-agent/openjiuwen_agent.py")
_REMOTE_CONFIG = PurePosixPath("/installed-agent/openjiuwen-config.json")
_REMOTE_INSTRUCTION = PurePosixPath("/installed-agent/openjiuwen-instruction.txt")
_REMOTE_API_KEY = PurePosixPath("/installed-agent/openjiuwen-api-key")
_REMOTE_API_KEY_FIFO = PurePosixPath("/installed-agent/openjiuwen-api-key.fifo")
_REMOTE_LOGS = PurePosixPath("/logs/agent/openjiuwen")
_OUTPUT_FILENAME = "openjiuwen.txt"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _runtime_metadata(runtime: Path) -> dict[str, Any]:
    return {
        "format": 1,
        "openjiuwen_version": OPENJIUWEN_VERSION,
        "openjiuwen_tag_commit": OPENJIUWEN_TAG_COMMIT,
        "python_version": OPENJIUWEN_PYTHON_VERSION,
        "python_sha256": _sha256(runtime / "python/bin/python3.12"),
        "ripgrep_sha256": _sha256(runtime / "bin/rg"),
        "dependency_lock_sha256": _sha256(_RUNTIME_LOCK),
        "openjiuwen_record_sha256": _sha256(
            runtime
            / "site-packages"
            / f"openjiuwen-{OPENJIUWEN_VERSION}.dist-info"
            / "RECORD"
        ),
    }


def prepare_runtime(*, force: bool = False) -> Path:
    """Build the local runtime once; no model API is contacted."""

    if not _RUNTIME_LOCK.is_file():
        raise FileNotFoundError(f"missing dependency lock: {_RUNTIME_LOCK}")
    if _LOCAL_RUNTIME.exists() and not force:
        metadata_path = _LOCAL_RUNTIME / "runtime.json"
        if not metadata_path.is_file():
            raise RuntimeError(
                f"incomplete runtime at {_LOCAL_RUNTIME}; rerun with --force"
            )
        recorded = json.loads(metadata_path.read_text(encoding="utf-8"))
        expected = _runtime_metadata(_LOCAL_RUNTIME)
        if recorded != expected:
            raise RuntimeError(
                f"runtime integrity check failed at {_LOCAL_RUNTIME}; rerun with --force"
            )
        return _LOCAL_RUNTIME

    uv = shutil.which("uv")
    rg = shutil.which("rg")
    if uv is None or rg is None:
        raise RuntimeError("runtime preparation requires host uv and rg executables")
    cache_dir = _LOCAL_RUNTIME.parents[1] / "uv"
    env = {**os.environ, "UV_CACHE_DIR": str(cache_dir)}
    subprocess.run(
        [uv, "python", "install", OPENJIUWEN_PYTHON_VERSION],
        check=True,
        env=env,
    )
    uv_python_dir = Path(
        subprocess.check_output([uv, "python", "dir"], text=True, env=env).strip()
    )
    candidates = sorted(
        uv_python_dir.glob(
            f"cpython-{OPENJIUWEN_PYTHON_VERSION}-linux-x86_64-gnu/bin/python3.12"
        )
    )
    if len(candidates) != 1:
        raise RuntimeError(
            "expected exactly one uv-managed Linux x86_64 Python "
            f"{OPENJIUWEN_PYTHON_VERSION}; found {candidates}"
        )

    _LOCAL_RUNTIME.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="openjiuwen-runtime-", dir=_LOCAL_RUNTIME.parent
    ) as staging_text:
        staging = Path(staging_text)
        shutil.copytree(candidates[0].parents[1], staging / "python")
        (staging / "bin").mkdir()
        shutil.copy2(rg, staging / "bin/rg")
        (staging / "site-packages").mkdir()
        subprocess.run(
            [
                uv,
                "pip",
                "install",
                "--python",
                str(staging / "python/bin/python3.12"),
                "--target",
                str(staging / "site-packages"),
                "-r",
                str(_RUNTIME_LOCK),
            ],
            check=True,
            env=env,
        )
        (staging / "runtime.json").write_text(
            json.dumps(_runtime_metadata(staging), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        # tempfile creates the staging root as 0700.  Several official TB4
        # images run as a non-root USER, so the bind-mounted runtime root must
        # be traversable even though the mount itself remains read-only.
        staging.chmod(0o755)
        if _LOCAL_RUNTIME.exists():
            shutil.rmtree(_LOCAL_RUNTIME)
        staging.rename(_LOCAL_RUNTIME)
    return _LOCAL_RUNTIME


def validate_job_concurrency(config: dict[str, Any]) -> None:
    """Reject the accidental outer-parallel/agent-serial configuration."""

    outer = int(config.get("n_concurrent_trials", 4))
    agents = config.get("agents")
    if not isinstance(agents, list) or len(agents) != 1:
        raise ValueError("openJiuwen candidate jobs require exactly one agent config")
    cap = agents[0].get("n_concurrent")
    if cap is not None and int(cap) != outer:
        raise ValueError(
            "openJiuwen concurrency mismatch: omit agents[0].n_concurrent or "
            f"set it to n_concurrent_trials={outer}; got {cap}"
        )


def _api_key_env_name(access: ResolvedModelConnection) -> str:
    if access.api_key is None:
        raise ValueError("openJiuwen requires an OpenRouter API key")
    for name, value in sorted(access.env.items()):
        if value == access.api_key:
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None:
                raise ValueError("invalid API-key environment variable name")
            return name
    raise ValueError("openJiuwen requires an API-key environment reference")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


class OpenJiuwenCodingAgent(BaseInstalledAgent):
    """Explicit, text-first Coding Agent assembled from openJiuwen Rails."""

    MODEL_CONNECTION = ModelConnectionSpec(passthrough=True)

    def __init__(
        self,
        *args: Any,
        context_window: int,
        openrouter_route: dict[str, Any],
        runtime_budget_seconds: int,
        completion_timeout_seconds: int,
        runtime_budget_rail_enabled: bool = True,
        context_compression_enabled: bool = True,
        max_outer_rounds: int = 8,
        llm_request_timeout_seconds: int = 360,
        llm_stream_first_chunk_timeout_seconds: int = 300,
        llm_stream_idle_timeout_seconds: int = 300,
        llm_http_max_retries: int = 5,
        prompt_language: Literal["en"] = "en",
        python_version: str = OPENJIUWEN_PYTHON_VERSION,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        if self._version not in (None, OPENJIUWEN_VERSION):
            raise ValueError(
                f"openJiuwen is pinned to {OPENJIUWEN_VERSION}; got {self._version!r}"
            )
        self._version = OPENJIUWEN_VERSION
        self._context_window = int(context_window)
        self._runtime_budget_seconds = int(runtime_budget_seconds)
        self._completion_timeout_seconds = int(completion_timeout_seconds)
        if not isinstance(runtime_budget_rail_enabled, bool):
            raise ValueError("runtime_budget_rail_enabled must be boolean")
        self._runtime_budget_rail_enabled = runtime_budget_rail_enabled
        if context_compression_enabled is not True:
            raise ValueError(
                "the registered openJiuwen path requires native context compression"
            )
        self._context_compression_enabled = True
        self._max_outer_rounds = int(max_outer_rounds)
        self._llm_request_timeout_seconds = int(llm_request_timeout_seconds)
        self._llm_stream_first_chunk_timeout_seconds = int(
            llm_stream_first_chunk_timeout_seconds
        )
        self._llm_stream_idle_timeout_seconds = int(
            llm_stream_idle_timeout_seconds
        )
        self._llm_http_max_retries = int(llm_http_max_retries)
        self._prompt_language = prompt_language
        self._python_version = str(python_version)
        self._route = dict(openrouter_route)
        self._agent_owner: str | None = None
        self._task_workdir: str | None = None

        only = self._route.get("only")
        if not isinstance(only, list) or not only:
            raise ValueError("OpenRouter routing requires a non-empty provider.only")
        if self._route.get("allow_fallbacks") is not False:
            raise ValueError("OpenRouter routing requires allow_fallbacks=false")
        if self._route.get("require_parameters") is not True:
            raise ValueError("OpenRouter routing requires require_parameters=true")
        if self._context_window <= 0:
            raise ValueError("context_window must be positive")
        if self._runtime_budget_seconds <= 0 or self._completion_timeout_seconds <= 0:
            raise ValueError("runtime budgets must be positive")
        if self._completion_timeout_seconds > self._runtime_budget_seconds:
            raise ValueError("completion timeout cannot exceed the runtime budget")
        if (
            self._llm_request_timeout_seconds <= 0
            or self._llm_stream_first_chunk_timeout_seconds <= 0
            or self._llm_stream_idle_timeout_seconds <= 0
        ):
            raise ValueError("openJiuwen LLM timeouts must be positive")
        if self._llm_http_max_retries < 0:
            raise ValueError("openJiuwen HTTP retries must be non-negative")
        if self._max_outer_rounds <= 1:
            raise ValueError("max_outer_rounds must allow a confirmation round")
        if self._prompt_language != "en":
            raise ValueError("the English benchmark configuration requires prompt_language=en")
        if self._python_version != OPENJIUWEN_PYTHON_VERSION:
            raise ValueError(
                f"openJiuwen runtime requires Python {OPENJIUWEN_PYTHON_VERSION}"
            )

    @staticmethod
    @override
    def name() -> str:
        return "openjiuwen-coding-agent"

    @override
    def get_version_command(self) -> str | None:
        return (
            f"PYTHONPATH={_REMOTE_SITE_PACKAGES} {_REMOTE_PYTHON} -c "
            "'import importlib.metadata; "
            "print(importlib.metadata.version(\"openjiuwen\"))'"
        )

    @override
    def parse_version(self, stdout: str) -> str:
        return stdout.strip().splitlines()[-1].strip()

    async def _resolve_agent_owner(self, environment: BaseEnvironment) -> str:
        if self._agent_owner is not None:
            return self._agent_owner
        result = await self.exec_as_agent(environment, command="id -u && id -g")
        values = (result.stdout or "").splitlines()
        if len(values) != 2 or not all(value.strip().isdigit() for value in values):
            raise RuntimeError(f"could not resolve task image user identity: {values!r}")
        self._agent_owner = f"{values[0].strip()}:{values[1].strip()}"
        return self._agent_owner

    async def _resolve_task_workdir(self, environment: BaseEnvironment) -> str:
        """Return the task image's native workdir without assuming ``/app``."""
        if self._task_workdir is not None:
            return self._task_workdir
        result = await self.exec_as_agent(environment, command="pwd -P")
        values = (result.stdout or "").splitlines()
        if len(values) != 1:
            raise RuntimeError(f"could not resolve task image workdir: {values!r}")
        workdir = values[0].strip()
        path = PurePosixPath(workdir)
        if not workdir.startswith("/") or ".." in path.parts:
            raise RuntimeError(f"invalid task image workdir: {workdir!r}")
        self._task_workdir = path.as_posix()
        return self._task_workdir

    async def _upload_source(
        self, environment: BaseEnvironment, source: Path, remote: PurePosixPath
    ) -> None:
        await environment.upload_file(source, remote.as_posix())
        target = shlex.quote(remote.as_posix())
        owner = shlex.quote(await self._resolve_agent_owner(environment))
        await self.exec_as_root(
            environment, command=f"chown {owner} {target} && chmod 700 {target}"
        )

    @override
    async def _upload_config_text(
        self,
        environment: BaseEnvironment,
        *,
        content: str,
        remote_path: str,
        filename: str,
    ) -> None:
        await super()._upload_config_text(
            environment,
            content=content,
            remote_path=remote_path,
            filename=filename,
        )
        owner = shlex.quote(await self._resolve_agent_owner(environment))
        target = shlex.quote(remote_path)
        await self.exec_as_root(
            environment, command=f"chown {owner} {target} && chmod 600 {target}"
        )

    async def _upload_root_secret(
        self, environment: BaseEnvironment, secret: str
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="openjiuwen-secret-") as temp_dir:
            local_path = Path(temp_dir) / _REMOTE_API_KEY.name
            local_path.write_text(secret + "\n", encoding="utf-8")
            local_path.chmod(0o600)
            await environment.upload_file(local_path, _REMOTE_API_KEY.as_posix())
        target = shlex.quote(_REMOTE_API_KEY.as_posix())
        await self.exec_as_root(
            environment, command=f"chown root:root {target} && chmod 600 {target}"
        )

    async def _run_with_secret_pipe(
        self,
        environment: BaseEnvironment,
        *,
        command: str,
        cwd: str,
        timeout_sec: int,
    ) -> None:
        if "{API_KEY_FIFO}" not in command:
            raise ValueError("secret-pipe command must consume {API_KEY_FIFO}")
        owner = shlex.quote(await self._resolve_agent_owner(environment))
        secret_path = shlex.quote(_REMOTE_API_KEY.as_posix())
        fifo_path = shlex.quote(_REMOTE_API_KEY_FIFO.as_posix())
        await self.exec_as_root(
            environment,
            command=(
                f"set -euo pipefail; rm -f {fifo_path}; mkfifo {fifo_path}; "
                f"chown {owner} {fifo_path}; chmod 600 {fifo_path}; "
                f"test -r {secret_path}"
            ),
        )
        writer = asyncio.create_task(self.exec_as_root(
            environment,
            command=(
                f"set -euo pipefail; cat {secret_path} > {fifo_path}; "
                f"rm -f {secret_path} {fifo_path}"
            ),
            timeout_sec=30,
        ))
        agent = asyncio.create_task(self.exec_as_agent(
            environment,
            command=(
                "set -euo pipefail; "
                + command.replace("{API_KEY_FIFO}", fifo_path)
            ),
            cwd=cwd,
            timeout_sec=timeout_sec,
        ))
        try:
            await writer
            await self.exec_as_root(
                environment,
                command=f"test ! -e {secret_path} && test ! -e {fifo_path}",
            )
            await agent
        except BaseException:
            agent.cancel()
            raise
        finally:
            await self.exec_as_root(
                environment, command=f"rm -f {secret_path} {fifo_path}"
            )

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        actual_runner_sha256 = hashlib.sha256(_RUNNER.read_bytes()).hexdigest()
        if actual_runner_sha256 != _RUNNER_SHA256:
            raise ValueError("openJiuwen candidate runner does not match its frozen hash")
        if not _RUNTIME_LOCK.is_file():
            raise FileNotFoundError(f"missing openJiuwen dependency lock: {_RUNTIME_LOCK}")
        expected_runtime = {
            "format": 1,
            "openjiuwen_version": OPENJIUWEN_VERSION,
            "openjiuwen_tag_commit": OPENJIUWEN_TAG_COMMIT,
            "python_version": OPENJIUWEN_PYTHON_VERSION,
            "dependency_lock_sha256": _sha256(_RUNTIME_LOCK),
        }
        quoted_owner = shlex.quote(await self._resolve_agent_owner(environment))
        await self.exec_as_root(
            environment,
            command=(
                f"mkdir -p {_REMOTE_LOGS} && "
                f"chown -R {quoted_owner} {_REMOTE_LOGS}"
            ),
        )
        expected_json = shlex.quote(json.dumps(expected_runtime, sort_keys=True))
        await self.exec_as_agent(
            environment,
            command=(
                "set -euo pipefail; "
                f"cd {_REMOTE_LOGS}; "
                f"test -x {_REMOTE_PYTHON} && "
                f"test -x {_REMOTE_RUNTIME}/bin/rg && "
                f"test -r {_REMOTE_RUNTIME_METADATA} && "
                f"test -r {_REMOTE_CA_CERT} && "
                f"PYTHONPATH={_REMOTE_SITE_PACKAGES} {_REMOTE_PYTHON} -c "
                f"'import hashlib,sys; actual=hashlib.sha256(open(sys.argv[1], "
                f"\"rb\").read()).hexdigest(); assert actual == sys.argv[2], "
                f"(actual, sys.argv[2])' {_REMOTE_CA_CERT} "
                f"{_REMOTE_CA_CERT_SHA256} && "
                f"PYTHONPATH={_REMOTE_SITE_PACKAGES} {_REMOTE_PYTHON} -c "
                f"'import json,sys; actual=json.load(open(sys.argv[1])); "
                f"expected=json.loads(sys.argv[2]); "
                f"assert all(actual.get(k) == v for k,v in expected.items()), "
                f"(actual, expected)' {_REMOTE_RUNTIME_METADATA} {expected_json} && "
                f"{self.get_version_command()} && "
                f"PYTHONPATH={_REMOTE_SITE_PACKAGES} {_REMOTE_PYTHON} -c "
                "'from openjiuwen.harness import create_deep_agent'"
            ),
        )
        await self._upload_source(environment, _RUNNER, _REMOTE_RUNNER)
        # Exercise the installed package, filesystem tools, SSE parser,
        # reasoning replay and both completion confirmations without paid I/O.
        await self.exec_as_agent(
            environment,
            command=(
                f"cd {_REMOTE_LOGS} && "
                f"export PYTHONPATH={_REMOTE_SITE_PACKAGES}; "
                f"export PATH={_REMOTE_RUNTIME}/bin:$PATH; "
                f"{_REMOTE_PYTHON} {_REMOTE_RUNNER} --self-test "
                f"--self-test-output {_REMOTE_LOGS}/self-test.json"
            ),
            timeout_sec=120,
        )
        await self._upload_root_secret(environment, "credential-handoff-self-test")
        record = _REMOTE_LOGS / "credential-handoff-self-test.json"
        await self._run_with_secret_pipe(
            environment,
            command=(
                "IFS= read -r OJ_TEST_SECRET < {API_KEY_FIFO}; "
                "test \"$OJ_TEST_SECRET\" = credential-handoff-self-test; "
                "test -z \"${OPENROUTER_API_KEY:-}\"; "
                f"printf '%s\\n' '{{\"status\":\"passed\","
                "\"transport\":\"root-file-to-fifo-to-stdin\","
                "\"environment_key_visible\":false}' > "
                f"{shlex.quote(record.as_posix())}"
            ),
            cwd=_REMOTE_LOGS.as_posix(),
            timeout_sec=30,
        )

    def _resolved_config(self) -> tuple[dict[str, Any], str]:
        if not self.model_name or not self.model_name.startswith("openrouter/"):
            raise ValueError("openJiuwen study candidate requires openrouter/<model-id>")
        model_id = self.model_name.removeprefix("openrouter/")
        access = self.model_connection
        if access.provider != "openrouter":
            raise ValueError("openJiuwen study candidate requires provider=openrouter")
        base_url = access.base_url
        if base_url is None or not base_url.rstrip("/").endswith("openrouter.ai/api/v1"):
            raise ValueError("openJiuwen study candidate requires the official OpenRouter v1 base URL")
        key = access.api_key
        if not isinstance(key, str) or not key or any(c in key for c in ("\x00", "\r", "\n")):
            raise ValueError("invalid OpenRouter API key")
        quantizations = self._route.get("quantizations")
        config = {
            "model_id": model_id,
            "provider_only": list(self._route["only"]),
            "quantizations": list(quantizations) if quantizations else None,
            "context_window": self._context_window,
            "reasoning_effort": "high",
            "runtime_budget_seconds": self._runtime_budget_seconds,
            "runtime_budget_rail_enabled": self._runtime_budget_rail_enabled,
            "context_compression_enabled": self._context_compression_enabled,
            "completion_timeout_seconds": self._completion_timeout_seconds,
            "max_outer_rounds": self._max_outer_rounds,
            "prompt_language": self._prompt_language,
            "llm_request_timeout_seconds": self._llm_request_timeout_seconds,
            "llm_stream_first_chunk_timeout_seconds": (
                self._llm_stream_first_chunk_timeout_seconds
            ),
            "llm_stream_idle_timeout_seconds": self._llm_stream_idle_timeout_seconds,
            "llm_http_max_retries": self._llm_http_max_retries,
            "llm_ssl_cert_path": _REMOTE_CA_CERT.as_posix(),
            "base_url": base_url,
        }
        return config, key

    @override
    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        config, api_key = self._resolved_config()
        task_workdir = await self._resolve_task_workdir(environment)
        # A TB4 trial already runs in an isolated per-task container. Keep the
        # image's native cwd for relative shell/file operations, while using
        # that isolated container root as the filesystem sandbox boundary so
        # benchmark-required absolute paths are not spuriously rejected.
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
            f"--workspace {shlex.quote(task_workdir)} "
            f"--log-dir {_REMOTE_LOGS} "
            "--api-key-stdin < {API_KEY_FIFO} "
            f"2>&1 | stdbuf -oL tee {_REMOTE_LOGS.parent / _OUTPUT_FILENAME}"
        )
        await self._run_with_secret_pipe(
            environment,
            command=command,
            cwd=task_workdir,
            timeout_sec=self._runtime_budget_seconds,
        )

    @override
    def populate_context_post_run(self, context: AgentContext) -> None:
        transcript = _read_json(self.logs_dir / "openjiuwen" / "native-transcript.json")
        if not isinstance(transcript, list):
            return
        input_tokens = 0
        output_tokens = 0
        cache_tokens = 0
        total_cost = 0.0
        model_calls = 0
        for message in transcript:
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            usage = message.get("usage_metadata")
            if not isinstance(usage, dict):
                continue
            model_calls += 1
            input_tokens += int(usage.get("input_tokens") or 0)
            output_tokens += int(usage.get("output_tokens") or 0)
            cache_tokens += int(
                usage.get("cache_read_tokens")
                if usage.get("cache_read_tokens") is not None
                else usage.get("cache_tokens") or 0
            )
            total_cost += float(usage.get("total_cost") or 0.0)
        context.n_input_tokens = input_tokens
        context.n_output_tokens = output_tokens
        context.n_cache_tokens = cache_tokens
        context.cost_usd = total_cost if total_cost > 0 else None
        events_path = self.logs_dir / "openjiuwen" / "native-events.jsonl"
        event_counts: dict[str, int] = {}
        if events_path.exists():
            with events_path.open(
                "r", encoding="utf-8", errors="replace"
            ) as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    name = str(event.get("event") or "unknown")
                    event_counts[name] = event_counts.get(name, 0) + 1
        wire_generations_path = (
            self.logs_dir / "openjiuwen" / "wire-generations.jsonl"
        )
        early_generation_ids: set[str] = set()
        if wire_generations_path.exists():
            for line in wire_generations_path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                response_id = str(row.get("response_id") or "")
                if response_id:
                    early_generation_ids.add(response_id)
        context.metadata = {
            **(context.metadata or {}),
            "openjiuwen": {
                "version": OPENJIUWEN_VERSION,
                "source_commit": OPENJIUWEN_TAG_COMMIT,
                "model_calls_with_usage": model_calls,
                "native_event_counts": event_counts,
                "early_wire_generation_count": len(early_generation_ids),
                "cost_source": "openjiuwen-sdk-secondary-provider-authoritative",
            },
        }


__all__ = [
    "OpenJiuwenCodingAgent",
    "prepare_runtime",
    "validate_job_concurrency",
]
