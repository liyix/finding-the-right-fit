"""Frozen study controls for stock Codex on ALE-CLI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ale_run.agents.codex.config import CodexConfig


@dataclass
class StudyCodexConfig(CodexConfig):
    """Use Codex's bundled GPT-5.6-Sol metadata without a custom catalog."""

    name: ClassVar[str] = "study_codex"

    model: str = "gpt-6-astra"
    provider: str = "openrouter"
    base_url: str | None = "http://127.0.0.1:4170/v1"
    reasoning_effort: str = "high"
    codex_version: str = "0.150.1"
    patched_binary_url: str = ""
    patched_binary_url_windows: str = ""
    fork_version: str = "0.150.1"
    model_catalog_path: str = ""
    model_catalog_content: str = ""
    otel_enabled: bool = True

    injector_port: int = 4170
    injector_upstream: str = "https://openrouter.ai/api"
    upstream_model: str = "openai/gpt-6-astra"
    provider_only: tuple[str, ...] = ("openai",)
    allow_fallbacks: bool = False
    require_parameters: bool = False

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.model != "gpt-6-astra":
            raise ValueError("StudyCodexConfig only permits bundled gpt-6-astra")
        if self.provider != "openrouter":
            raise ValueError("StudyCodexConfig requires provider='openrouter'")
        expected = f"http://127.0.0.1:{self.injector_port}/v1"
        if self.base_url != expected:
            raise ValueError(f"StudyCodexConfig base_url must be {expected!r}")
        if self.reasoning_effort != "high":
            raise ValueError("study-wide reasoning_effort must remain high")
        if self.feature_overrides:
            raise ValueError("ALE stock-Codex qualification forbids feature overrides")
