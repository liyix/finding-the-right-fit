"""ALE deployer for the qualified Claude Code × Claude control path."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

from ale_run.agents.claude_code.deployer import ClaudeCodeDeployer
from ale_run.base_interface import AgentRunResult, StepMetrics, TrajectoryBuilder

from .config import StudyClaudeCodeConfig
from .proxy import make_app


class StudyClaudeCodeDeployer(ClaudeCodeDeployer):
    """Reuse ALE's upstream deployer, adding only frozen study controls."""

    hot_artifacts: ClassVar[tuple[str, ...]] = (
        *ClaudeCodeDeployer.hot_artifacts,
        "provider-injector.jsonl",
    )

    def __init__(self, executor: Any):
        super().__init__(executor)
        self._injector_url: str | None = None

    def _build_env(
        self,
        cfg: StudyClaudeCodeConfig,
        *,
        otel_endpoint: str | None = None,
    ) -> dict[str, str]:
        if self._injector_url is None:
            raise RuntimeError("Claude Code provider injector is not running")
        env = super()._build_env(cfg, otel_endpoint=otel_endpoint)
        alias = cfg.model
        env.update({
            "ANTHROPIC_BASE_URL": self._injector_url,
            "ANTHROPIC_CUSTOM_HEADERS": "X-OpenRouter-Metadata: enabled",
            "ANTHROPIC_MODEL": alias,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": alias,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": alias,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": alias,
            "CLAUDE_CODE_SUBAGENT_MODEL": alias,
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS": str(cfg.context_tokens),
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(cfg.max_output_tokens),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "IS_SANDBOX": "1",
        })
        return env

    async def launch(self, prompt: str) -> AgentRunResult:
        from aiohttp import web

        cfg: StudyClaudeCodeConfig = self.config  # type: ignore[assignment]
        work_dir = Path(self.executor.work_dir)
        app = make_app(
            model="anthropic/claude-opus-5",
            route=cfg.openrouter_route,
            upstream=cfg.upstream_base_url,
            log_path=work_dir / "provider-injector.jsonl",
        )
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        sockets = getattr(site, "_server", None).sockets
        port = sockets[0].getsockname()[1]
        self._injector_url = f"http://127.0.0.1:{port}"
        try:
            return await super().launch(prompt)
        finally:
            self._injector_url = None
            await runner.cleanup()

    @classmethod
    def parse_artifacts(
        cls,
        *,
        work_dir: Path,
        config: StudyClaudeCodeConfig,
        run_result: AgentRunResult,
        builder: TrajectoryBuilder,
    ) -> None:
        super().parse_artifacts(
            work_dir=work_dir,
            config=config,
            run_result=run_result,
            builder=builder,
        )
        cls._repair_streamed_usage(work_dir, builder)
        records: list[dict[str, Any]] = []
        log_path = work_dir / "provider-injector.jsonl"
        if log_path.is_file():
            for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    records.append(record)
        message_records = [item for item in records if item.get("path") == "/v1/messages"]
        builder.trajectory.extra["provider_injector"] = {
            "source": str(log_path),
            "request_count": len(records),
            "messages_request_count": len(message_records),
            "all_messages_changed_only_provider": bool(message_records)
            and all(
                item.get("changed_fields") == ["provider"]
                and item.get("semantic_parity_except_provider") is True
                for item in message_records
            ),
            "upstream_statuses": [item.get("upstream_status") for item in message_records],
            "injector_retry_count": sum(
                int(item.get("injector_retry_count") or 0) for item in records
            ),
            "route": config.openrouter_route,
        }

    @staticmethod
    def _repair_streamed_usage(work_dir: Path, builder: TrajectoryBuilder) -> None:
        """Deduplicate Claude stream-json usage and retain thinking text.

        Claude Code emits one ``assistant`` event per content block.  Every
        event for the same response repeats that response's cumulative usage,
        while ALE upstream currently assigns it to every generated step.  Keep
        usage on the first event for each response ID and let the existing
        reconciliation step carry the remaining delta to the final cumulative
        result.  This is telemetry-only; the native transcript is untouched.
        """
        transcript = work_dir / "transcript.jsonl"
        if not transcript.is_file():
            return
        assistant_events: list[dict[str, Any]] = []
        for line in transcript.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("type") == "assistant":
                assistant_events.append(event)
        agent_steps = [
            step for step in builder.trajectory.steps if step.source == "agent"
        ]
        if len(assistant_events) != len(agent_steps):
            builder.trajectory.extra.setdefault("claude_code", {})[
                "usage_deduplication"
            ] = {
                "status": "skipped_event_step_count_mismatch",
                "assistant_events": len(assistant_events),
                "agent_steps": len(agent_steps),
            }
            return

        seen_response_ids: set[str] = set()
        duplicate_events = 0
        reasoning_blocks = 0
        for event, step in zip(assistant_events, agent_steps, strict=True):
            message = event.get("message") or {}
            response_id = str(message.get("id") or "")
            if response_id and response_id in seen_response_ids:
                step.metrics = StepMetrics()
                duplicate_events += 1
            elif response_id:
                seen_response_ids.add(response_id)
            reasoning = [
                str(block.get("thinking") or "")
                for block in (message.get("content") or [])
                if isinstance(block, dict) and block.get("type") == "thinking"
            ]
            if reasoning:
                step.reasoning = "\n".join(item for item in reasoning if item) or None
                reasoning_blocks += len(reasoning)

        result = builder.trajectory.extra.get("result") or {}
        usage = result.get("usage") or {}
        sums = {
            "input": sum((step.metrics.input_tokens or 0) for step in agent_steps if step.metrics),
            "output": sum((step.metrics.output_tokens or 0) for step in agent_steps if step.metrics),
            "cache_read": sum((step.metrics.cache_read_tokens or 0) for step in agent_steps if step.metrics),
            "cache_creation": sum((step.metrics.cache_creation_tokens or 0) for step in agent_steps if step.metrics),
        }
        reconciliation = [
            step
            for step in builder.trajectory.steps
            if step.extra.get("usage_reconciliation") is True
        ]
        if len(reconciliation) == 1:
            reconciliation[0].metrics = StepMetrics(
                input_tokens=max(int(usage.get("input_tokens") or 0) - sums["input"], 0),
                output_tokens=max(int(usage.get("output_tokens") or 0) - sums["output"], 0),
                cache_read_tokens=max(
                    int(usage.get("cache_read_input_tokens") or 0) - sums["cache_read"], 0
                ),
                cache_creation_tokens=max(
                    int(usage.get("cache_creation_input_tokens") or 0)
                    - sums["cache_creation"],
                    0,
                ),
                cost_usd=result.get("total_cost_usd"),
            )
        builder.trajectory.extra.setdefault("claude_code", {})[
            "usage_deduplication"
        ] = {
            "status": "applied",
            "unique_response_ids": len(seen_response_ids),
            "duplicate_content_events": duplicate_events,
            "reasoning_blocks_retained": reasoning_blocks,
        }
