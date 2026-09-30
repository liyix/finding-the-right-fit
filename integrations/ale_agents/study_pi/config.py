"""Frozen PI model routes for the ALE study deployer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar


PI_MODEL_SPECS: dict[str, dict[str, Any]] = {
    "anthropic/claude-opus-5": {
        "route": {"only": ["anthropic"], "allow_fallbacks": False, "require_parameters": True},
        "context": 1_000_000,
        "max_output": 128_000,
    },
    "openai/gpt-6-astra": {
        "route": {"only": ["openai"], "allow_fallbacks": False, "require_parameters": True},
        "context": 1_050_000,
        "max_output": 128_000,
    },
    "moonshotai/kimi-k3": {
        "route": {
            "only": ["moonshotai/mxfp4"],
            "quantizations": ["mxfp4"],
            "allow_fallbacks": False,
            "require_parameters": True,
        },
        "context": 1_048_576,
        # Preserve PI 0.84.4's bundled non-batch model entry.
        "max_output": 131_072,
    },
    "deepseek/deepseek-v4-pro-0813": {
        "route": {"only": ["deepseek"], "allow_fallbacks": False, "require_parameters": True},
        "context": 1_048_576,
        "max_output": 384_000,
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
    },
}


@dataclass
class StudyPiConfig:
    name: ClassVar[str] = "pi"

    model: str = "anthropic/claude-opus-5"
    provider: str = "openrouter"
    api_key: str | None = None
    cli_version: str = "0.84.4"
    thinking: str = "high"

    def __post_init__(self) -> None:
        if self.model not in PI_MODEL_SPECS:
            raise ValueError(f"unregistered PI study model: {self.model}")
        if self.provider != "openrouter":
            raise ValueError("study PI deployer requires OpenRouter")
        if self.cli_version != "0.84.4":
            raise ValueError("study PI version has drifted")
        if self.thinking != "high":
            raise ValueError("study reasoning target must remain high")

    @property
    def model_spec(self) -> dict[str, Any]:
        return PI_MODEL_SPECS[self.model]

    def models_json(self) -> dict[str, Any]:
        return {
            "providers": {
                "openrouter": {
                    "modelOverrides": {
                        self.model: {
                            "compat": {
                                "maxTokensField": "max_tokens",
                                "openRouterRouting": self.model_spec["route"],
                            }
                        }
                    }
                }
            }
        }
