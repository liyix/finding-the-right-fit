#!/usr/bin/env python3
"""Run the pinned OpenHands SDK agent inside an ALE sandbox."""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from importlib.metadata import version
from pathlib import Path
from typing import Any

from openhands.sdk import Agent, AgentContext, Conversation, LLM, Tool
from openhands.sdk.llm import llm as llm_module
from openhands.tools.file_editor import FileEditorTool
from openhands.tools.task_tracker import TaskTrackerTool
from openhands.tools.terminal import TerminalTool


PATCH_ID = "openhands-1.44.1-openrouter-reasoning-roundtrip-v2"


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"expected JSON object: {path}")
    return value


def _safe_model_info(llm: LLM) -> dict[str, Any] | None:
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


def _dump(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _dump(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_dump(item) for item in value]
    return {"repr": repr(value)}


def _install_chat_compatibility() -> None:
    upstream = llm_module.select_chat_options

    def controlled(*args: Any, **kwargs: Any) -> dict[str, Any]:
        options = upstream(*args, **kwargs)
        llm = args[0] if args else kwargs.get("llm")
        if isinstance(llm, LLM) and (
            llm.model.startswith("openrouter/")
            or "openrouter.ai" in (llm.base_url or "")
        ):
            value = options.pop("max_completion_tokens", None)
            if value is not None:
                options["max_tokens"] = value
            allowed = list(options.get("allowed_openai_params") or [])
            if "reasoning_effort" not in allowed:
                allowed.append("reasoning_effort")
            options["allowed_openai_params"] = allowed
        return options

    llm_module.select_chat_options = controlled


def _token_metrics(llm: LLM) -> dict[str, Any]:
    metrics = llm.metrics
    usage = metrics.accumulated_token_usage
    return {
        "prompt_tokens": getattr(usage, "prompt_tokens", 0) if usage else 0,
        "completion_tokens": getattr(usage, "completion_tokens", 0) if usage else 0,
        "cache_read_tokens": getattr(usage, "cache_read_tokens", 0) if usage else 0,
        "cache_write_tokens": getattr(usage, "cache_write_tokens", 0) if usage else 0,
        "cost_usd": metrics.accumulated_cost,
        "native_metrics": _dump(metrics),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--patch-record", type=Path, required=True)
    args = parser.parse_args()

    cfg = _load_object(args.config)
    patch_record = _load_object(args.patch_record)
    if patch_record.get("patch_id") != PATCH_ID:
        raise RuntimeError("unexpected OpenHands reasoning patch identity")
    if patch_record.get("self_test", {}).get("status") != "passed":
        raise RuntimeError("OpenHands reasoning patch self-test did not pass")
    if version("openhands-sdk") != cfg["sdk_version"]:
        raise RuntimeError("OpenHands SDK version drift")
    if version("openhands-tools") != cfg["tools_version"]:
        raise RuntimeError("OpenHands tools version drift")

    api_key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OpenHands ALE runner requires OPENROUTER_API_KEY")

    args.work_dir.mkdir(parents=True, exist_ok=True)
    events_path = args.work_dir / "openhands-events.jsonl"
    metrics_path = args.work_dir / "openhands-metrics.json"
    resolved_path = args.work_dir / "resolved-openhands.json"
    completions = args.work_dir / "completions"
    events_path.write_text("", encoding="utf-8")
    completions.mkdir(parents=True, exist_ok=True)

    _install_chat_compatibility()
    llm_kwargs: dict[str, Any] = {
        "model": cfg["model"],
        "api_key": api_key,
        "base_url": cfg["base_url"],
        "reasoning_effort": cfg["reasoning_effort"],
        "max_input_tokens": cfg["max_input_tokens"],
        "max_output_tokens": cfg["max_output_tokens"],
        "api_mode": cfg["api_mode"],
        "capability_overrides": cfg["capability_overrides"],
        "litellm_extra_body": cfg["litellm_extra_body"],
        "log_completions": True,
        "log_completions_folder": str(completions),
    }
    if cfg["inline_image_urls"] is not None:
        llm_kwargs["inline_image_urls"] = cfg["inline_image_urls"]
    # temperature/top_p/seed are deliberately absent: provider defaults.
    llm = LLM(**llm_kwargs)
    tools = [
        Tool(name=TerminalTool.name),
        Tool(name=FileEditorTool.name),
        Tool(name=TaskTrackerTool.name),
    ]
    context = AgentContext(
        skills=[],
        load_user_skills=False,
        load_public_skills=False,
        load_project_skills=False,
        load_memory=False,
    )
    agent = Agent(
        llm=llm,
        tools=tools,
        agent_context=context,
        mcp_config=cfg["mcp_config"],
    )
    event_counts: Counter[str] = Counter()

    def save_event(event: Any) -> None:
        payload = event.model_dump(mode="json") if hasattr(event, "model_dump") else {
            "repr": repr(event)
        }
        event_counts[str(payload.get("kind") or type(event).__name__)] += 1
        with events_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
            stream.flush()

    conversation = Conversation(
        agent=agent,
        workspace=args.work_dir,
        callbacks=[save_event],
        max_iteration_per_run=cfg["max_iterations"],
        stuck_detection=True,
    )
    error: BaseException | None = None
    try:
        conversation.send_message(args.prompt_file.read_text(encoding="utf-8"))
        conversation.run()
    except BaseException as exc:
        error = exc
        raise
    finally:
        metrics = _token_metrics(llm)
        metrics_path.write_text(
            json.dumps(metrics, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        runtime = getattr(llm, "_runtime_metadata", None)
        resolved = {
            "versions": {
                "openhands-sdk": version("openhands-sdk"),
                "openhands-tools": version("openhands-tools"),
                "litellm": version("litellm"),
            },
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
            "seed": llm.seed,
            "num_retries": llm.num_retries,
            "retry_multiplier": llm.retry_multiplier,
            "retry_min_wait": llm.retry_min_wait,
            "retry_max_wait": llm.retry_max_wait,
            "timeout": llm.timeout,
            "max_message_chars": llm.max_message_chars,
            "caching_prompt": llm.caching_prompt,
            "enable_encrypted_reasoning": llm.enable_encrypted_reasoning,
            "native_tool_calling": llm.native_tool_calling,
            "disable_vision": llm.disable_vision,
            "inline_image_urls": llm.inline_image_urls,
            "capability_overrides": llm.capability_overrides,
            "litellm_extra_body": llm.litellm_extra_body,
            "model_info": _safe_model_info(llm),
            "runtime_metadata": _dump(runtime),
            "configured_tools": [
                TerminalTool.name,
                FileEditorTool.name,
                TaskTrackerTool.name,
                "finish",
                "think",
            ],
            "effective_tools": (
                sorted(agent.tools_map)
                if getattr(agent, "_initialized", False)
                else []
            ),
            "mcp_servers": sorted(cfg["mcp_config"]),
            "skills": [],
            "condenser": None,
            "max_iterations": cfg["max_iterations"],
            "stuck_detection": True,
            "event_counts": dict(event_counts),
            "execution_status": str(conversation.state.execution_status),
            "compatibility_patch": patch_record,
            "runner_error": None if error is None else f"{type(error).__name__}: {error}",
        }
        resolved_path.write_text(
            json.dumps(resolved, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        conversation.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
