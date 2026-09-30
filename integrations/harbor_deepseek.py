"""Harbor v0.22.0 thin installed-agent integration for DSH rc.2.

The upstream one-shot profile creates an uncomposed agent.  The pinned patch
only joins that fresh agent to the shipped ``standard`` preset; the preset
itself, agent loop, tools, prompts, compaction, and persistence remain upstream.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from pathlib import Path, PurePosixPath
from typing import Any, Literal, override

from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.agents.installed.node_install import nvm_node_install_snippet
from harbor.agents.model_connection import ModelConnectionSpec, ResolvedModelConnection
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext


DSH_VERSION = "0.1.1-rc.2"
DSH_SOURCE_COMMIT = "b150a551b8d465e31e418e1b2eaf5e79bbb7d28e"
_PATCH = Path(__file__).with_name("patches") / "dsh-0.1.1-rc.2-headless-standard.patch"
DSH_PATCH_SHA256 = "b072cb7582dcb1c6baa948f4a292d748eed75f349e878842c50791908a801622"
_OPENROUTER_INJECTOR = Path(__file__).with_name("openrouter_body_injector.mjs")
OPENROUTER_INJECTOR_SHA256 = "617726fc07c07f3098e0222110c29b2ab4da3f3b361a5a04603e81dfa11fc49c"
_REMOTE_ROOT = PurePosixPath("/logs/agent/deepseek-harness")
_REMOTE_HOME = PurePosixPath("/tmp/harbor-dsh-home")
_REMOTE_SESSIONS = _REMOTE_ROOT / "sessions"
_REMOTE_PATCH = PurePosixPath("/installed-agent/dsh-headless-standard.patch")
_REMOTE_CONFIG = PurePosixPath("/installed-agent/dsh-run.patch.json")
_REMOTE_INJECTOR = PurePosixPath("/installed-agent/openrouter-body-injector.mjs")
_REMOTE_INJECTOR_CONFIG = PurePosixPath("/installed-agent/openrouter-route.json")
_REMOTE_API_KEY = PurePosixPath("/installed-agent/dsh-api-key")
_REMOTE_INJECTOR_LOG = _REMOTE_ROOT / "openrouter-injector.jsonl"
_OUTPUT_FILENAME = "dsh.log"
_PROTOCOLS = {"openai-completions", "openai-responses", "anthropic-messages"}


def _route_id(provider: str) -> str:
    cleaned = re.sub(r"[^a-z0-9-]+", "-", provider.lower()).strip("-")
    return f"harbor-{cleaned or 'provider'}"


def _api_key_env_name(access: ResolvedModelConnection) -> str:
    if access.api_key is None:
        raise ValueError("DeepSeek Harness requires an API key")
    for name, value in sorted(access.env.items()):
        if value == access.api_key:
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None:
                raise ValueError("invalid API-key environment variable name")
            return name
    raise ValueError("DeepSeek Harness requires an API-key environment reference")


def log_permissions_guard_shell() -> str:
    """Keep bind-mounted audit logs readable by Harbor's host-side scrubber."""
    root = shlex.quote(_REMOTE_ROOT.as_posix())
    return (
        f"(while :; do chmod -R a+rX {root} 2>/dev/null || true; sleep 1; done) & "
        'DSH_LOG_PERMS_PID="$!"; '
        "DSH_CLEANUP='DSH_CLEANUP_RC=$?; trap - EXIT HUP INT TERM; "
        "kill ${DSH_INJECTOR_PID:-} ${DSH_LOG_PERMS_PID:-} 2>/dev/null || true; "
        f"rm -f {shlex.quote(_REMOTE_API_KEY.as_posix())}; "
        f"chmod -R a+rX {root} 2>/dev/null || true; "
        "exit $DSH_CLEANUP_RC'; "
        "trap \"$DSH_CLEANUP\" EXIT HUP INT TERM; "
    )


def build_dsh_patch(
    *,
    adapter: Literal["pi-ai", "deepseek-official"],
    provider: str,
    model_id: str,
    api_key_env: str,
    base_url: str,
    model_api: str | None,
    context_window: int | None,
    max_tokens: int | None,
    input_modalities: list[Literal["text", "image"]] | None,
    reasoning_effort: Literal["high"] = "high",
    cache_retention: Literal["none", "short", "long"] | None = None,
    compat: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return the complete per-run overlay without embedding credentials."""
    if not model_id:
        raise ValueError("model id cannot be empty")
    if not base_url:
        raise ValueError("base URL cannot be empty")
    if reasoning_effort != "high":
        raise ValueError("the study-wide DSH reasoning effort is fixed to high")
    if cache_retention not in (None, "none", "short", "long"):
        raise ValueError("cache_retention must be none, short, long, or unset")

    rows: list[dict[str, Any]] = [
        {
            "insert": [
                {
                    "id": "agent-presets",
                    "name": "@deepseek-ai/dsh-agent-presets",
                    "config": {
                        "default": "standard",
                        "includeUserRoot": False,
                    },
                }
            ]
        }
    ]

    if adapter == "deepseek-official":
        if provider != "deepseek":
            raise ValueError("deepseek-official adapter requires provider=deepseek")
        if cache_retention is not None:
            raise ValueError("cache_retention is only supported by the pi-ai adapter")
        llm_config: dict[str, Any] = {
            "apiKeyEnv": api_key_env,
            "baseURL": base_url,
            "reasoningEffort": reasoning_effort,
        }
        if max_tokens is not None:
            llm_config["maxTokens"] = max_tokens
        rows.extend(
            [
                {"id": "llm-deepseek", "config": llm_config},
                {
                    "id": "agent-default-model",
                    "config": {"provider": "deepseek-official", "model": model_id},
                },
            ]
        )
    else:
        if model_api not in _PROTOCOLS:
            raise ValueError(
                "pi-ai adapter requires model_api to be one of "
                + ", ".join(sorted(_PROTOCOLS))
            )
        if context_window is None or max_tokens is None:
            raise ValueError(
                "pi-ai adapter requires explicit context_window and max_tokens"
            )
        route = _route_id(provider)
        model: dict[str, Any] = {
            "id": model_id,
            "contextWindow": context_window,
            "maxTokens": max_tokens,
            "reasoningEfforts": {"off": None, "high": "high"},
        }
        if input_modalities is not None:
            if not input_modalities or any(
                item not in ("text", "image") for item in input_modalities
            ):
                raise ValueError(
                    "pi-ai input_modalities must be a non-empty subset of text/image"
                )
            model["input"] = input_modalities
        if compat:
            model["compat"] = compat
        provider_config: dict[str, Any] = {
            "displayName": f"Harbor {provider}",
            "apiKeyEnv": api_key_env,
            "api": model_api,
            "baseURL": base_url,
            "reasoning": reasoning_effort,
            "models": [model],
        }
        if cache_retention is not None:
            provider_config["cacheRetention"] = cache_retention
        rows.extend(
            [
                {
                    "id": "llm-pi-ai",
                    "config": {
                        "providers": {
                            route: provider_config
                        }
                    },
                },
                {
                    "id": "agent-default-model",
                    "config": {"provider": route, "model": model_id},
                },
            ]
        )

    rows.append(
        {
            "id": "session-persistence-jsonl",
            "config": {
                "root": _REMOTE_SESSIONS.as_posix(),
                "compression": "none",
                "packChunks": False,
            },
        }
    )
    return rows


class DeepSeekHarness(BaseInstalledAgent):
    """Pinned DSH ``standard`` preset driven through its one-shot CLI."""

    MODEL_CONNECTION = ModelConnectionSpec(passthrough=True)

    def __init__(
        self,
        *args: Any,
        adapter: Literal["auto", "pi-ai", "deepseek-official"] = "auto",
        model_api: str | None = None,
        context_window: int | None = None,
        max_tokens: int | None = None,
        input_modalities: list[Literal["text", "image"]] | None = None,
        reasoning_effort: Literal["high"] = "high",
        cache_retention: Literal["none", "short", "long"] | None = None,
        compat: dict[str, Any] | None = None,
        openrouter_route: dict[str, Any] | None = None,
        permission_mode: Literal["danger-full-access"] = "danger-full-access",
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        if self._version not in (None, DSH_VERSION):
            raise ValueError(
                f"DeepSeek Harness is pinned to {DSH_VERSION}; got {self._version!r}"
            )
        self._version = DSH_VERSION
        self._adapter = adapter
        self._model_api = model_api
        self._context_window = context_window
        self._max_tokens = max_tokens
        self._input_modalities = input_modalities
        self._reasoning_effort = reasoning_effort
        self._cache_retention = cache_retention
        self._compat = compat
        self._openrouter_route = openrouter_route
        if openrouter_route is not None:
            only = openrouter_route.get("only")
            if not isinstance(only, list) or not only:
                raise ValueError("OpenRouter routing requires a non-empty provider.only")
            if openrouter_route.get("allow_fallbacks") is not False:
                raise ValueError("OpenRouter routing requires allow_fallbacks=false")
        if permission_mode != "danger-full-access":
            raise ValueError(
                "Harbor's ordinary Docker task containers cannot provide DSH rc.2 "
                "with a nested sandbox; permission_mode must be danger-full-access "
                "and Harbor remains the outer sandbox"
            )
        self._permission_mode = permission_mode

    @staticmethod
    @override
    def name() -> str:
        return "deepseek-harness"

    @override
    def get_version_command(self) -> str | None:
        return '. ~/.nvm/nvm.sh; dsh --version'

    @override
    def parse_version(self, stdout: str) -> str:
        return stdout.strip().splitlines()[-1].strip()

    async def _upload_file(
        self, environment: BaseEnvironment, source: Path, remote: PurePosixPath
    ) -> None:
        await environment.upload_file(source, remote.as_posix())
        if environment.default_user is not None:
            owner = shlex.quote(str(environment.default_user))
            target = shlex.quote(remote.as_posix())
            await self.exec_as_root(
                environment, command=f"chown {owner} {target} && chmod 600 {target}"
            )

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        actual_patch_sha256 = hashlib.sha256(_PATCH.read_bytes()).hexdigest()
        if actual_patch_sha256 != DSH_PATCH_SHA256:
            raise ValueError(
                "DeepSeek Harness compatibility patch does not match its frozen hash"
            )
        actual_injector_sha256 = hashlib.sha256(_OPENROUTER_INJECTOR.read_bytes()).hexdigest()
        if actual_injector_sha256 != OPENROUTER_INJECTOR_SHA256:
            raise ValueError("OpenRouter body injector does not match its frozen hash")
        await self.ensure_system_dependencies(environment, ("curl", "git"))
        await self._upload_file(environment, _PATCH, _REMOTE_PATCH)
        await self._upload_file(environment, _OPENROUTER_INJECTOR, _REMOTE_INJECTOR)
        package = shlex.quote(f"@deepseek-ai/dsh@{DSH_VERSION}")
        patch = shlex.quote(_REMOTE_PATCH.as_posix())
        await self.exec_as_agent(
            environment,
            command=(
                "set -euo pipefail; "
                f"{nvm_node_install_snippet()} && "
                f"npm install -g {package} && "
                'DSH_NPM_ROOT="$(npm root -g)" && '
                f"git apply --check --unsafe-paths --directory=\"$DSH_NPM_ROOT\" {patch} && "
                f"git apply --unsafe-paths --directory=\"$DSH_NPM_ROOT\" {patch} && "
                "dsh --version"
            ),
        )

    def _resolved_run(
        self,
    ) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, Any] | None]:
        if not self.model_name or "/" not in self.model_name:
            raise ValueError("model_name must be provider/model_id")
        declared_provider, model_id = self.model_name.split("/", 1)
        access = self.model_connection
        provider = access.provider or declared_provider
        base_url = access.base_url
        if base_url is None:
            raise ValueError(f"no base URL resolved for provider {provider!r}")
        injector_config: dict[str, Any] | None = None
        if self._openrouter_route is not None:
            if provider != "openrouter":
                raise ValueError("openrouter_route requires provider=openrouter")
            if self._adapter not in ("auto", "pi-ai"):
                raise ValueError("OpenRouter routing requires the pi-ai adapter")
            if self._model_api != "openai-completions":
                raise ValueError("OpenRouter routing requires Chat Completions")
            injector_config = {
                "model": model_id,
                "provider": self._openrouter_route,
            }
            base_url = "http://127.0.0.1:4010/v1"
        adapter = self._adapter
        if adapter == "auto":
            adapter = "deepseek-official" if provider == "deepseek" else "pi-ai"
        patch = build_dsh_patch(
            adapter=adapter,
            provider=provider,
            model_id=model_id,
            api_key_env=_api_key_env_name(access),
            base_url=base_url,
            model_api=self._model_api,
            context_window=self._context_window,
            max_tokens=self._max_tokens,
            input_modalities=self._input_modalities,
            reasoning_effort=self._reasoning_effort,
            cache_retention=self._cache_retention,
            compat=self._compat,
        )
        return patch, dict(access.env), injector_config

    @override
    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        patch, env, injector_config = self._resolved_run()
        api_key_env = _api_key_env_name(self.model_connection)
        api_key = env.get(api_key_env)
        if not isinstance(api_key, str) or not api_key or any(
            character in api_key for character in ("\x00", "\r", "\n")
        ):
            raise ValueError("invalid API key value")
        await self.exec_as_agent(
            environment,
            command=(
                f"mkdir -p {shlex.quote(_REMOTE_ROOT.as_posix())} "
                f"{shlex.quote(_REMOTE_SESSIONS.as_posix())} "
                f"{shlex.quote(_REMOTE_HOME.as_posix())} && "
                f"chmod 755 {shlex.quote(_REMOTE_ROOT.as_posix())} "
                f"{shlex.quote(_REMOTE_SESSIONS.as_posix())}"
            ),
        )
        await self._upload_config_text(
            environment,
            content=json.dumps(patch, indent=2) + "\n",
            remote_path=_REMOTE_CONFIG.as_posix(),
            filename=_REMOTE_CONFIG.name,
        )
        injector_setup = ""
        if injector_config is not None:
            await self._upload_config_text(
                environment,
                content=json.dumps(injector_config, indent=2) + "\n",
                remote_path=_REMOTE_INJECTOR_CONFIG.as_posix(),
                filename=_REMOTE_INJECTOR_CONFIG.name,
            )
            injector_setup = (
                f"node {shlex.quote(_REMOTE_INJECTOR.as_posix())} "
                f"--config {shlex.quote(_REMOTE_INJECTOR_CONFIG.as_posix())} "
                "--upstream https://openrouter.ai/api "
                f"--log {shlex.quote(_REMOTE_INJECTOR_LOG.as_posix())} --port 4010 & "
                'DSH_INJECTOR_PID="$!"; '
                "for DSH_WAIT in $(seq 1 100); do "
                "curl -fsS http://127.0.0.1:4010/health >/dev/null 2>&1 && break; "
                "sleep 0.1; done; "
                "curl -fsS http://127.0.0.1:4010/health >/dev/null; "
            )
        # Harbor's Docker exec transport renders ``env`` values as ``-e
        # NAME=value`` process arguments. Upload the credential as a private
        # ephemeral file instead, then export it inside the container shell so
        # it never appears in the host process command line or frozen config.
        # Upload it last to minimize its lifetime before the cleanup trap starts.
        await self._upload_config_text(
            environment,
            content=api_key + "\n",
            remote_path=_REMOTE_API_KEY.as_posix(),
            filename=_REMOTE_API_KEY.name,
        )
        log_permissions = log_permissions_guard_shell()
        command = (
            "set -o pipefail; . ~/.nvm/nvm.sh; "
            f"{log_permissions}"
            f"{api_key_env}=\"$(cat {shlex.quote(_REMOTE_API_KEY.as_posix())})\"; "
            f"export {api_key_env}; "
            f"{injector_setup}"
            "env -u DSH_TOOLS_MODE "
            f"DSH_HOME={shlex.quote(_REMOTE_HOME.as_posix())} "
            f"DSH_PERMISSION_MODE={shlex.quote(self._permission_mode)} "
            "DSH_TELEMETRY_DISABLED=1 "
            "dsh --profile headless "
            f"--patch {shlex.quote(_REMOTE_CONFIG.as_posix())} "
            f"{shlex.quote(instruction)} "
            f"2>&1 | stdbuf -oL tee {_REMOTE_ROOT / _OUTPUT_FILENAME}"
        )
        await self.exec_as_agent(environment, command=command)

    @override
    def populate_context_post_run(self, context: AgentContext) -> None:
        sessions = self.logs_dir / "deepseek-harness" / "sessions"
        if not sessions.exists():
            return
        input_tokens = 0
        output_tokens = 0
        cache_tokens = 0
        cache_write_tokens = 0
        reasoning_tokens = 0
        reasoning_usage_events = 0
        reasoning_messages = 0
        reasoning_blocks = 0
        compaction_events = 0
        compaction_starts = 0
        compaction_failures = 0
        finish_reasons: dict[str, int] = {}
        title_llm_requests = 0
        tool_calls = 0
        tool_results = 0
        tools_used: dict[str, int] = {}
        first_compaction: dict[str, Any] | None = None
        preset_headers: list[str | None] = []
        request_headers: list[dict[str, Any]] = []
        request_contexts: list[dict[str, Any]] = []
        tool_catalog_sizes: list[int] = []
        malformed = 0
        for path in sessions.rglob("session.jsonl"):
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    malformed += 1
                    continue
                if event.get("type") == "session":
                    preset_headers.append(event.get("agentPreset"))
                event_type = str(event.get("type", ""))
                if "compaction" in event_type:
                    compaction_events += 1
                if event_type == "compaction/start":
                    compaction_starts += 1
                    if first_compaction is None:
                        data = event.get("data") or {}
                        first_compaction = {
                            "time": event.get("time"),
                            "turn": data.get("turn"),
                        }
                if event_type == "compaction/end" and (event.get("data") or {}).get(
                    "error"
                ):
                    compaction_failures += 1
                if event_type == "request/header":
                    header = (event.get("data") or {}).get("header") or {}
                    request_headers.append(
                        {
                            "config": header.get("config") or {},
                            "adapterDefaults": header.get("adapterDefaults") or {},
                        }
                    )
                    tool_catalog_sizes.append(len(header.get("tools") or []))
                if event_type == "request/context":
                    request_contexts.append(event.get("data") or {})
                data = event.get("data") or {}
                if event_type == "session/title-llm-request":
                    title_llm_requests += 1
                if event_type == "tool/call":
                    tool_calls += 1
                    tool_name = str(
                        data.get("name") or data.get("toolName") or "unknown"
                    )
                    tools_used[tool_name] = tools_used.get(tool_name, 0) + 1
                if event_type == "tool/result":
                    tool_results += 1
                if event_type == "assistant/chunk":
                    chunk = data.get("chunk") or {}
                    if chunk.get("type") == "finish":
                        reason = str(
                            (chunk.get("reason") or {}).get("kind") or "unknown"
                        )
                        finish_reasons[reason] = finish_reasons.get(reason, 0) + 1
                if event_type != "assistant/message":
                    continue
                message = data.get("message") or {}
                message_reasoning_blocks = sum(
                    1
                    for block in message.get("content") or []
                    if isinstance(block, dict) and block.get("type") == "reasoning"
                )
                if message_reasoning_blocks:
                    reasoning_messages += 1
                    reasoning_blocks += message_reasoning_blocks
                usage = (
                    event.get("usage")
                    or data.get("usage")
                    or {}
                )
                input_tokens += int(usage.get("inputTokens") or 0)
                output_tokens += int(usage.get("outputTokens") or 0)
                cache_tokens += int(
                    usage.get("cacheReadTokens")
                    or usage.get("cachedInputTokens")
                    or 0
                )
                cache_write_tokens += int(usage.get("cacheWriteTokens") or 0)
                reasoning_tokens += int(usage.get("reasoningTokens") or 0)
                reasoning_usage_events += int("reasoningTokens" in usage)
        context.n_input_tokens = input_tokens
        context.n_output_tokens = output_tokens
        context.n_cache_tokens = cache_tokens
        context.metadata = {
            "dsh_usage_source": "native-session-jsonl-main-and-subagents",
            "dsh_cost_source": "provider-required; native title-call cost is absent",
            "dsh_reasoning_tokens": reasoning_tokens,
            "dsh_cache_read_tokens": cache_tokens,
            "dsh_cache_write_tokens": cache_write_tokens,
            "dsh_reasoning_token_counter_available": reasoning_usage_events > 0,
            "dsh_reasoning_message_count": reasoning_messages,
            "dsh_reasoning_block_count": reasoning_blocks,
            "dsh_compaction_event_records": compaction_events,
            "dsh_compaction_count": compaction_starts,
            "dsh_compaction_failures": compaction_failures,
            "dsh_first_compaction": first_compaction,
            "dsh_session_agent_presets": preset_headers,
            "dsh_request_headers": request_headers,
            "dsh_request_contexts": request_contexts,
            "dsh_tool_catalog_sizes": tool_catalog_sizes,
            "dsh_tool_calls": tool_calls,
            "dsh_tool_results": tool_results,
            "dsh_tools_used": tools_used,
            "dsh_finish_reasons": finish_reasons,
            "dsh_output_limit_finish_events": finish_reasons.get("max-tokens", 0),
            "dsh_title_llm_requests": title_llm_requests,
            "dsh_malformed_jsonl_lines": malformed,
            "dsh_profile": "headless-one-shot",
            "dsh_agent_preset": "standard",
            "dsh_permission_mode": self._permission_mode,
            "dsh_outer_sandbox": "harbor-task-container",
        }
