#!/usr/bin/env python3
"""Apply the pinned OpenHands 1.44.1 OpenRouter reasoning round-trip patch."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
from dataclasses import dataclass
from pathlib import Path


PATCH_ID = "openhands-1.44.1-openrouter-reasoning-roundtrip-v2"
PATCH_MARKER = "# harness-test patch: OpenRouter reasoning round-trip v2"
PACKAGE_VERSION = "1.44.1"


@dataclass(frozen=True)
class Replacement:
    path: str
    before: str
    after: str
    count: int = 1


REPLACEMENTS = (
    Replacement(
        "llm/message.py",
        "import json\nfrom abc import abstractmethod\n",
        (
            "# harness-test patch: OpenRouter reasoning round-trip v2\n"
            "import copy\nimport json\nfrom abc import abstractmethod\n"
        ),
    ),
    Replacement(
        "llm/message.py",
        '''    reasoning_content: str | None = Field(
        default=None,
        description="Intermediate reasoning/thinking content from reasoning models",
    )
    # Anthropic-specific thinking blocks (not normalized by LiteLLM)
''',
        '''    reasoning_content: str | None = Field(
        default=None,
        description="Intermediate reasoning/thinking content from reasoning models",
    )
    reasoning_details: list[dict[str, Any]] | None = Field(
        default=None,
        description=(
            "Opaque provider reasoning metadata. Preserve it byte-for-byte at the "
            "JSON-value level for multi-turn Chat Completions replay."
        ),
    )
    # Anthropic-specific thinking blocks (not normalized by LiteLLM)
''',
    ),
    Replacement(
        "llm/message.py",
        '''        # Required for model like kimi-k2-thinking
        if send_reasoning_content and self.reasoning_content:
            message_dict["reasoning_content"] = self.reasoning_content

        return message_dict
''',
        '''        # OpenRouter and other gateways use reasoning_details as opaque
        # continuation state. Prefer the complete structure over normalized text,
        # preserve an explicit empty array, and never reconstruct signatures.
        if self.role == "assistant" and self.reasoning_details is not None:
            message_dict["reasoning_details"] = copy.deepcopy(self.reasoning_details)
        elif send_reasoning_content and self.reasoning_content:
            message_dict["reasoning_content"] = self.reasoning_content

        return message_dict
''',
    ),
    Replacement(
        "llm/message.py",
        '''        rc = getattr(message, "reasoning_content", None)
        thinking_blocks = getattr(message, "thinking_blocks", None)
''',
        '''        rc = getattr(message, "reasoning_content", None)
        provider_fields = getattr(message, "provider_specific_fields", None)
        reasoning_details = None
        if isinstance(provider_fields, dict) and "reasoning_details" in provider_fields:
            reasoning_details = copy.deepcopy(provider_fields["reasoning_details"])
        elif getattr(message, "reasoning_details", None) is not None:
            reasoning_details = copy.deepcopy(message.reasoning_details)
        thinking_blocks = getattr(message, "thinking_blocks", None)
''',
    ),
    Replacement(
        "llm/message.py",
        '''            tool_calls=tool_calls,
            reasoning_content=rc,
            thinking_blocks=thinking_blocks,
''',
        '''            tool_calls=tool_calls,
            reasoning_content=rc,
            reasoning_details=reasoning_details,
            thinking_blocks=thinking_blocks,
''',
    ),
    Replacement(
        "event/llm_convertible/action.py",
        "from collections.abc import Sequence\n\n",
        "from collections.abc import Sequence\nfrom typing import Any\n\n",
    ),
    Replacement(
        "event/llm_convertible/action.py",
        '''    reasoning_content: str | None = Field(
        default=None,
        description="Intermediate reasoning/thinking content from reasoning models",
    )
    thinking_blocks: list[ThinkingBlock | RedactedThinkingBlock] = Field(
''',
        '''    reasoning_content: str | None = Field(
        default=None,
        description="Intermediate reasoning/thinking content from reasoning models",
    )
    reasoning_details: list[dict[str, Any]] | None = Field(
        default=None,
        description="Opaque provider reasoning metadata for exact replay",
    )
    thinking_blocks: list[ThinkingBlock | RedactedThinkingBlock] = Field(
''',
    ),
    Replacement(
        "event/llm_convertible/action.py",
        '''            tool_calls=[self.tool_call],
            reasoning_content=self.reasoning_content,
            thinking_blocks=self.thinking_blocks,
''',
        '''            tool_calls=[self.tool_call],
            reasoning_content=self.reasoning_content,
            reasoning_details=self.reasoning_details,
            thinking_blocks=self.thinking_blocks,
''',
    ),
    Replacement(
        "event/base.py",
        '''        reasoning_content=events[0].reasoning_content,  # Shared reasoning content
        thinking_blocks=events[0].thinking_blocks,  # Shared thinking blocks
''',
        '''        reasoning_content=events[0].reasoning_content,  # Shared reasoning content
        reasoning_details=events[0].reasoning_details,  # Shared opaque metadata
        thinking_blocks=events[0].thinking_blocks,  # Shared thinking blocks
''',
    ),
    Replacement(
        "agent/response_dispatch.py",
        '''        message.responses_reasoning_item is not None
        or message.reasoning_content is not None
        or message.thinking_blocks
''',
        '''        message.responses_reasoning_item is not None
        or message.reasoning_content is not None
        or message.reasoning_details is not None
        or message.thinking_blocks
''',
    ),
    Replacement(
        "agent/response_dispatch.py",
        '''            reasoning_content: str | None = None,
            thinking_blocks: (
''',
        '''            reasoning_content: str | None = None,
            reasoning_details: list[dict[str, object]] | None = None,
            thinking_blocks: (
''',
    ),
    Replacement(
        "agent/response_dispatch.py",
        '''                reasoning_content=(message.reasoning_content if i == 0 else None),
                thinking_blocks=(list(message.thinking_blocks) if i == 0 else []),
''',
        '''                reasoning_content=(message.reasoning_content if i == 0 else None),
                reasoning_details=(message.reasoning_details if i == 0 else None),
                thinking_blocks=(list(message.thinking_blocks) if i == 0 else []),
''',
        count=2,
    ),
    Replacement(
        "agent/agent.py",
        '''        reasoning_content: str | None = None,
        thinking_blocks: list[ThinkingBlock | RedactedThinkingBlock] | None = None,
        responses_reasoning_item: ReasoningItemModel | None = None,
    ) -> None:
''',
        '''        reasoning_content: str | None = None,
        reasoning_details: list[dict[str, object]] | None = None,
        thinking_blocks: list[ThinkingBlock | RedactedThinkingBlock] | None = None,
        responses_reasoning_item: ReasoningItemModel | None = None,
    ) -> None:
''',
    ),
    Replacement(
        "agent/agent.py",
        '''            thought=thought or [],
            reasoning_content=reasoning_content,
            thinking_blocks=thinking_blocks or [],
''',
        '''            thought=thought or [],
            reasoning_content=reasoning_content,
            reasoning_details=reasoning_details,
            thinking_blocks=thinking_blocks or [],
''',
        count=2,
    ),
    Replacement(
        "agent/agent.py",
        '''        thought: list[TextContent] | None = None,
        reasoning_content: str | None = None,
        thinking_blocks: list[ThinkingBlock | RedactedThinkingBlock] | None = None,
        responses_reasoning_item: ReasoningItemModel | None = None,
    ) -> ActionEvent | None:
''',
        '''        thought: list[TextContent] | None = None,
        reasoning_content: str | None = None,
        reasoning_details: list[dict[str, object]] | None = None,
        thinking_blocks: list[ThinkingBlock | RedactedThinkingBlock] | None = None,
        responses_reasoning_item: ReasoningItemModel | None = None,
    ) -> ActionEvent | None:
''',
    ),
    Replacement(
        "agent/agent.py",
        '''                    thought=thought,
                    reasoning_content=reasoning_content,
                    thinking_blocks=thinking_blocks,
                    responses_reasoning_item=responses_reasoning_item,
''',
        '''                    thought=thought,
                    reasoning_content=reasoning_content,
                    reasoning_details=reasoning_details,
                    thinking_blocks=thinking_blocks,
                    responses_reasoning_item=responses_reasoning_item,
''',
    ),
    Replacement(
        "agent/agent.py",
        '''                thought=thought,
                reasoning_content=reasoning_content,
                thinking_blocks=thinking_blocks,
                responses_reasoning_item=responses_reasoning_item,
''',
        '''                thought=thought,
                reasoning_content=reasoning_content,
                reasoning_details=reasoning_details,
                thinking_blocks=thinking_blocks,
                responses_reasoning_item=responses_reasoning_item,
''',
    ),
    # OpenHands 1.44.1 stores only the final OpenAI Responses reasoning item
    # when one response contains several.  Keep the legacy singular field for
    # old event compatibility, but make the ordered plural field authoritative
    # for new response capture and replay.
    Replacement(
        "llm/message.py",
        '''    responses_reasoning_item: ReasoningItemModel | None = Field(
        default=None,
        description="OpenAI Responses reasoning item from model output",
    )
''',
        '''    responses_reasoning_item: ReasoningItemModel | None = Field(
        default=None,
        description=(
            "Legacy final OpenAI Responses reasoning item from model output"
        ),
    )
    responses_reasoning_items: list[ReasoningItemModel] = Field(
        default_factory=list,
        description=(
            "All OpenAI Responses reasoning items in provider output order; "
            "authoritative for continuation replay"
        ),
    )
''',
    ),
    Replacement(
        "llm/message.py",
        '''        responses_reasoning_item: ReasoningItemModel | None = None

        # Helper to access fields from typed Pydantic objects, generic
''',
        '''        responses_reasoning_item: ReasoningItemModel | None = None
        responses_reasoning_items: list[ReasoningItemModel] = []

        # Helper to access fields from typed Pydantic objects, generic
''',
    ),
    Replacement(
        "llm/message.py",
        '''                        encrypted_content=_get(item, "encrypted_content"),
                        status=_get(item, "status"),
                    )

        assistant_text = "\\n".join(assistant_text_parts).strip()
''',
        '''                        encrypted_content=_get(item, "encrypted_content"),
                        status=_get(item, "status"),
                    )
                responses_reasoning_items.append(responses_reasoning_item)

        assistant_text = "\\n".join(assistant_text_parts).strip()
''',
    ),
    Replacement(
        "llm/message.py",
        '''            tool_calls=tool_calls or None,
            responses_reasoning_item=responses_reasoning_item,
        )
''',
        '''            tool_calls=tool_calls or None,
            responses_reasoning_item=responses_reasoning_item,
            responses_reasoning_items=responses_reasoning_items,
        )
''',
    ),
    Replacement(
        "llm/utils/responses_serialization.py",
        '''    reasoning_item = _build_reasoning_item(message.responses_reasoning_item)
    if reasoning_item:
        items.append(reasoning_item)
''',
        '''    # New events preserve every reasoning output item.  Old serialized
    # events have only the legacy singular field, so retain that fallback.
    reasoning_items = message.responses_reasoning_items
    if not reasoning_items and message.responses_reasoning_item is not None:
        reasoning_items = [message.responses_reasoning_item]
    for source_item in reasoning_items:
        reasoning_item = _build_reasoning_item(source_item)
        if reasoning_item:
            items.append(reasoning_item)
''',
    ),
    Replacement(
        "event/llm_convertible/action.py",
        '''    responses_reasoning_item: ReasoningItemModel | None = Field(
        default=None, description="OpenAI Responses reasoning item from model output"
    )
''',
        '''    responses_reasoning_item: ReasoningItemModel | None = Field(
        default=None,
        description="Legacy final OpenAI Responses reasoning item from model output",
    )
    responses_reasoning_items: list[ReasoningItemModel] = Field(
        default_factory=list,
        description="All OpenAI Responses reasoning items in provider output order",
    )
''',
    ),
    Replacement(
        "event/llm_convertible/action.py",
        '''            responses_reasoning_item=self.responses_reasoning_item,
        )
''',
        '''            responses_reasoning_item=self.responses_reasoning_item,
            responses_reasoning_items=self.responses_reasoning_items,
        )
''',
    ),
    Replacement(
        "event/base.py",
        '''        # Shared responses reasoning item
        responses_reasoning_item=events[0].responses_reasoning_item,
    )
''',
        '''        # Shared Responses reasoning state lives only on the first action.
        responses_reasoning_item=events[0].responses_reasoning_item,
        responses_reasoning_items=events[0].responses_reasoning_items,
    )
''',
    ),
    Replacement(
        "agent/response_dispatch.py",
        '''        message.responses_reasoning_item is not None
        or message.reasoning_content is not None
        or message.reasoning_details is not None
''',
        '''        message.responses_reasoning_item is not None
        or message.responses_reasoning_items
        or message.reasoning_content is not None
        or message.reasoning_details is not None
''',
    ),
    Replacement(
        "agent/response_dispatch.py",
        '''            responses_reasoning_item: ReasoningItemModel | None = None,
        ) -> ActionEvent | None: ...
''',
        '''            responses_reasoning_item: ReasoningItemModel | None = None,
            responses_reasoning_items: list[ReasoningItemModel] | None = None,
        ) -> ActionEvent | None: ...
''',
    ),
    Replacement(
        "agent/response_dispatch.py",
        '''                responses_reasoning_item=(
                    message.responses_reasoning_item if i == 0 else None
                ),
''',
        '''                responses_reasoning_item=(
                    message.responses_reasoning_item if i == 0 else None
                ),
                responses_reasoning_items=(
                    list(message.responses_reasoning_items) if i == 0 else []
                ),
''',
        count=2,
    ),
    Replacement(
        "agent/agent.py",
        '''        responses_reasoning_item: ReasoningItemModel | None = None,
    ) -> None:
''',
        '''        responses_reasoning_item: ReasoningItemModel | None = None,
        responses_reasoning_items: list[ReasoningItemModel] | None = None,
    ) -> None:
''',
    ),
    Replacement(
        "agent/agent.py",
        '''        responses_reasoning_item: ReasoningItemModel | None = None,
    ) -> ActionEvent | None:
''',
        '''        responses_reasoning_item: ReasoningItemModel | None = None,
        responses_reasoning_items: list[ReasoningItemModel] | None = None,
    ) -> ActionEvent | None:
''',
    ),
    Replacement(
        "agent/agent.py",
        '''            responses_reasoning_item=responses_reasoning_item,
''',
        '''            responses_reasoning_item=responses_reasoning_item,
            responses_reasoning_items=responses_reasoning_items or [],
''',
        count=4,
    ),
    Replacement(
        "llm/llm.py",
        '''                if m.role == "assistant" and m.responses_reasoning_item is not None:
                    m.responses_reasoning_item = None
''',
        '''                if m.role == "assistant" and (
                    m.responses_reasoning_item is not None
                    or m.responses_reasoning_items
                ):
                    m.responses_reasoning_item = None
                    m.responses_reasoning_items = []
''',
    ),
)


def package_root() -> Path:
    distribution = importlib.metadata.distribution("openhands-sdk")
    return Path(distribution.locate_file("openhands/sdk")).resolve()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def self_test() -> dict[str, object]:
    """Exercise capture, event persistence, merge, and Responses replay offline."""
    from types import SimpleNamespace

    from openhands.sdk.agent.response_dispatch import ResponseDispatchMixin
    from openhands.sdk.event.base import _combine_action_events
    from openhands.sdk.event.llm_convertible.action import ActionEvent
    from openhands.sdk.llm import Message

    raw_output: list[dict[str, object]] = []
    for index in range(3):
        raw_output.append(
            {
                "type": "reasoning",
                "id": f"rs_test_{index}",
                "summary": [
                    {"type": "summary_text", "text": f"summary-{index}"}
                ],
                "content": [
                    {"type": "reasoning_text", "text": f"content-{index}"}
                ],
                "encrypted_content": f"encrypted-{index}",
                "status": "completed",
            }
        )
    raw_output.append(
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "tool batch"}],
        }
    )
    for index in range(3):
        raw_output.append(
            {
                "type": "function_call",
                "id": f"fc_test_{index}",
                "call_id": f"call_test_{index}",
                "name": "terminal",
                "arguments": json.dumps({"command": f"echo {index}"}),
            }
        )

    message = Message.from_llm_responses_output(raw_output)
    expected_reasoning = [
        (f"rs_test_{index}", f"encrypted-{index}") for index in range(3)
    ]
    captured_reasoning = [
        (item.id, item.encrypted_content)
        for item in message.responses_reasoning_items
    ]
    if captured_reasoning != expected_reasoning:
        raise RuntimeError(
            f"reasoning capture mismatch: {captured_reasoning!r}"
        )
    if message.responses_reasoning_item != message.responses_reasoning_items[-1]:
        raise RuntimeError("legacy singular reasoning field is not the final item")
    if message.tool_calls is None or len(message.tool_calls) != 3:
        raise RuntimeError("synthetic Responses tool batch did not parse")

    class CaptureDispatch(ResponseDispatchMixin):
        def __init__(self) -> None:
            self.captured: list[dict[str, object]] = []

        def _get_action_event(self, _tool_call: object, **kwargs: object) -> object:
            self.captured.append(kwargs)
            return object()

        def _requires_user_confirmation(
            self, _state: object, _events: list[object]
        ) -> bool:
            return False

        def _execute_actions(
            self, _conversation: object, _events: list[object], _callback: object
        ) -> None:
            return None

        def _maybe_emit_vllm_tokens(
            self, _response: object, _callback: object
        ) -> None:
            return None

    dispatcher = CaptureDispatch()
    dispatcher._handle_tool_calls(
        message,
        SimpleNamespace(id="resp_test"),
        SimpleNamespace(),
        SimpleNamespace(security_analyzer=None),
        lambda _event: None,
    )
    dispatched_counts = [
        len(value) if isinstance(value, list) else -1
        for value in (
            item.get("responses_reasoning_items") for item in dispatcher.captured
        )
    ]
    if dispatched_counts != [3, 0, 0]:
        raise RuntimeError(
            f"reasoning dispatch placement mismatch: {dispatched_counts!r}"
        )

    events: list[ActionEvent] = []
    for index, tool_call in enumerate(message.tool_calls):
        event = ActionEvent(
            thought=message.content if index == 0 else [],
            reasoning_content=message.reasoning_content if index == 0 else None,
            reasoning_details=message.reasoning_details if index == 0 else None,
            thinking_blocks=(list(message.thinking_blocks) if index == 0 else []),
            responses_reasoning_item=(
                message.responses_reasoning_item if index == 0 else None
            ),
            responses_reasoning_items=(
                list(message.responses_reasoning_items) if index == 0 else []
            ),
            action=None,
            tool_name=tool_call.name,
            tool_call_id=tool_call.id,
            tool_call=tool_call,
            llm_response_id="resp_test",
        )
        # Round-trip through the same JSON-shaped event representation saved by
        # the controlled Harbor runner, rather than relying on object identity.
        events.append(ActionEvent.model_validate(event.model_dump(mode="json")))

    combined = _combine_action_events(events)
    replay = combined.to_responses_dict(vision_enabled=False)
    expected_types = ["reasoning", "reasoning", "reasoning", "message"] + [
        "function_call"
    ] * 3
    replay_types = [item.get("type") for item in replay]
    if replay_types != expected_types:
        raise RuntimeError(f"Responses replay order mismatch: {replay_types!r}")
    replay_reasoning = [
        (item.get("id"), item.get("encrypted_content"))
        for item in replay
        if item.get("type") == "reasoning"
    ]
    if replay_reasoning != expected_reasoning:
        raise RuntimeError(f"reasoning replay mismatch: {replay_reasoning!r}")

    legacy_message = Message(
        role="assistant",
        responses_reasoning_item=message.responses_reasoning_items[-1],
    )
    legacy_replay = legacy_message.to_responses_dict(vision_enabled=False)
    if len(legacy_replay) != 1 or legacy_replay[0].get("id") != "rs_test_2":
        raise RuntimeError("legacy singular reasoning replay fallback failed")

    return {
        "status": "passed",
        "reasoning_items": len(expected_reasoning),
        "tool_calls": 3,
        "event_json_roundtrip": True,
        "dispatch_item_counts": dispatched_counts,
        "legacy_singular_fallback": True,
    }


def apply(root: Path) -> dict[str, object]:
    root = root.resolve()
    try:
        installed_root = package_root()
    except importlib.metadata.PackageNotFoundError:
        installed_root = None
    if (
        installed_root is not None
        and root == installed_root
        and importlib.metadata.version("openhands-sdk") != PACKAGE_VERSION
    ):
        raise RuntimeError("refusing to patch an OpenHands version other than 1.44.1")

    touched = {root / replacement.path for replacement in REPLACEMENTS}
    if PATCH_MARKER in (root / "llm/message.py").read_text(encoding="utf-8"):
        return {
            "patch_id": PATCH_ID,
            "package_version": PACKAGE_VERSION,
            "status": "already-applied",
            "changed_files": [],
            "file_sha256": {
                str(path.relative_to(root)): sha256(path) for path in sorted(touched)
            },
        }

    changed: set[Path] = set()
    for replacement in REPLACEMENTS:
        path = root / replacement.path
        text = path.read_text(encoding="utf-8")
        count_before = text.count(replacement.before)
        count_after = text.count(replacement.after)
        if (
            replacement.before in replacement.after
            and count_after == replacement.count
        ):
            continue
        if count_before == replacement.count:
            path.write_text(
                text.replace(
                    replacement.before, replacement.after, replacement.count
                ),
                encoding="utf-8",
            )
            changed.add(path)
        elif count_before == 0 and count_after == replacement.count:
            continue
        else:
            raise RuntimeError(
                f"unexpected source shape for {path}: before={count_before}, after={count_after}"
            )

    return {
        "patch_id": PATCH_ID,
        "package_version": PACKAGE_VERSION,
        "status": "applied" if changed else "already-applied",
        "changed_files": sorted(str(path.relative_to(root)) for path in changed),
        "file_sha256": {
            str(path.relative_to(root)): sha256(path) for path in sorted(touched)
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path)
    parser.add_argument("--record", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve() if args.root else package_root()
    record = apply(root)
    if args.self_test:
        record["self_test"] = self_test()
    encoded = json.dumps(record, indent=2, sort_keys=True) + "\n"
    if args.record:
        args.record.parent.mkdir(parents=True, exist_ok=True)
        args.record.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
