"""Frozen DSH rc.2 model routes for the ALE study deployer."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Literal


_BASE_COMPAT: dict[str, Any] = {
    "supportsDeveloperRole": False,
    "supportsReasoningEffort": True,
    "maxTokensField": "max_tokens",
    "thinkingFormat": "openrouter",
}


DSH_MODEL_SPECS: dict[str, dict[str, Any]] = {
    "anthropic/claude-opus-5": {
        "route": {
            "only": ["anthropic"],
            "allow_fallbacks": False,
            "require_parameters": True,
        },
        "context": 1_000_000,
        "max_output": 128_000,
        "input": ["text", "image"],
        "compat": {**_BASE_COMPAT, "cacheControlFormat": "anthropic"},
        "cache_retention": "short",
    },
    "openai/gpt-6-astra": {
        "route": {
            "only": ["openai"],
            "allow_fallbacks": False,
            "require_parameters": True,
        },
        "context": 1_050_000,
        "max_output": 128_000,
        "input": ["text", "image"],
        "compat": dict(_BASE_COMPAT),
        "cache_retention": None,
    },
    "moonshotai/kimi-k3": {
        "route": {
            "only": ["moonshotai/mxfp4"],
            "quantizations": ["mxfp4"],
            "allow_fallbacks": False,
            "require_parameters": True,
        },
        "context": 1_048_576,
        "max_output": 943_718,
        "input": ["text", "image"],
        "compat": dict(_BASE_COMPAT),
        "cache_retention": None,
    },
    "deepseek/deepseek-v4-pro-0813": {
        "route": {
            "only": ["deepseek"],
            "allow_fallbacks": False,
            "require_parameters": True,
        },
        "context": 1_048_576,
        "max_output": 384_000,
        "input": ["text"],
        "compat": dict(_BASE_COMPAT),
        "cache_retention": None,
    },
    "z-ai/glm-5.3": {
        "route": {
            "only": ["z-ai/fp8"],
            "quantizations": ["fp8"],
            "allow_fallbacks": False,
            "require_parameters": True,
        },
        "context": 1_048_576,
        "max_output": 131_072,
        "input": ["text"],
        "compat": dict(_BASE_COMPAT),
        "cache_retention": None,
    },
}


def build_harbor_dataset_config(
    *,
    job_name: str,
    jobs_dir: Path,
    dataset_path: Path,
    task_names: list[str],
    model_id: str,
    route: dict[str, Any],
    context_window: int,
    max_tokens: int,
    input_modalities: list[Literal["text", "image"]],
    compat: dict[str, Any],
    cache_retention: Literal["none", "short", "long"] | None = None,
    agent_setup_timeout_multiplier: float = 1.0,
    install_only: bool = False,
) -> dict[str, Any]:
    """Build one explicit Harbor dataset job without importing Harbor itself."""
    if not task_names:
        raise ValueError("at least one task name is required")
    if agent_setup_timeout_multiplier <= 0:
        raise ValueError("agent_setup_timeout_multiplier must be positive")
    return {
        "job_name": job_name,
        "jobs_dir": str(jobs_dir),
        "n_attempts": 1,
        "install_only": install_only,
        "timeout_multiplier": 1.0,
        "agent_timeout_multiplier": 1.0,
        "verifier_timeout_multiplier": 1.0,
        "environment_build_timeout_multiplier": 1.0,
        "agent_setup_timeout_multiplier": agent_setup_timeout_multiplier,
        "n_concurrent_trials": 1,
        "retry": {"max_retries": 0},
        "environment": {
            "type": "docker",
            "force_build": False,
            "delete": True,
            "cpu_enforcement_policy": "auto",
            "memory_enforcement_policy": "auto",
        },
        "agents": [{
            "name": "integrations.harbor_deepseek:DeepSeekHarness",
            "model_name": f"openrouter/{model_id}",
            "n_concurrent": 1,
            "skills": [],
            "resume_trajectory": False,
            "include_logs": ["**/*"],
            "kwargs": {
                "version": "0.1.1-rc.2",
                "adapter": "pi-ai",
                "model_api": "openai-completions",
                "context_window": context_window,
                "max_tokens": max_tokens,
                "input_modalities": input_modalities,
                "reasoning_effort": "high",
                "cache_retention": cache_retention,
                "compat": compat,
                "openrouter_route": route,
                "permission_mode": "danger-full-access",
            },
            "env": {"OPENROUTER_API_KEY": "${OPENROUTER_API_KEY}"},
            "mcp_servers": [],
        }],
        "datasets": [{"path": str(dataset_path), "task_names": task_names}],
        "tasks": [],
        "artifacts": [],
        "extra_instruction_paths": [],
        "extra_instructions": [],
    }


@dataclass
class StudyDeepSeekConfig:
    name: ClassVar[str] = "deepseek-harness"

    model: str = "anthropic/claude-opus-5"
    provider: str = "openrouter"
    api_key: str | None = None
    cli_version: str = "0.1.1-rc.2"
    reasoning_effort: str = "high"
    permission_mode: str = "danger-full-access"
    base_url: str = "http://127.0.0.1:4010/v1"
    injector_upstream: str = "https://openrouter.ai/api"
    injector_port: int = 4010
    mcp_policy: str = "ale-cua-only"

    def __post_init__(self) -> None:
        if self.model not in DSH_MODEL_SPECS:
            raise ValueError(f"unregistered DSH study model: {self.model}")
        if self.provider != "openrouter":
            raise ValueError("study DSH deployer requires OpenRouter")
        if self.cli_version != "0.1.1-rc.2":
            raise ValueError("study DSH version has drifted")
        if self.reasoning_effort != "high":
            raise ValueError("study reasoning target must remain high")
        if self.permission_mode != "danger-full-access":
            raise ValueError("DSH must use ALE's outer sandbox as its safety boundary")
        if self.base_url != f"http://127.0.0.1:{self.injector_port}/v1":
            raise ValueError("DSH base_url must resolve to the local strict-route injector")
        if self.mcp_policy != "ale-cua-only":
            raise ValueError("ALE exposes only its benchmark CUA MCP server")

    @property
    def model_spec(self) -> dict[str, Any]:
        return DSH_MODEL_SPECS[self.model]
