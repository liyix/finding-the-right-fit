#!/usr/bin/env python3
"""Pinned text-first openJiuwen Coding Agent used by the study adapter.

This is an explicit composition built from the public ``openjiuwen.harness``
API.  It is not presented as a vendor-default preset or as the unpublished
configuration used for the openJiuwen paper.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
import ssl
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from openjiuwen.core.context_engine import ContextEngineConfig
from openjiuwen.core.foundation.llm.model import Model
from openjiuwen.core.foundation.llm.schema.config import (
    LLMApiMode,
    ModelClientConfig,
    ModelRequestConfig,
    ProviderType,
)
from openjiuwen.core.foundation.llm.schema.message import AssistantMessage
from openjiuwen.core.foundation.tool.mcp.base import McpServerConfig
from openjiuwen.core.runner import Runner
from openjiuwen.core.session.agent import Session
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness import create_deep_agent
from openjiuwen.harness.prompts.prompt_attachment_manager import (
    PromptAttachmentKind,
)
from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.harness.rails.context_engineer import ContextProcessorRail
from openjiuwen.harness.rails.sys_operation_rail import SysOperationRail
from openjiuwen.harness.rails.task_completion_rail import (
    TaskCompletionRail,
    extract_promise_block,
    promise_matches,
)


OPENJIUWEN_VERSION = "0.1.18"
OPENJIUWEN_TAG_COMMIT = "1d37ae3007f9df9a8489a7ab271141b03be08f66"
REASONING_METADATA_KEY = "_openrouter_reasoning_details"
WIRE_RESPONSE_METADATA_KEY = "_study_wire_response"
CONTENT_CHAIN_KEY = "_study_stream_content_chain"
REASONING_CHAIN_KEY = "_study_stream_reasoning_chain"
DETAILS_CHAIN_KEY = "_study_stream_reasoning_details_chain"
COMPLETION_PROMISE = "task_complete"
DEFAULT_MAX_OUTER_ROUNDS = 8
RUNTIME_ATTACHMENT_SECTION = "runtime_budget"
DEFAULT_LLM_REQUEST_TIMEOUT_SECONDS = 360
DEFAULT_LLM_STREAM_FIRST_CHUNK_TIMEOUT_SECONDS = 300
DEFAULT_LLM_STREAM_IDLE_TIMEOUT_SECONDS = 300
DEFAULT_LLM_HTTP_MAX_RETRIES = 5
DEFAULT_RUNTIME_BUDGET_RAIL_ENABLED = False
DEFAULT_CONTEXT_COMPRESSION_ENABLED = True
OPENROUTER_UPSTREAM_IDLE_TIMEOUT_MARKER = "Upstream idle timeout exceeded"


def install_openrouter_upstream_idle_retry_patch() -> tuple[str, ...]:
    """Classify OpenRouter's in-stream idle error as a native stream timeout.

    ``openai.AsyncOpenAI.max_retries`` cannot replay a request after an HTTP
    200 response has already started streaming. openJiuwen's native
    ``ModelAnomalyDetectionRail`` owns safe whole-model-call replay, but
    v0.1.18 only recognizes its two locally generated timeout strings. Keep
    the native retry budget/backoff and extend only that classifier with the
    exact OpenRouter timeout marker observed on the wire.
    """
    from openjiuwen.harness.rails import model_anomaly_detection_rail as module

    raw_markers = getattr(module, "_STREAM_TIMEOUT_MARKERS", None)
    if not isinstance(raw_markers, tuple) or not all(
        isinstance(marker, str) for marker in raw_markers
    ):
        raise RuntimeError(
            "unsupported openJiuwen stream-timeout classifier shape"
        )
    markers = raw_markers
    if OPENROUTER_UPSTREAM_IDLE_TIMEOUT_MARKER not in markers:
        markers = markers + (OPENROUTER_UPSTREAM_IDLE_TIMEOUT_MARKER,)
        module._STREAM_TIMEOUT_MARKERS = markers
    if not module.ModelAnomalyDetectionRail._is_stream_timeout_exception(
        RuntimeError(OPENROUTER_UPSTREAM_IDLE_TIMEOUT_MARKER)
    ):
        raise RuntimeError(
            "OpenRouter upstream-idle timeout retry patch did not take effect"
        )
    return markers


class _FragmentChain:
    """Persistent O(1)-append chain used only while a response is streaming."""

    __slots__ = ("previous", "value", "length")

    def __init__(self, previous: "_FragmentChain | None", value: Any) -> None:
        self.previous = previous
        self.value = value
        self.length = (previous.length if previous is not None else 0) + 1


def _chain_append(
    chain: _FragmentChain | None, value: Any
) -> _FragmentChain | None:
    if value is None or value == "" or value == []:
        return chain
    return _FragmentChain(chain, value)


def _chain_values(chain: _FragmentChain | None) -> list[Any]:
    values: list[Any] = []
    while chain is not None:
        values.append(chain.value)
        chain = chain.previous
    values.reverse()
    return values


def _chain_from_message(
    message: Any, *, key: str, field: str
) -> _FragmentChain | None:
    metadata = getattr(message, "metadata", {})
    chain = metadata.get(key) if isinstance(metadata, dict) else None
    if isinstance(chain, _FragmentChain):
        return chain
    return _chain_append(None, getattr(message, field, None))


def _materialize_content(chain: _FragmentChain | None) -> Any:
    values = _chain_values(chain)
    if not values:
        return ""
    if all(isinstance(value, str) for value in values):
        return "".join(values)
    if all(isinstance(value, list) for value in values):
        combined_list: list[Any] = []
        for value in values:
            combined_list.extend(value)
        return combined_list
    combined = values[0]
    for value in values[1:]:
        if isinstance(combined, str) and isinstance(value, str):
            combined = combined + value
        elif isinstance(combined, list) and isinstance(value, list):
            combined = combined + value
        else:
            combined = value
    return combined


def _materialize_text(chain: _FragmentChain | None) -> str | None:
    values = _chain_values(chain)
    if not values:
        return None
    return "".join(str(value) for value in values if value is not None)


def _materialize_details(chain: _FragmentChain | None) -> list[Any]:
    details: list[Any] = []
    for value in _chain_values(chain):
        if isinstance(value, list):
            details.extend(value)
    return details


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def _reasoning_replay_mode(details: Any) -> str:
    """Choose one non-duplicated OpenRouter reasoning replay representation.

    OpenRouter permits plaintext reasoning to be replayed through
    ``reasoning_content``.  Signed, encrypted, summarized, or otherwise
    structured blocks must instead be replayed verbatim through
    ``reasoning_details``.  Never send both representations for one message.
    """
    if not isinstance(details, list) or not details:
        return "none"
    raw_text_keys = {"type", "text", "format", "index", "signature", "id"}
    for detail in details:
        if not isinstance(detail, dict):
            return "structured"
        if detail.get("type") != "reasoning.text":
            return "structured"
        if detail.get("signature") not in (None, ""):
            return "structured"
        if detail.get("id") not in (None, ""):
            return "structured"
        if detail.get("format") not in (None, "", "unknown"):
            return "structured"
        if any(
            key not in raw_text_keys and value not in (None, "", [], {})
            for key, value in detail.items()
        ):
            return "structured"
    return "plaintext"


def _plaintext_reasoning_from_details(details: list[Any]) -> str:
    return "".join(
        str(detail.get("text") or "")
        for detail in details
        if isinstance(detail, dict)
    )


def install_openrouter_reasoning_roundtrip_patch(
    *, wire_generation_log: Path | None = None
) -> None:
    """Preserve OpenRouter reasoning through tool-result replay once.

    openJiuwen 0.1.18 keeps visible reasoning text but drops the structured
    signed/encrypted blocks.  The patch stores the untouched JSON-compatible
    list in message metadata.  Plain reasoning is replayed only through
    ``reasoning_content``; special blocks are replayed only through the
    original ``reasoning_details`` sequence.
    """
    from openjiuwen.core.foundation.llm.model_clients.base_model_client import (
        BaseModelClient,
    )
    from openjiuwen.core.foundation.llm.model_clients.openai_model_client import (
        OpenAIModelClient,
    )
    from openjiuwen.core.foundation.llm.schema.message_chunk import (
        AssistantMessageChunk,
        _concat_token_ids,
        _merge_logprobs,
    )
    from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall

    if getattr(BaseModelClient, "_study_reasoning_patch", False):
        return

    original_convert = BaseModelClient._convert_messages_to_dict
    original_parse = OpenAIModelClient._parse_response
    original_parse_stream_chunk = OpenAIModelClient._parse_stream_chunk
    observed_wire_ids: set[str] = set()
    wire_log_lock = threading.Lock()

    def record_wire_generation(chunk: Any) -> None:
        """Persist an OpenRouter generation id before stream inspectors run."""
        if wire_generation_log is None:
            return
        response_id = str(getattr(chunk, "id", "") or "")
        if not response_id:
            return
        with wire_log_lock:
            if response_id in observed_wire_ids:
                return
            observed_wire_ids.add(response_id)
            record = {
                "monotonic_seconds": time.monotonic(),
                "response_id": response_id,
                "response_model": str(getattr(chunk, "model", "") or "") or None,
                "capture_stage": "raw-stream-chunk-before-openjiuwen-inspection",
            }
            wire_generation_log.parent.mkdir(parents=True, exist_ok=True)
            with wire_generation_log.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")

    def _reasoning_details(value: Any) -> list[Any]:
        details = _jsonable(getattr(value, "reasoning_details", None))
        return details if isinstance(details, list) else []

    def convert_messages(messages: Any) -> list[dict[str, Any]]:
        converted = original_convert(messages)
        if not isinstance(messages, list):
            return converted
        for source, target in zip(messages, converted):
            if not isinstance(target, dict):
                continue
            metadata = (
                source.get("metadata", {})
                if isinstance(source, dict)
                else getattr(source, "metadata", {})
            )
            if not isinstance(metadata, dict):
                continue
            details = metadata.get(REASONING_METADATA_KEY)
            if isinstance(details, list) and details:
                replay_mode = _reasoning_replay_mode(details)
                if replay_mode == "structured":
                    target["reasoning_details"] = copy.deepcopy(details)
                    target.pop("reasoning_content", None)
                elif replay_mode == "plaintext":
                    target.pop("reasoning_details", None)
                    if not target.get("reasoning_content"):
                        plaintext = _plaintext_reasoning_from_details(details)
                        if plaintext:
                            target["reasoning_content"] = plaintext
        return converted

    async def parse_response(
        self: Any, response: Any, parser: Any = None
    ) -> AssistantMessage:
        result = await original_parse(self, response, parser)
        choices = getattr(response, "choices", None)
        message = getattr(choices[0], "message", None) if choices else None
        details = _jsonable(getattr(message, "reasoning_details", None))
        if isinstance(details, list) and details:
            result.metadata = dict(result.metadata)
            result.metadata[REASONING_METADATA_KEY] = details
        return result

    def parse_stream_chunk(self: Any, chunk: Any) -> Any:
        # ModelAnomalyDetectionRail may interrupt a stream before the normal
        # after-model callback. Capture the provider id from the raw parsed SSE
        # object first so every started generation remains reconcilable.
        record_wire_generation(chunk)
        result = original_parse_stream_chunk(self, chunk)
        if result is None:
            return None
        choices = getattr(chunk, "choices", None)
        choice = choices[0] if choices else None
        details = _reasoning_details(getattr(choice, "delta", None))
        if not details:
            details = _reasoning_details(getattr(choice, "message", None))
        if details:
            result.metadata = dict(result.metadata)
            result.metadata[REASONING_METADATA_KEY] = copy.deepcopy(details)
        wire_response = {
            key: value
            for key, value in {
                "id": str(getattr(chunk, "id", "") or "") or None,
                "model": str(getattr(chunk, "model", "") or "") or None,
            }.items()
            if value is not None
        }
        if wire_response:
            result.metadata = dict(result.metadata)
            result.metadata[WIRE_RESPONSE_METADATA_KEY] = wire_response
        return result

    def add_stream_chunks(self: Any, other: Any) -> Any:
        if not isinstance(other, AssistantMessageChunk):
            raise TypeError(
                f"Cannot add AssistantMessageChunk to {type(other)}"
            )

        # v0.1.18 concatenates the full content/reasoning strings on every SSE
        # chunk. Adding reasoning_details the same way made long reasoning
        # O(n^2). Persistent chains keep both nested openJiuwen accumulators
        # immutable and O(1) per intermediate chunk, then materialize once at
        # finish/usage. This is a representation-only compatibility fix.
        content_chain = _chain_append(
            _chain_from_message(self, key=CONTENT_CHAIN_KEY, field="content"),
            other.content,
        )
        reasoning_chain = _chain_append(
            _chain_from_message(
                self, key=REASONING_CHAIN_KEY, field="reasoning_content"
            ),
            other.reasoning_content,
        )

        left_metadata = dict(self.metadata or {})
        right_metadata = dict(other.metadata or {})
        left_details_chain = left_metadata.get(DETAILS_CHAIN_KEY)
        if not isinstance(left_details_chain, _FragmentChain):
            left_details_chain = _chain_append(
                None, left_metadata.get(REASONING_METADATA_KEY)
            )
        details_chain = _chain_append(
            left_details_chain, right_metadata.get(REASONING_METADATA_KEY)
        )

        private_keys = {
            CONTENT_CHAIN_KEY,
            REASONING_CHAIN_KEY,
            DETAILS_CHAIN_KEY,
        }
        visible_left = {
            key: value for key, value in left_metadata.items()
            if key not in private_keys and key != REASONING_METADATA_KEY
        }
        visible_right = {
            key: value for key, value in right_metadata.items()
            if key not in private_keys and key != REASONING_METADATA_KEY
        }
        metadata = visible_right or visible_left

        merged_tool_calls: list[Any] = []
        for call in self.tool_calls or []:
            merged_tool_calls.append(
                ToolCall(
                    id=call.id,
                    type=call.type,
                    name=call.name,
                    arguments=call.arguments,
                    index=call.index,
                    response_item_id=call.response_item_id,
                )
            )
        for incoming in other.tool_calls or []:
            if merged_tool_calls:
                last = merged_tool_calls[-1]
                same_id = (
                    bool(last.id and incoming.id and last.id == incoming.id)
                    or not last.id
                    or not incoming.id
                )
                if (
                    same_id
                    and last.type == "function"
                    and incoming.type == "function"
                ):
                    merged_tool_calls[-1] = ToolCall(
                        id=last.id or incoming.id,
                        type=last.type or incoming.type,
                        name=(last.name if last.name else incoming.name) or "",
                        arguments=(last.arguments or "")
                        + (incoming.arguments or ""),
                        index=last.index,
                        response_item_id=(
                            last.response_item_id or incoming.response_item_id
                        ),
                    )
                    continue
            merged_tool_calls.append(
                ToolCall(
                    id=incoming.id,
                    type=incoming.type,
                    name=incoming.name,
                    arguments=incoming.arguments,
                    index=len(merged_tool_calls),
                    response_item_id=incoming.response_item_id,
                )
            )

        merged_finish_reason = (
            other.finish_reason
            if other.finish_reason != "null"
            else self.finish_reason
        )
        terminal = (
            merged_finish_reason != "null"
            or other.usage_metadata is not None
        )
        if terminal:
            combined_content = _materialize_content(content_chain)
            combined_reasoning = _materialize_text(reasoning_chain)
            combined_details = _materialize_details(details_chain)
            if combined_details:
                metadata[REASONING_METADATA_KEY] = combined_details
        else:
            combined_content = ""
            combined_reasoning = None
            if content_chain is not None:
                metadata[CONTENT_CHAIN_KEY] = content_chain
            if reasoning_chain is not None:
                metadata[REASONING_CHAIN_KEY] = reasoning_chain
            if details_chain is not None:
                metadata[DETAILS_CHAIN_KEY] = details_chain

        return AssistantMessageChunk(
            role=self.role,
            content=combined_content,
            metadata=metadata,
            tool_calls=merged_tool_calls or None,
            usage_metadata=other.usage_metadata or self.usage_metadata,
            finish_reason=merged_finish_reason,
            parser_content=(
                other.parser_content
                if other.parser_content is not None
                else self.parser_content
            ),
            reasoning_content=combined_reasoning,
            prompt_token_ids=self.prompt_token_ids or other.prompt_token_ids,
            completion_token_ids=_concat_token_ids(
                self.completion_token_ids, other.completion_token_ids
            ),
            logprobs=_merge_logprobs(self.logprobs, other.logprobs),
            response_id=other.response_id or self.response_id,
            response_model=other.response_model or self.response_model,
            provider_metadata={
                **self.provider_metadata,
                **other.provider_metadata,
            },
            provider_content=(
                other.provider_content
                if other.provider_content is not None
                else self.provider_content
            ),
        )

    BaseModelClient._convert_messages_to_dict = staticmethod(convert_messages)
    BaseModelClient._study_reasoning_patch = True
    OpenAIModelClient._parse_response = parse_response
    OpenAIModelClient._parse_stream_chunk = parse_stream_chunk
    AssistantMessageChunk.__add__ = add_stream_chunks


class ConfirmedCompletionRail(TaskCompletionRail):
    """Make the public completion evaluator actually drive follow-up rounds."""

    priority = 10

    def __init__(self, *, max_rounds: int, timeout_seconds: float) -> None:
        super().__init__(
            completion_promise=COMPLETION_PROMISE,
            required_confirmations=2,
            allow_promise_details=False,
            max_rounds=max_rounds,
            timeout_seconds=timeout_seconds,
        )

    async def after_task_iteration(self, ctx: AgentCallbackContext) -> None:
        result = getattr(ctx.inputs, "result", None)
        if isinstance(result, dict) and result.get("result_type") == "error":
            # A provider/tool exception is not an invitation to start another
            # outer task round. Native per-request retries have already fired.
            return
        content = self._extract_output(ctx) or ""
        block = extract_promise_block(content)
        matched = bool(
            block is not None and promise_matches(block, COMPLETION_PROMISE)
        )

        coordinator = getattr(ctx.agent, "loop_coordinator", None)
        evaluator = (
            coordinator.get_completion_promise_evaluator()
            if coordinator is not None
            else None
        )
        if matched:
            await super().after_task_iteration(ctx)
        elif evaluator is not None:
            evaluator.notify_absent()

        if evaluator is not None and evaluator.should_stop(None):
            return

        iteration = int(getattr(ctx.inputs, "iteration", 0) or 0)
        if iteration >= self.max_rounds:
            return
        controller = getattr(ctx.agent, "loop_controller", None)
        if controller is None:
            return
        if matched:
            follow_up = (
                "Before finalizing, re-check the task against every stated "
                "requirement and run appropriate tests or validation. Fix any "
                "issue found. When fully complete, end with "
                f"<promise>{COMPLETION_PROMISE}</promise>."
            )
        else:
            follow_up = (
                "Continue working on the original task. Check the current "
                "workspace state, finish every stated requirement, and run "
                "appropriate validation. Only when fully complete, end with "
                f"<promise>{COMPLETION_PROMISE}</promise>."
            )
        controller.enqueue_follow_up(follow_up)


class RuntimeBudgetRail(DeepAgentRail):
    """Expose an approximate remaining wall-clock budget before every call."""

    priority = 20

    def __init__(self, *, budget_seconds: float) -> None:
        super().__init__()
        self.budget_seconds = float(budget_seconds)
        self.started_at: float | None = None
        self.attachment_manager: Any = None

    def init(self, agent: Any) -> None:
        self.attachment_manager = getattr(
            agent, "prompt_attachment_manager", None
        )

    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        if self.started_at is None:
            self.started_at = time.monotonic()

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        if self.started_at is None:
            self.started_at = time.monotonic()
        elapsed = time.monotonic() - self.started_at
        remaining = max(0, int(self.budget_seconds - elapsed))
        # Minute buckets keep the changing suffix small while remaining useful.
        rounded = max(0, (remaining // 60) * 60)
        manager = self.attachment_manager or getattr(
            ctx.agent, "prompt_attachment_manager", None
        )
        if manager is None:
            raise RuntimeError("runtime budget prompt attachment manager is missing")
        writer = manager.bind_context(ctx)
        await writer.add_section(
            section=RUNTIME_ATTACHMENT_SECTION,
            content=(
                f"Runtime budget: approximately {rounded} seconds remain. "
                "Reserve enough time to validate the work and leave all "
                "requested artifacts in the workspace before the deadline."
            ),
            kind=PromptAttachmentKind.RUNTIME,
            source="study.openjiuwen.runtime_budget_rail",
            priority=95,
            metadata={"remaining_seconds_bucket": rounded},
        )


class AuditRail(DeepAgentRail):
    """Write behavior-neutral native lifecycle evidence as JSONL."""

    priority = 1

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path
        self.sequence = 0

    def _write(self, event: str, payload: dict[str, Any]) -> None:
        self.sequence += 1
        record = {
            "sequence": self.sequence,
            "monotonic_seconds": time.monotonic(),
            "event": event,
            **payload,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            # JSONL consumers split on Unicode line boundaries. Escaping
            # non-ASCII characters prevents tool output containing U+0085
            # (NEL) from being mistaken for a second physical record.
            handle.write(json.dumps(record, ensure_ascii=True, default=str) + "\n")

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        messages = list(getattr(ctx.inputs, "messages", None) or [])
        tools = list(getattr(ctx.inputs, "tools", None) or [])
        message_payload = _jsonable(messages)
        tool_payload = _jsonable(tools)
        replay_counts = []
        replay_modes = []
        for message in messages:
            metadata = getattr(message, "metadata", {})
            details = (
                metadata.get(REASONING_METADATA_KEY, [])
                if isinstance(metadata, dict)
                else []
            )
            replay_counts.append(len(details) if isinstance(details, list) else 0)
            replay_modes.append(_reasoning_replay_mode(details))
        self._write(
            "before_model_call",
            {
                "react_iteration": getattr(ctx.inputs, "react_iteration", None),
                "message_roles": [getattr(item, "role", None) for item in messages],
                "message_count": len(messages),
                "messages_sha256": hashlib.sha256(
                    json.dumps(message_payload, sort_keys=True, default=str).encode("utf-8")
                ).hexdigest(),
                "tool_count": len(tools),
                "tool_names": [getattr(item, "name", None) for item in tools],
                "tool_schema_sha256": hashlib.sha256(
                    json.dumps(tool_payload, sort_keys=True, default=str).encode("utf-8")
                ).hexdigest(),
                "reasoning_detail_counts_in_history": replay_counts,
                "reasoning_replay_modes_in_history": replay_modes,
            },
        )

    async def after_model_call(self, ctx: AgentCallbackContext) -> None:
        response = getattr(ctx.inputs, "response", None)
        usage = getattr(response, "usage_metadata", None)
        metadata = getattr(response, "metadata", {})
        details = (
            metadata.get(REASONING_METADATA_KEY, [])
            if isinstance(metadata, dict)
            else []
        )
        wire_response = (
            metadata.get(WIRE_RESPONSE_METADATA_KEY, {})
            if isinstance(metadata, dict)
            else {}
        )
        self._write(
            "after_model_call",
            {
                "react_iteration": getattr(ctx.inputs, "react_iteration", None),
                "finish_reason": getattr(response, "finish_reason", None),
                "response_id": getattr(response, "response_id", None),
                "response_model": getattr(response, "response_model", None),
                "wire_response_id": (
                    wire_response.get("id")
                    if isinstance(wire_response, dict)
                    else None
                ),
                "wire_response_model": (
                    wire_response.get("model")
                    if isinstance(wire_response, dict)
                    else None
                ),
                "tool_call_count": len(getattr(response, "tool_calls", None) or []),
                "reasoning_details_count": len(details),
                "reasoning_details_sha256": (
                    hashlib.sha256(
                        json.dumps(details, sort_keys=True).encode("utf-8")
                    ).hexdigest()
                    if details
                    else None
                ),
                "usage": _jsonable(usage) if usage is not None else None,
            },
        )

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        self._write(
            "before_tool_call",
            {
                "react_iteration": getattr(ctx.inputs, "react_iteration", None),
                "tool_name": getattr(ctx.inputs, "tool_name", None),
                "tool_args": _jsonable(getattr(ctx.inputs, "tool_args", None)),
            },
        )

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        self._write(
            "after_tool_call",
            {
                "react_iteration": getattr(ctx.inputs, "react_iteration", None),
                "tool_name": getattr(ctx.inputs, "tool_name", None),
                "tool_result": _jsonable(getattr(ctx.inputs, "tool_result", None)),
            },
        )

    async def on_model_exception(self, ctx: AgentCallbackContext) -> None:
        self._write(
            "model_exception",
            {
                "retry_attempt": getattr(ctx, "retry_attempt", None),
                "exception_type": type(ctx.exception).__name__,
                "exception": str(ctx.exception),
            },
        )

    async def on_tool_exception(self, ctx: AgentCallbackContext) -> None:
        self._write(
            "tool_exception",
            {
                "retry_attempt": getattr(ctx, "retry_attempt", None),
                "tool_name": getattr(ctx.inputs, "tool_name", None),
                "exception_type": type(ctx.exception).__name__,
                "exception": str(ctx.exception),
            },
        )

    async def after_task_iteration(self, ctx: AgentCallbackContext) -> None:
        self._write(
            "after_task_iteration",
            {
                "iteration": getattr(ctx.inputs, "iteration", None),
                "result": _jsonable(getattr(ctx.inputs, "result", None)),
            },
        )


def _validate_config(config: dict[str, Any]) -> None:
    required = {
        "model_id",
        "provider_only",
        "context_window",
        "reasoning_effort",
        "runtime_budget_seconds",
        "context_compression_enabled",
        "completion_timeout_seconds",
        "max_outer_rounds",
        "prompt_language",
    }
    missing = sorted(required - set(config))
    if missing:
        raise ValueError(f"missing openJiuwen config fields: {', '.join(missing)}")
    if config["reasoning_effort"] != "high":
        raise ValueError("study openJiuwen reasoning_effort must be high")
    if not isinstance(config["provider_only"], list) or not config["provider_only"]:
        raise ValueError("provider_only must be a non-empty list")
    if int(config["context_window"]) <= 0:
        raise ValueError("context_window must be positive")
    if int(config["max_outer_rounds"]) <= 1:
        raise ValueError("max_outer_rounds must allow the confirmation round")
    if float(config["completion_timeout_seconds"]) <= 0:
        raise ValueError("completion_timeout_seconds must be positive")
    if float(config.get(
        "llm_stream_first_chunk_timeout_seconds",
        DEFAULT_LLM_STREAM_FIRST_CHUNK_TIMEOUT_SECONDS,
    )) <= 0:
        raise ValueError("llm_stream_first_chunk_timeout_seconds must be positive")
    if float(config.get(
        "llm_stream_idle_timeout_seconds",
        DEFAULT_LLM_STREAM_IDLE_TIMEOUT_SECONDS,
    )) <= 0:
        raise ValueError("llm_stream_idle_timeout_seconds must be positive")
    if int(config.get(
        "llm_http_max_retries",
        DEFAULT_LLM_HTTP_MAX_RETRIES,
    )) < 0:
        raise ValueError("llm_http_max_retries must be non-negative")
    ssl_cert_path = config.get("llm_ssl_cert_path")
    if ssl_cert_path is not None:
        if not isinstance(ssl_cert_path, str) or not ssl_cert_path.strip():
            raise ValueError("llm_ssl_cert_path must be a non-empty string")
        certificate = Path(ssl_cert_path)
        if not certificate.is_absolute() or not certificate.is_file():
            raise ValueError(
                "llm_ssl_cert_path must name a readable absolute certificate file"
            )
    if not isinstance(
        config.get(
            "runtime_budget_rail_enabled",
            DEFAULT_RUNTIME_BUDGET_RAIL_ENABLED,
        ),
        bool,
    ):
        raise ValueError("runtime_budget_rail_enabled must be boolean")
    if config["context_compression_enabled"] is not True:
        raise ValueError(
            "the registered openJiuwen path requires its native context compression preset"
        )
    if config["prompt_language"] != "en":
        raise ValueError("the English benchmark configuration requires prompt_language=en")
    mcp_servers = config.get("mcp_servers", [])
    if not isinstance(mcp_servers, list):
        raise ValueError("mcp_servers must be a list")
    for item in mcp_servers:
        if not isinstance(item, dict):
            raise ValueError("each mcp_servers entry must be an object")
        if item.get("server_name") != "cua":
            raise ValueError("the ALE integration permits only the benchmark CUA MCP")
        if item.get("client_type") != "stdio":
            raise ValueError("the ALE CUA MCP must use stdio")
        if not isinstance(item.get("command"), str) or not item["command"]:
            raise ValueError("the ALE CUA MCP requires a command")
        if not isinstance(item.get("args"), list) or not item["args"]:
            raise ValueError("the ALE CUA MCP requires args")
        env = item.get("env")
        if not isinstance(env, dict) or set(env) != {"CUA_SERVER_URL"}:
            raise ValueError("the ALE CUA MCP requires only CUA_SERVER_URL")


def install_scoped_safe_cert_dir(ssl_cert_path: str) -> None:
    """Apply upstream CA confinement without leaking it to tool subprocesses."""
    from openjiuwen.core.common.security.ssl_utils import SslUtils

    certificate = Path(ssl_cert_path).resolve(strict=True)
    current = SslUtils.create_strict_ssl_context
    installed_for = getattr(current, "_study_scoped_cert_path", None)
    if installed_for is not None:
        if installed_for != str(certificate):
            raise RuntimeError("conflicting scoped SSL certificate paths")
        return

    def scoped_create_strict_ssl_context(
        ssl_cert: str | None = None,
    ) -> ssl.SSLContext:
        if ssl_cert is None or Path(ssl_cert).resolve() != certificate:
            return current(ssl_cert)
        previous = os.environ.get("SAFE_CERT_DIR")
        os.environ["SAFE_CERT_DIR"] = str(certificate.parent)
        try:
            return current(str(certificate))
        finally:
            if previous is None:
                os.environ.pop("SAFE_CERT_DIR", None)
            else:
                os.environ["SAFE_CERT_DIR"] = previous

    setattr(
        scoped_create_strict_ssl_context,
        "_study_scoped_cert_path",
        str(certificate),
    )
    SslUtils.create_strict_ssl_context = staticmethod(
        scoped_create_strict_ssl_context
    )


def _mcp_configs(config: dict[str, Any]) -> list[McpServerConfig] | None:
    """Translate the optional ALE-owned CUA endpoint into native MCP config."""
    result: list[McpServerConfig] = []
    for item in config.get("mcp_servers", []):
        params: dict[str, Any] = {
            "command": item["command"],
            "args": list(item["args"]),
            "env": {str(key): str(value) for key, value in item["env"].items()},
        }
        if item.get("cwd"):
            params["cwd"] = str(item["cwd"])
        result.append(
            McpServerConfig(
                server_name=str(item["server_name"]),
                server_path=str(item["command"]),
                client_type="stdio",
                params=params,
                include_image_content=bool(item.get("include_image_content", False)),
            )
        )
    return result or None


def build_agent(
    config: dict[str, Any], *, workspace: Path, log_dir: Path,
    api_key: str | None = None,
) -> Any:
    _validate_config(config)
    if os.getenv("USE_RL_ONLINE_RAIL", "").strip().lower() in {
        "1", "true", "yes", "on",
    }:
        raise RuntimeError("online training Rail must be disabled for benchmark trials")
    install_openrouter_reasoning_roundtrip_patch(
        wire_generation_log=log_dir / "wire-generations.jsonl"
    )
    install_openrouter_upstream_idle_retry_patch()

    route: dict[str, Any] = {
        "only": list(config["provider_only"]),
        "allow_fallbacks": False,
        "require_parameters": True,
    }
    quantizations = config.get("quantizations")
    if quantizations:
        route["quantizations"] = list(quantizations)

    api_key = (api_key or os.environ.get("OPENROUTER_API_KEY", "")).strip()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is required")

    ssl_cert_path = config.get("llm_ssl_cert_path")
    if ssl_cert_path:
        install_scoped_safe_cert_dir(str(ssl_cert_path))

    client_config = ModelClientConfig(
        client_provider=ProviderType.OpenRouter,
        api_key=api_key,
        api_base=str(config.get("base_url", "https://openrouter.ai/api/v1")),
        api_mode=LLMApiMode.ChatCompletions,
        # The task image's OS trust store is not uniform. Keep certificate
        # verification strict while using the CA bundle frozen in the mounted
        # openJiuwen runtime instead of changing task-process SSL variables.
        verify_ssl=True,
        ssl_cert=ssl_cert_path,
        timeout=float(config.get(
            "llm_request_timeout_seconds",
            DEFAULT_LLM_REQUEST_TIMEOUT_SECONDS,
        )),
        stream_first_chunk_timeout=float(config.get(
            "llm_stream_first_chunk_timeout_seconds",
            DEFAULT_LLM_STREAM_FIRST_CHUNK_TIMEOUT_SECONDS,
        )),
        stream_idle_timeout=float(config.get(
            "llm_stream_idle_timeout_seconds",
            DEFAULT_LLM_STREAM_IDLE_TIMEOUT_SECONDS,
        )),
        max_retries=int(config.get(
            "llm_http_max_retries",
            DEFAULT_LLM_HTTP_MAX_RETRIES,
        )),
    )
    request_config = ModelRequestConfig(
        model=str(config["model_id"]),
        temperature=None,
        top_p=None,
        max_tokens=None,
        context_window=int(config["context_window"]),
        # The v0.1.18 reasoning profile table does not yet cover every study
        # model. Send OpenRouter's provider-neutral control directly so all
        # five arms have the same wire field instead of model-name fallbacks.
        reasoning=None,
        extra_body={
            "provider": route,
            "reasoning": {"effort": "high"},
        },
    )
    model = Model(client_config, request_config)
    rails: list[DeepAgentRail] = [
        SysOperationRail(
            with_code_tool=False,
            read_only=False,
            enable_read_image_multimodal=False,
        ),
        ContextProcessorRail(preset=True),
    ]
    if bool(config.get(
        "runtime_budget_rail_enabled",
        DEFAULT_RUNTIME_BUDGET_RAIL_ENABLED,
    )):
        rails.append(
            RuntimeBudgetRail(
                budget_seconds=float(config["runtime_budget_seconds"])
            )
        )
    rails.extend([
        ConfirmedCompletionRail(
            max_rounds=int(config["max_outer_rounds"]),
            timeout_seconds=float(config["completion_timeout_seconds"]),
        ),
        AuditRail(log_dir / "native-events.jsonl"),
    ])
    project_root = Path(str(config.get("project_root", workspace))).resolve()
    return create_deep_agent(
        model=model,
        rails=rails,
        enable_task_loop=True,
        workspace=str(workspace),
        cwd=str(workspace),
        project_root=str(project_root),
        skills=None,
        mcps=_mcp_configs(config),
        subagents=None,
        enable_task_planning=False,
        add_general_purpose_agent=False,
        enable_async_subagent=False,
        enable_subagent_runtime=False,
        enable_read_image_multimodal=False,
        restrict_to_work_dir=True,
        parallel_tool_calls=True,
        language=str(config["prompt_language"]),
        completion_timeout=float(config["completion_timeout_seconds"]),
        context_engine_config=ContextEngineConfig(
            context_window_tokens=int(config["context_window"]),
            model_name=str(config["model_id"]),
            model_provider="openrouter",
            enable_context_debug=True,
            context_debug_dir=str(log_dir / "context-debug"),
        ),
        # The product default scaffolds AGENT/SOUL/IDENTITY/memory/skills files
        # inside the workspace. Benchmark task repositories must stay clean;
        # SysOperationRail remains enabled against this same workspace.
        auto_create_workspace=False,
        # Keep the v0.1.18 native defaults enabled.
        enable_security_rail=True,
        enable_model_anomaly_detection_rail=True,
    )


async def run_agent(
    config: dict[str, Any], instruction: str, *, workspace: Path, log_dir: Path,
    api_key: str | None = None,
) -> dict[str, Any]:
    agent = build_agent(
        config, workspace=workspace, log_dir=log_dir, api_key=api_key
    )
    # The client has copied the credential into its private configuration.
    # Remove any legacy environment copy before a harness tool can spawn a
    # task subprocess; formal adapters pass the key only through stdin.
    inherited_api_key = os.environ.pop("OPENROUTER_API_KEY", None)
    session_id = f"study-openjiuwen-{uuid.uuid4().hex}"
    # Passing a pre-built Session lets us preserve the exact native context for
    # audit. openJiuwen 0.1.18 requires its owning AgentCard at pre_run time.
    session = Session(session_id=session_id, card=agent.card)
    await Runner.start()
    try:
        result = await Runner.run_agent(
            agent, {"query": instruction}, session=session
        )
        context = agent._react_agent.context_engine.get_context(  # noqa: SLF001
            session_id=session_id
        )
        if context is None:
            raise RuntimeError("openJiuwen native context is missing after invoke")
        messages = context.get_messages(with_history=True)
        transcript = [
            _jsonable(message)
            for message in messages
        ]
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "native-transcript.json").write_text(
            json.dumps(transcript, indent=2, ensure_ascii=False, default=str) + "\n",
            encoding="utf-8",
        )
        (log_dir / "result.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False, default=str) + "\n",
            encoding="utf-8",
        )
        recorder = getattr(context, "_processor_state_recorder", None)
        history = recorder.history() if recorder is not None else []
        (log_dir / "context-compression-events.json").write_text(
            json.dumps(history, indent=2, ensure_ascii=False, default=str) + "\n",
            encoding="utf-8",
        )
        processor_policy = []
        react_config = getattr(agent._react_agent, "config", None)  # noqa: SLF001
        for name, processor_config in list(
            getattr(react_config, "context_processors", None) or []
        ):
            if hasattr(processor_config, "model_dump"):
                effective = processor_config.model_dump(
                    mode="json", exclude={"model", "model_client"}
                )
            else:
                effective = _jsonable(processor_config)
            processor_policy.append({"name": name, "config": effective})
        (log_dir / "context-compression-policy.json").write_text(
            json.dumps(
                {
                    "enabled": True,
                    "context_window_tokens": int(config["context_window"]),
                    "processors": processor_policy,
                },
                indent=2,
                ensure_ascii=False,
                default=str,
            ) + "\n",
            encoding="utf-8",
        )
        return result
    finally:
        await Runner.stop()
        if inherited_api_key is not None:
            os.environ["OPENROUTER_API_KEY"] = inherited_api_key


class _FakeOpenRouterHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []
    expected_reasoning = [
        {
            "type": "reasoning.text",
            "text": "I should create the requested file.",
            "signature": "study-signature",
            "id": "reasoning-1",
            "format": "anthropic-claude-v1",
            "index": 0,
        }
    ]

    def log_message(self, *_args: Any) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
        length = int(self.headers.get("content-length", "0"))
        payload = json.loads(self.rfile.read(length))
        type(self).requests.append(payload)
        call = len(type(self).requests)
        if call >= 2:
            assistant_rows = [
                item for item in payload.get("messages", [])
                if item.get("role") == "assistant" and item.get("tool_calls")
            ]
            if not assistant_rows or assistant_rows[-1].get(
                "reasoning_details"
            ) != type(self).expected_reasoning or assistant_rows[-1].get(
                "reasoning_content"
            ):
                self.send_response(400)
                self.end_headers()
                self.wfile.write(
                    b'{"error":{"message":"structured reasoning replay invalid"}}'
                )
                return

        if call == 1:
            delta = {
                "role": "assistant",
                "content": "",
                "reasoning": "I should create the requested file.",
                "reasoning_details": type(self).expected_reasoning,
                "tool_calls": [
                    {
                        "id": "call_write_1",
                        "type": "function",
                        "function": {
                            "name": "bash",
                            "arguments": json.dumps(
                                {
                                    "command": (
                                        "test -z \"$OPENROUTER_API_KEY\" && "
                                        "printf 'ok\\n' > proof.txt"
                                    ),
                                    "description": (
                                        "Verify tool subprocess credential isolation"
                                    ),
                                }
                            ),
                        },
                    }
                ],
            }
            finish_reason = "tool_calls"
        else:
            delta = {
                "role": "assistant",
                "content": f"Checked. <promise>{COMPLETION_PROMISE}</promise>",
            }
            finish_reason = "stop"
        chunk = {
            "id": f"gen-test-{call}",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": payload.get("model"),
            "choices": [
                {"index": 0, "delta": delta, "finish_reason": finish_reason}
            ],
        }
        usage_chunk = {
            "id": f"gen-test-{call}",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": payload.get("model"),
            "choices": [],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
        }
        encoded = (
            f"data: {json.dumps(chunk)}\n\n"
            f"data: {json.dumps(usage_chunk)}\n\n"
            "data: [DONE]\n\n"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def _stream_accumulation_self_test(chunk_count: int = 4096) -> dict[str, Any]:
    """Exercise stream accumulation and plaintext replay without a paid request."""
    from openjiuwen.core.foundation.llm.model_clients.base_model_client import (
        BaseModelClient,
    )
    from openjiuwen.core.foundation.llm.schema.message_chunk import (
        AssistantMessageChunk,
    )
    from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall

    started_at = time.perf_counter()
    inner: AssistantMessageChunk | None = None
    outer: AssistantMessageChunk | None = None
    expected_details: list[dict[str, Any]] = []
    for index in range(chunk_count):
        detail = {
            "type": "reasoning.text",
            "text": f"r{index}",
            "format": "unknown",
            "index": index,
        }
        expected_details.append(detail)
        parsed = AssistantMessageChunk(
            content="c",
            reasoning_content="r",
            metadata={REASONING_METADATA_KEY: [detail]},
            finish_reason="null",
        )
        inner = inner + parsed if inner is not None else parsed
        # Model.stream independently accumulates the original parsed chunks
        # yielded by OpenAIModelClient.stream.
        outer = outer + parsed if outer is not None else parsed

    terminal = AssistantMessageChunk(content="", finish_reason="stop")
    inner = inner + terminal if inner is not None else terminal
    outer = outer + terminal if outer is not None else terminal
    elapsed = time.perf_counter() - started_at

    for accumulated in (inner, outer):
        assert accumulated.content == "c" * chunk_count
        assert accumulated.reasoning_content == "r" * chunk_count
        assert accumulated.metadata.get(REASONING_METADATA_KEY) == expected_details
        assert not ({CONTENT_CHAIN_KEY, REASONING_CHAIN_KEY, DETAILS_CHAIN_KEY}
                    & set(accumulated.metadata))
    replay_message = AssistantMessage(
        content="",
        reasoning_content=inner.reasoning_content,
        metadata=inner.metadata,
        tool_calls=[
            ToolCall(
                id="call-plaintext",
                type="function",
                name="bash",
                arguments='{"command":"true"}',
            )
        ],
    )
    replay = BaseModelClient._convert_messages_to_dict([replay_message])[0]
    assert replay.get("reasoning_content") == "r" * chunk_count
    assert "reasoning_details" not in replay
    # A deliberately loose guard catches the prior quadratic regression while
    # avoiding sensitivity to shared-host scheduling.
    assert elapsed < 10.0, f"stream accumulation took {elapsed:.3f}s"
    return {
        "status": "passed",
        "chunk_count": chunk_count,
        "elapsed_seconds": round(elapsed, 6),
        "reasoning_details_count": len(expected_details),
        "plaintext_replay_field": "reasoning_content",
        "plaintext_details_omitted": True,
    }


async def _upstream_idle_retry_self_test() -> dict[str, Any]:
    """Prove the exact provider error uses openJiuwen's native retry path."""
    from openjiuwen.harness.rails.model_anomaly_detection_rail import (
        ModelAnomalyDetectionRail,
    )

    markers = install_openrouter_upstream_idle_retry_patch()

    class RetryProbe:
        def __init__(self, exception: BaseException) -> None:
            self.exception = exception
            self.delays: list[float] = []

        def request_retry(self, *, delay_seconds: float) -> None:
            self.delays.append(delay_seconds)

    rail = ModelAnomalyDetectionRail(
        max_retries=2,
        backoff_seconds=[0.5, 1.0],
    )
    probe = RetryProbe(RuntimeError(
        "[181001] model call failed, reason: openAI API async stream error: "
        f"APIError: {OPENROUTER_UPSTREAM_IDLE_TIMEOUT_MARKER}"
    ))
    await rail.on_model_exception(probe)
    await rail.on_model_exception(probe)
    await rail.on_model_exception(probe)
    assert probe.delays == [0.5, 1.0]
    assert rail.stream_timeout_retry_count == 0

    unrelated = RetryProbe(RuntimeError("non-retryable model error"))
    await rail.on_model_exception(unrelated)
    assert unrelated.delays == []
    return {
        "status": "passed",
        "marker": OPENROUTER_UPSTREAM_IDLE_TIMEOUT_MARKER,
        "classifier_markers": list(markers),
        "native_retry_count": len(probe.delays),
        "native_backoff_seconds": probe.delays,
        "unrelated_error_retried": False,
    }


async def self_test(output_path: Path | None = None) -> None:
    _FakeOpenRouterHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOpenRouterHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    old_key = os.environ.get("OPENROUTER_API_KEY")
    old_safe_cert_dir = os.environ.pop("SAFE_CERT_DIR", None)
    os.environ["OPENROUTER_API_KEY"] = "self-test-not-a-secret"
    try:
        import certifi
        from openjiuwen.core.common.security.ssl_utils import SslUtils

        self_test_ca = str(Path(certifi.where()).resolve())
        install_scoped_safe_cert_dir(self_test_ca)
        strict_context = SslUtils.create_strict_ssl_context(self_test_ca)
        assert isinstance(strict_context, ssl.SSLContext)
        assert "SAFE_CERT_DIR" not in os.environ
        with tempfile.TemporaryDirectory(prefix="openjiuwen-study-") as temp:
            root = Path(temp)
            config = {
                "model_id": "anthropic/test-model",
                "provider_only": ["anthropic"],
                "quantizations": None,
                "context_window": 1_000_000,
                "reasoning_effort": "high",
                "runtime_budget_seconds": 600,
                "runtime_budget_rail_enabled": False,
                "context_compression_enabled": True,
                "completion_timeout_seconds": 570,
                "max_outer_rounds": 8,
                "prompt_language": "en",
                "llm_request_timeout_seconds": 30,
                "llm_stream_first_chunk_timeout_seconds": 30,
                "llm_stream_idle_timeout_seconds": 30,
                "llm_http_max_retries": 0,
                "base_url": f"http://127.0.0.1:{server.server_port}/v1",
            }
            result = await run_agent(
                config,
                "Create proof.txt containing exactly ok followed by a newline.",
                workspace=root,
                log_dir=root / "logs",
            )
            assert (root / "proof.txt").read_text(encoding="utf-8") == "ok\n"
            scaffold_names = {
                ".workspace",
                "AGENT.md",
                "SOUL.md",
                "HEARTBEAT.md",
                "IDENTITY.md",
                "USER.md",
                "memory",
                "todo",
                "messages",
                "skills",
                "agents",
                "context",
            }
            assert not (scaffold_names & {item.name for item in root.iterdir()})
            assert len(_FakeOpenRouterHandler.requests) == 3
            for payload in _FakeOpenRouterHandler.requests:
                assert payload["provider"] == {
                    "only": ["anthropic"],
                    "allow_fallbacks": False,
                    "require_parameters": True,
                }
                assert payload["reasoning"] == {"effort": "high"}
                assert "temperature" not in payload
                assert "top_p" not in payload
                assert "max_tokens" not in payload
                assert "seed" not in payload
            first = _FakeOpenRouterHandler.requests[0]
            assert {item["function"]["name"] for item in first["tools"]} == {
                "read_file",
                "write_file",
                "edit_file",
                "glob",
                "list_files",
                "grep",
                "bash",
            }
            assert not any(
                "Runtime budget:" in str(item.get("content", ""))
                for payload in _FakeOpenRouterHandler.requests
                for item in payload["messages"]
            )
            assert result.get("result_type") == "answer"
            compression_policy = json.loads(
                (root / "logs" / "context-compression-policy.json").read_text(
                    encoding="utf-8"
                )
            )
            processors = {
                item["name"]: item["config"]
                for item in compression_policy["processors"]
            }
            assert compression_policy["context_window_tokens"] == 1_000_000
            assert processors["MessageSummaryOffloader"][
                "add_message_threshold_ratio"
            ] == 0.1
            assert processors["SessionMemoryCompressor"]["enabled"] is False
            for processor_name in (
                "DialogueCompressor",
                "CurrentRoundCompressor",
                "RoundLevelCompressor",
            ):
                assert processors[processor_name]["trigger_context_ratio"] == 0.8
            assert json.loads(
                (root / "logs" / "context-compression-events.json").read_text(
                    encoding="utf-8"
                )
            ) == []
            with (root / "logs" / "native-events.jsonl").open(
                "r", encoding="utf-8"
            ) as handle:
                audit_events = [
                    json.loads(line) for line in handle if line.strip()
                ]
            wire_response_ids = [
                item.get("wire_response_id")
                for item in audit_events
                if item.get("event") == "after_model_call"
            ]
            assert wire_response_ids == [
                "gen-test-1",
                "gen-test-2",
                "gen-test-3",
            ]
            early_wire_rows = [
                json.loads(line)
                for line in (root / "logs" / "wire-generations.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            assert [item.get("response_id") for item in early_wire_rows] == [
                "gen-test-1",
                "gen-test-2",
                "gen-test-3",
            ]
            assert all(
                item.get("capture_stage")
                == "raw-stream-chunk-before-openjiuwen-inspection"
                for item in early_wire_rows
            )
            audit_jsonl_path = root / "logs" / "audit-jsonl-self-test.jsonl"
            AuditRail(audit_jsonl_path)._write(
                "unicode_line_boundary_test", {"tool_result": "left\u0085right"}
            )
            audit_jsonl_text = audit_jsonl_path.read_text(encoding="utf-8")
            assert len(audit_jsonl_text.splitlines()) == 1
            assert json.loads(audit_jsonl_text)["tool_result"] == "left\u0085right"
            stream_accumulation = _stream_accumulation_self_test()
            upstream_idle_retry = await _upstream_idle_retry_self_test()
            system_messages = [
                item for item in first["messages"] if item.get("role") == "system"
            ]
            summary = {
                "status": "passed",
                "openjiuwen_version": OPENJIUWEN_VERSION,
                "openjiuwen_tag_commit": OPENJIUWEN_TAG_COMMIT,
                "model_calls": len(_FakeOpenRouterHandler.requests),
                "tool_calls": 1,
                "completion_confirmations": 2,
                "reasoning_replay_policy": "plaintext-or-structured-exclusive",
                "structured_reasoning_details_roundtrip": True,
                "structured_reasoning_content_omitted": True,
                "runtime_budget_rail_enabled": False,
                "context_compression": {
                    "enabled": True,
                    "preset": "openjiuwen-0.1.18-native",
                    "whole_context_trigger_ratio": 0.8,
                    "single_tool_result_offload_ratio": 0.1,
                    "session_memory_enabled": False,
                    "compression_recall_enabled": False,
                    "context_debug_enabled": True,
                },
                "wire_response_id_capture": True,
                "wire_response_id_capture_stage": (
                    "raw-stream-chunk-before-openjiuwen-inspection"
                ),
                "audit_jsonl_unicode_line_boundaries_escaped": True,
                "safe_cert_dir_scoped_and_restored": True,
                "tool_subprocess_api_key_visible": False,
                "stream_accumulation": stream_accumulation,
                "openrouter_upstream_idle_retry": upstream_idle_retry,
                "reasoning_effort": "high",
                "prompt_language": "en",
                "sampling_fields_absent": ["temperature", "top_p", "seed"],
                "max_tokens_absent": True,
                "workspace_scaffold_created": False,
                "tool_names": sorted(
                    item["function"]["name"] for item in first["tools"]
                ),
                "tool_schema_sha256": hashlib.sha256(
                    json.dumps(first["tools"], sort_keys=True).encode("utf-8")
                ).hexdigest(),
                "system_prompt_sha256": hashlib.sha256(
                    json.dumps(system_messages, sort_keys=True).encode("utf-8")
                ).hexdigest(),
            }
            if output_path is not None:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(
                    json.dumps(summary, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            print(json.dumps(summary, sort_keys=True))
    finally:
        server.shutdown()
        server.server_close()
        if old_safe_cert_dir is None:
            os.environ.pop("SAFE_CERT_DIR", None)
        else:
            os.environ["SAFE_CERT_DIR"] = old_safe_cert_dir
        if old_key is None:
            os.environ.pop("OPENROUTER_API_KEY", None)
        else:
            os.environ["OPENROUTER_API_KEY"] = old_key


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path)
    parser.add_argument("--instruction-file", type=Path)
    parser.add_argument("--workspace", type=Path, default=Path("/app"))
    parser.add_argument("--log-dir", type=Path, default=Path("/logs/agent/openjiuwen"))
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--self-test-output", type=Path)
    parser.add_argument("--api-key-stdin", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        asyncio.run(self_test(args.self_test_output))
        return 0
    if args.config is None or args.instruction_file is None:
        parser.error("--config and --instruction-file are required")
    config = json.loads(args.config.read_text(encoding="utf-8"))
    instruction = args.instruction_file.read_text(encoding="utf-8")
    api_key = None
    if args.api_key_stdin:
        api_key = sys.stdin.readline().rstrip("\r\n")
        if not api_key:
            raise RuntimeError("OpenRouter API key was not received on stdin")
    result = asyncio.run(
        run_agent(
            config,
            instruction,
            workspace=args.workspace.resolve(),
            log_dir=args.log_dir.resolve(),
            api_key=api_key,
        )
    )
    if isinstance(result, dict) and result.get("result_type") == "error":
        raise RuntimeError(str(result.get("error") or result.get("output") or result))
    output = result.get("output") if isinstance(result, dict) else result
    if output:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
