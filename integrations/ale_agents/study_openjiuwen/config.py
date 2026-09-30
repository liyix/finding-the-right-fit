"""Frozen openJiuwen 0.1.18 controls for ALE-CLI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar


OPENJIUWEN_MODEL_SPECS: dict[str, dict[str, Any]] = {
    "anthropic/claude-opus-5": {
        "route": {"only": ["anthropic"], "allow_fallbacks": False,
                  "require_parameters": True},
        "context": 1_000_000,
    },
    "openai/gpt-6-astra": {
        "route": {"only": ["openai"], "allow_fallbacks": False,
                  "require_parameters": True},
        "context": 1_050_000,
    },
    "openai/gpt-6-astra": {
        "route": {"only": ["openai"], "allow_fallbacks": False,
                  "require_parameters": True},
        "context": 1_050_000,
    },
    "moonshotai/kimi-k3": {
        "route": {"only": ["moonshotai/mxfp4"], "quantizations": ["mxfp4"],
                  "allow_fallbacks": False, "require_parameters": True},
        "context": 1_048_576,
    },
    "deepseek/deepseek-v4-pro-0813": {
        "route": {"only": ["deepseek"], "allow_fallbacks": False,
                  "require_parameters": True},
        "context": 1_048_576,
    },
    "z-ai/glm-5.3": {
        "route": {"only": ["z-ai/fp8"], "quantizations": ["fp8"],
                  "allow_fallbacks": False, "require_parameters": True},
        "context": 1_048_576,
    },
}


@dataclass
class StudyOpenJiuwenConfig:
    """Only fields consumed by the candidate ALE deployer."""

    name: ClassVar[str] = "openjiuwen-coding-agent"

    model: str = "deepseek/deepseek-v4-pro-0813"
    provider: str = "openrouter"
    api_key: str | None = None
    version: str = "0.1.18"
    runtime_version: str = "0.1.18-r1"
    reasoning_effort: str = "high"
    runtime_budget_rail_enabled: bool = False
    context_compression_enabled: bool = True
    max_outer_rounds: int = 8
    prompt_language: str = "en"
    llm_request_timeout_seconds: int = 360
    llm_stream_first_chunk_timeout_seconds: int = 300
    llm_stream_idle_timeout_seconds: int = 300
    llm_http_max_retries: int = 5
    mcp_policy: str = "ale-cua-only"

    def __post_init__(self) -> None:
        if self.model not in OPENJIUWEN_MODEL_SPECS:
            raise ValueError(f"unregistered openJiuwen study model: {self.model}")
        expected = {
            "provider": "openrouter",
            "version": "0.1.18",
            "runtime_version": "0.1.18-r1",
            "reasoning_effort": "high",
            "runtime_budget_rail_enabled": False,
            "context_compression_enabled": True,
            "max_outer_rounds": 8,
            "prompt_language": "en",
            "llm_request_timeout_seconds": 360,
            "llm_stream_first_chunk_timeout_seconds": 300,
            "llm_stream_idle_timeout_seconds": 300,
            "llm_http_max_retries": 5,
            "mcp_policy": "ale-cua-only",
        }
        for field_name, expected_value in expected.items():
            actual = getattr(self, field_name)
            if actual != expected_value:
                raise ValueError(
                    f"openJiuwen study control drift: {field_name}={actual!r}; "
                    f"expected {expected_value!r}"
                )

    @property
    def model_spec(self) -> dict[str, Any]:
        return OPENJIUWEN_MODEL_SPECS[self.model]

    def safe_runner_config(
        self,
        *,
        runtime_budget_seconds: int,
        mcp_command: str,
        mcp_script: str,
        cua_url: str,
        work_dir: str,
    ) -> dict[str, Any]:
        """Build the secret-free config persisted with one ALE trial."""
        if runtime_budget_seconds <= 0:
            raise ValueError("ALE runtime budget must be positive")
        route = self.model_spec["route"]
        payload: dict[str, Any] = {
            "model_id": self.model,
            "provider_only": list(route["only"]),
            "context_window": int(self.model_spec["context"]),
            "reasoning_effort": self.reasoning_effort,
            "runtime_budget_seconds": runtime_budget_seconds,
            "runtime_budget_rail_enabled": self.runtime_budget_rail_enabled,
            "context_compression_enabled": self.context_compression_enabled,
            "completion_timeout_seconds": runtime_budget_seconds,
            "max_outer_rounds": self.max_outer_rounds,
            "prompt_language": self.prompt_language,
            "llm_request_timeout_seconds": self.llm_request_timeout_seconds,
            "llm_stream_first_chunk_timeout_seconds": (
                self.llm_stream_first_chunk_timeout_seconds
            ),
            "llm_stream_idle_timeout_seconds": self.llm_stream_idle_timeout_seconds,
            "llm_http_max_retries": self.llm_http_max_retries,
            "base_url": "https://openrouter.ai/api/v1",
            "mcp_servers": [{
                "server_name": "cua",
                "client_type": "stdio",
                "command": mcp_command,
                "args": [mcp_script],
                "env": {"CUA_SERVER_URL": cua_url},
                "cwd": work_dir,
                "include_image_content": True,
            }],
        }
        if route.get("quantizations"):
            payload["quantizations"] = list(route["quantizations"])
        return payload
