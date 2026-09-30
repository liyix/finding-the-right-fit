"""Thin Harbor compatibility overlay for the pinned OpenHands SDK agent.

The upstream Harbor v0.22.0 adapter owns installation, prompting, tools, and the
agent loop.  This module only makes study-wide LLM controls explicit and
auditable.  It is loaded with Harbor's ``agent.import_path`` mechanism.
"""

import json
from pathlib import Path
from typing import Any, override

from harbor.agents.installed.openhands_sdk import OpenHandsSDK
from harbor.environments.base import BaseEnvironment


_UPSTREAM_RUNNER = "/installed-agent/run_agent_upstream.py"
_CONTROLLED_RUNNER = "/installed-agent/run_agent.py"
_REASONING_PATCHER = "/installed-agent/openhands_reasoning_details_patch.py"
_REASONING_PATCH_RECORD = "/installed-agent/openhands-reasoning-details-patch.json"

_RUNNER_WRAPPER = r'''#!/usr/bin/env python3
"""Apply frozen LLM controls, then execute Harbor's upstream OpenHands runner."""

import json
import os
import runpy
from importlib.metadata import version
from pathlib import Path
from typing import Any

import openhands.sdk as sdk
from openhands.sdk import LLM as UpstreamLLM
from openhands.sdk.conversation.impl.local_conversation import LocalConversation
from openhands.sdk.llm import llm as llm_module


LOGS_DIR = Path(os.environ.get("AGENT_LOGS_DIR", "/logs/agent"))
RESOLVED_PATH = LOGS_DIR / "resolved-llm-config.json"
EVENTS_PATH = LOGS_DIR / "openhands-events.json"
PATCH_RECORD_PATH = Path("/installed-agent/openhands-reasoning-details-patch.json")
PATCH_ID = "openhands-1.44.1-openrouter-reasoning-roundtrip-v2"


def _required(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise RuntimeError(f"required OpenHands control is missing: {name}")
    return value.strip()


def _positive_int(name: str) -> int:
    raw = _required(name)
    value = int(raw)
    if value <= 0:
        raise RuntimeError(f"{name} must be positive")
    return value


def _json_object(name: str, default: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return dict(default or {})
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError(f"{name} must contain a JSON object")
    return value


def _safe_model_info(llm: UpstreamLLM) -> dict[str, Any] | None:
    info = llm.model_info
    if not isinstance(info, dict):
        return None
    keys = (
        "key",
        "litellm_provider",
        "mode",
        "max_input_tokens",
        "max_output_tokens",
        "max_tokens",
        "supported_endpoints",
        "supports_reasoning",
        "supports_adaptive_thinking",
        "supports_vision",
        "supports_function_calling",
        "supports_prompt_caching",
    )
    return {key: info.get(key) for key in keys if key in info}


def _runtime_metadata(llm: UpstreamLLM) -> dict[str, Any] | None:
    metadata = getattr(llm, "_runtime_metadata", None)
    if metadata is None:
        return None
    if hasattr(metadata, "model_dump"):
        return metadata.model_dump(mode="json")
    return {"repr": repr(metadata)}


def _metrics(llm: UpstreamLLM) -> dict[str, Any]:
    metrics = llm.metrics
    if hasattr(metrics, "model_dump"):
        return metrics.model_dump(mode="json")
    return {"repr": repr(metrics)}


def _write_resolved(llm: UpstreamLLM, stage: str) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    current: dict[str, Any] = {}
    if RESOLVED_PATH.exists():
        try:
            current = json.loads(RESOLVED_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = {}

    if not PATCH_RECORD_PATH.is_file():
        raise RuntimeError("OpenHands reasoning-details patch record is missing")
    patch_record = json.loads(PATCH_RECORD_PATH.read_text(encoding="utf-8"))
    if patch_record.get("patch_id") != PATCH_ID:
        raise RuntimeError("unexpected OpenHands reasoning-details patch identity")
    if patch_record.get("status") not in {"applied", "already-applied"}:
        raise RuntimeError("OpenHands reasoning-details patch did not apply cleanly")
    if patch_record.get("self_test", {}).get("status") != "passed":
        raise RuntimeError("OpenHands reasoning round-trip self-test did not pass")

    features = llm._model_features()
    snapshot = {
        "stage": stage,
        "model": llm.model,
        "base_url": llm.base_url,
        "api_mode": llm.api_mode,
        "uses_responses_api": llm.uses_responses_api(),
        "reasoning_effort": llm.reasoning_effort,
        "max_input_tokens": llm.max_input_tokens,
        "effective_max_input_tokens": llm.effective_max_input_tokens,
        "max_output_tokens": llm.max_output_tokens,
        "effective_max_output_tokens": llm.effective_max_output_tokens,
        "temperature": llm.temperature,
        "top_p": llm.top_p,
        "top_k": llm.top_k,
        "seed": llm.seed,
        "num_retries": llm.num_retries,
        "retry_multiplier": llm.retry_multiplier,
        "retry_min_wait": llm.retry_min_wait,
        "retry_max_wait": llm.retry_max_wait,
        "timeout": llm.timeout,
        "max_message_chars": llm.max_message_chars,
        "caching_prompt": llm.caching_prompt,
        "prompt_cache_retention": llm.prompt_cache_retention,
        "enable_encrypted_reasoning": llm.enable_encrypted_reasoning,
        "native_tool_calling": llm.native_tool_calling,
        "disable_vision": llm.disable_vision,
        "inline_image_urls": llm.inline_image_urls,
        "capability_overrides": llm.capability_overrides,
        "litellm_extra_body": llm.litellm_extra_body,
        "features": features.__dict__,
        "model_info": _safe_model_info(llm),
        "runtime_metadata": _runtime_metadata(llm),
        "metrics": _metrics(llm),
    }
    current[stage] = snapshot
    current["versions"] = {
        "openhands-sdk": version("openhands-sdk"),
        "openhands-tools": version("openhands-tools"),
        "litellm": version("litellm"),
    }
    current["compatibility_patch"] = patch_record
    current["study_controls"] = {
        "skills": "disabled by Harbor agent kwargs",
        "mcp": "none unless explicitly present in Harbor config",
        "condenser": "none (upstream Harbor SDK runner default)",
        "max_iterations": int(os.environ.get("MAX_ITERATIONS", "500")),
        "context_window": _positive_int("OPENHANDS_CONTEXT_LIMIT"),
        "context_window_source": "study override",
        "max_output_tokens": _positive_int("OPENHANDS_OUTPUT_LIMIT"),
        "max_output_tokens_source": "study override",
        "stuck_detection": True,
    }
    RESOLVED_PATH.write_text(
        json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


class ControlledLLM(UpstreamLLM):
    """Inject only the pre-registered study controls into the upstream LLM."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        effort = _required("OPENHANDS_REASONING_EFFORT")
        if effort != "high":
            raise RuntimeError("this study requires OPENHANDS_REASONING_EFFORT=high")

        api_mode = _required("OPENHANDS_API_MODE")
        if api_mode not in {"auto", "chat", "responses"}:
            raise RuntimeError("OPENHANDS_API_MODE must be auto, chat, or responses")

        kwargs["reasoning_effort"] = effort
        kwargs["max_input_tokens"] = _positive_int("OPENHANDS_CONTEXT_LIMIT")
        # OpenHands 1.44.1 caps model metadata at 16,384 whenever a custom
        # base_url is present.  Every target endpoint supports at least 128K,
        # so make the approved study ceiling explicit instead of allowing an
        # asymmetric 16K-for-known / unset-for-unknown fallback.
        kwargs["max_output_tokens"] = _positive_int("OPENHANDS_OUTPUT_LIMIT")
        kwargs["api_mode"] = api_mode
        kwargs["capability_overrides"] = _json_object(
            "OPENHANDS_CAPABILITY_OVERRIDES"
        )

        inline_raw = os.environ.get("OPENHANDS_INLINE_IMAGE_URLS")
        if inline_raw is not None:
            normalized = inline_raw.strip().lower()
            if normalized not in {"0", "1", "false", "true"}:
                raise RuntimeError("OPENHANDS_INLINE_IMAGE_URLS must be true or false")
            kwargs["inline_image_urls"] = normalized in {"1", "true"}

        # Completion logs are audit evidence only; they do not alter requests.
        kwargs["log_completions"] = True
        kwargs["log_completions_folder"] = str(LOGS_DIR / "completions")
        super().__init__(*args, **kwargs)
        _write_resolved(self, "constructed")


_upstream_local_conversation_run = LocalConversation.run


def _controlled_local_conversation_run(
    self: LocalConversation, *args: Any, **kwargs: Any
) -> Any:
    """Preserve the complete native SDK event stream and final LLM metadata."""

    try:
        return _upstream_local_conversation_run(self, *args, **kwargs)
    finally:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        serialized = []
        for event in self.state.events:
            if hasattr(event, "model_dump"):
                serialized.append(event.model_dump(mode="json"))
            else:
                serialized.append({"repr": repr(event)})
        encoded_events = json.dumps(serialized, indent=2) + "\n"
        # Fail inside the runner if our native-event artifact is not valid JSON;
        # a successful process must never silently produce unauditable output.
        json.loads(encoded_events)
        EVENTS_PATH.write_text(encoded_events, encoding="utf-8")
        agent_llm = getattr(self.agent, "llm", None)
        if isinstance(agent_llm, UpstreamLLM):
            _write_resolved(agent_llm, "completed")


_upstream_select_chat_options = llm_module.select_chat_options


def _select_chat_options(*args: Any, **kwargs: Any) -> dict[str, Any]:
    options = _upstream_select_chat_options(*args, **kwargs)
    llm = args[0] if args else kwargs.get("llm")
    if isinstance(llm, UpstreamLLM) and (
        llm.model.startswith("openrouter/")
        or "openrouter.ai" in (llm.base_url or "")
    ):
        # The pinned OpenRouter Chat endpoints advertise max_tokens.  Keep the
        # value identical and change only the compatibility field name.
        value = options.pop("max_completion_tokens", None)
        if value is not None:
            options["max_tokens"] = value

        # LiteLLM's bundled model catalog can lag new OpenRouter models.  This
        # permits the already selected reasoning_effort field; it does not set
        # or change the value and is not forwarded as a provider body field.
        allowed = list(options.get("allowed_openai_params") or [])
        if "reasoning_effort" not in allowed:
            allowed.append("reasoning_effort")
        options["allowed_openai_params"] = allowed
    return options


sdk.LLM = ControlledLLM
LocalConversation.run = _controlled_local_conversation_run
llm_module.select_chat_options = _select_chat_options
runpy.run_path("/installed-agent/run_agent_upstream.py", run_name="__main__")
'''


class ControlledOpenHandsSDK(OpenHandsSDK):
    """Harbor's pinned OpenHands agent plus an LLM-control/audit wrapper."""

    def __init__(
        self,
        *args: Any,
        reasoning_effort: str = "high",
        max_input_tokens: int = 1_000_000,
        max_output_tokens: int | None = None,
        api_mode: str = "auto",
        capability_overrides: dict[str, Any] | None = None,
        inline_image_urls: bool | None = None,
        openrouter_provider: dict[str, Any] | None = None,
        load_skills: bool = False,
        max_iterations: int = 500,
        temperature: float | None = None,
        extra_env: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        if reasoning_effort != "high":
            raise ValueError("this study requires reasoning_effort='high'")
        if max_input_tokens != 1_000_000:
            raise ValueError("this study requires max_input_tokens=1000000")
        if max_output_tokens is None:
            raise ValueError(
                "max_output_tokens must be explicit; the registered OpenHands/"
                "OpenRouter compatibility value is 128000"
            )
        if max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        if api_mode != "auto":
            raise ValueError("this study preserves OpenHands api_mode='auto'")
        if load_skills:
            raise ValueError("project and user skills are disabled for this study")
        if max_iterations != 500:
            raise ValueError("this study preserves max_iterations=500")
        if temperature is not None:
            raise ValueError("temperature must remain unset (provider default)")

        controlled_env = {
            "OPENHANDS_REASONING_EFFORT": reasoning_effort,
            # Do not use environment names containing KEY/SECRET/TOKEN/etc.
            # Harbor treats those names as sensitive and redacts their literal
            # values from downloaded artifacts, which can corrupt JSON numbers.
            "OPENHANDS_CONTEXT_LIMIT": str(max_input_tokens),
            "OPENHANDS_OUTPUT_LIMIT": str(max_output_tokens),
            "OPENHANDS_API_MODE": api_mode,
            "OPENHANDS_CAPABILITY_OVERRIDES": json.dumps(
                capability_overrides or {}, sort_keys=True, separators=(",", ":")
            ),
        }
        if inline_image_urls is not None:
            controlled_env["OPENHANDS_INLINE_IMAGE_URLS"] = (
                "true" if inline_image_urls else "false"
            )
        if openrouter_provider is not None:
            controlled_env["LITELLM_EXTRA_BODY"] = json.dumps(
                {"provider": openrouter_provider},
                sort_keys=True,
                separators=(",", ":"),
            )

        merged_env = dict(extra_env or {})
        for key, value in controlled_env.items():
            existing = merged_env.get(key)
            if existing is not None and existing != value:
                raise ValueError(f"conflicting controlled environment value: {key}")
            merged_env[key] = value

        super().__init__(
            *args,
            reasoning_effort=reasoning_effort,
            load_skills=False,
            max_iterations=max_iterations,
            temperature=None,
            extra_env=merged_env,
            **kwargs,
        )

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        await super().install(environment)
        patcher_path = Path(__file__).with_name(
            "openhands_reasoning_details_patch.py"
        )
        await environment.upload_file(
            source_path=patcher_path,
            target_path=_REASONING_PATCHER,
        )
        await self.exec_as_root(
            environment,
            command=(
                f"/opt/openhands-sdk-venv/bin/python {_REASONING_PATCHER} "
                f"--record {_REASONING_PATCH_RECORD} --self-test"
            ),
        )
        await self.exec_as_root(
            environment,
            command=(
                f"mv {_CONTROLLED_RUNNER} {_UPSTREAM_RUNNER}"
            ),
        )
        await self._upload_config_text(
            environment,
            content=_RUNNER_WRAPPER,
            remote_path=_CONTROLLED_RUNNER,
            filename=Path(_CONTROLLED_RUNNER).name,
        )
