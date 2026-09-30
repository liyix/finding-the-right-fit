"""Frozen Claude Code controls shared with the qualified Harbor path."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

from ale_run.agents.claude_code.config import ClaudeCodeConfig


_ROUTE = {
    "only": ["anthropic"],
    "allow_fallbacks": False,
    "require_parameters": True,
}


@dataclass
class StudyClaudeCodeConfig(ClaudeCodeConfig):
    """Exact Claude Code × Claude Opus 5 study candidate for ALE."""

    name: ClassVar[str] = "claude-code"
    model: str = "anthropic/claude-opus-5[1m]"
    provider: str = "openrouter"
    base_url: str | None = None
    api_key: str | None = None
    cli_version: str = "@anthropic-ai/claude-code@2.1.251"
    effort_level: str | None = "high"
    max_thinking_tokens: int | None = None
    max_turns: int | None = -1
    max_budget_usd: float | None = None
    dangerously_skip_permissions: bool = True
    otel_enabled: bool = True
    # ALE's upstream safety list is retained for this ALE candidate.  It is a
    # runner-owned headless compatibility control and must be shown in every
    # paid manifest; the first real request must audit the resulting tools.
    disabled_tools: tuple[str, ...] = (
        "EnterPlanMode",
        "ExitPlanMode",
        "EnterWorktree",
        "ExitWorktree",
        "AskUserQuestion",
        "TaskOutput",
        "TaskStop",
        "RemoteTrigger",
    )
    context_tokens: int = 1_000_000
    max_output_tokens: int = 64_000
    upstream_base_url: str = "https://openrouter.ai/api"
    openrouter_route: dict[str, Any] = field(default_factory=lambda: dict(_ROUTE))

    def __post_init__(self) -> None:
        if self.model != "anthropic/claude-opus-5[1m]":
            raise ValueError("study Claude Code deployer accepts only Claude Opus 5 [1m]")
        if self.provider != "openrouter":
            raise ValueError("study Claude Code deployer requires OpenRouter")
        if self.effort_level != "high":
            raise ValueError("study reasoning target must remain high")
        if self.context_tokens != 1_000_000 or self.max_output_tokens != 64_000:
            raise ValueError("study Claude Code context/output controls have drifted")
        if self.openrouter_route != _ROUTE:
            raise ValueError("study Claude provider route has drifted")
