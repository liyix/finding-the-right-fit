"""Frozen OpenHands SDK controls for ALE-CLI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar


OPENHANDS_MODEL_SPECS: dict[str, dict[str, Any]] = {
    "anthropic/claude-opus-5": {
        "route": {
            "only": ["anthropic"],
            "allow_fallbacks": False,
            "require_parameters": True,
        },
        "endpoint_context": 1_000_000,
        "endpoint_max_output": 128_000,
        "capability_overrides": {"supports_vision": True},
        "inline_image_urls": None,
    },
    "openai/gpt-6-astra": {
        "route": {
            "only": ["openai"],
            "allow_fallbacks": False,
            "require_parameters": True,
        },
        "endpoint_context": 1_050_000,
        "endpoint_max_output": 128_000,
        "capability_overrides": {"supports_vision": True},
        "inline_image_urls": None,
    },
    "moonshotai/kimi-k3": {
        "route": {
            "only": ["moonshotai/mxfp4"],
            "quantizations": ["mxfp4"],
            "allow_fallbacks": False,
            "require_parameters": True,
        },
        "endpoint_context": 1_048_576,
        "endpoint_max_output": 943_718,
        "capability_overrides": {
            "supports_reasoning_effort": True,
            "supports_vision": True,
        },
        "inline_image_urls": True,
    },
    "deepseek/deepseek-v4-pro-0813": {
        "route": {
            "only": ["deepseek"],
            "allow_fallbacks": False,
            "require_parameters": True,
        },
        "endpoint_context": 1_048_576,
        "endpoint_max_output": 384_000,
        "capability_overrides": {
            "supports_reasoning_effort": True,
            "supports_vision": False,
        },
        "inline_image_urls": None,
    },
    "z-ai/glm-5.3": {
        "route": {
            "only": ["z-ai/fp8"],
            "quantizations": ["fp8"],
            "allow_fallbacks": False,
            "require_parameters": True,
        },
        "endpoint_context": 1_048_576,
        "endpoint_max_output": 131_072,
        "capability_overrides": {
            "supports_reasoning_effort": True,
            "supports_vision": False,
        },
        "inline_image_urls": None,
    },
}


@dataclass
class StudyOpenHandsConfig:
    """Only controls consumed by the study's SDK deployer."""

    name: ClassVar[str] = "openhands-sdk"

    model: str = "anthropic/claude-opus-5"
    provider: str = "openrouter"
    base_url: str = "https://openrouter.ai/api/v1"
    api_key: str | None = None
    sdk_version: str = "1.44.1"
    tools_version: str = "1.44.1"
    reasoning_effort: str = "high"
    max_input_tokens: int = 1_000_000
    max_output_tokens: int = 128_000
    api_mode: str = "auto"
    max_iterations: int = 500
    temperature: float | None = None
    top_p: float | None = None
    seed: int | None = None
    load_skills: bool = False
    condenser: str | None = None
    mcp_policy: str = "ale-cua-only"

    def __post_init__(self) -> None:
        if self.model not in OPENHANDS_MODEL_SPECS:
            raise ValueError(f"unregistered OpenHands study model: {self.model}")
        expected = {
            "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "sdk_version": "1.44.1",
            "tools_version": "1.44.1",
            "reasoning_effort": "high",
            "max_input_tokens": 1_000_000,
            "max_output_tokens": 128_000,
            "api_mode": "auto",
            "max_iterations": 500,
            "temperature": None,
            "top_p": None,
            "seed": None,
            "load_skills": False,
            "condenser": None,
            "mcp_policy": "ale-cua-only",
        }
        for field_name, expected_value in expected.items():
            actual = getattr(self, field_name)
            if actual != expected_value:
                raise ValueError(
                    f"OpenHands study control drift: {field_name}={actual!r}; "
                    f"expected {expected_value!r}"
                )

    @property
    def model_spec(self) -> dict[str, Any]:
        return OPENHANDS_MODEL_SPECS[self.model]

    def safe_runner_config(
        self, *, mcp_command: str, mcp_args: list[str], cua_url: str
    ) -> dict[str, Any]:
        """Return a secret-free runner config frozen into the trial artifacts."""
        return {
            "model": f"openrouter/{self.model}",
            "provider_model": self.model,
            "provider": self.provider,
            "base_url": self.base_url,
            "sdk_version": self.sdk_version,
            "tools_version": self.tools_version,
            "reasoning_effort": self.reasoning_effort,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "api_mode": self.api_mode,
            "max_iterations": self.max_iterations,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "seed": self.seed,
            "load_skills": self.load_skills,
            "condenser": self.condenser,
            "litellm_extra_body": {"provider": self.model_spec["route"]},
            "capability_overrides": self.model_spec["capability_overrides"],
            "inline_image_urls": self.model_spec["inline_image_urls"],
            "endpoint_context_tokens": self.model_spec["endpoint_context"],
            "endpoint_max_output_tokens": self.model_spec["endpoint_max_output"],
            "mcp_policy": self.mcp_policy,
            "mcp_config": {
                "cua": {
                    "transport": "stdio",
                    "command": mcp_command,
                    "args": list(mcp_args),
                    "env": {"CUA_SERVER_URL": cua_url},
                }
            },
        }
