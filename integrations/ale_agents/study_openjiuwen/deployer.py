"""openJiuwen Coding Agent 0.1.18 deployer for ALE's sandbox lifecycle."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import subprocess
import time
from collections import Counter
from pathlib import Path
from typing import Any, ClassVar

from ale_run.base_interface import (
    AgentRunResult,
    BaseAgentDeployer,
    ContentPart,
    ImageSource,
    Observation,
    StepMetrics,
    ToolCall,
    ToolResult,
    TrajectoryBuilder,
)

from .config import StudyOpenJiuwenConfig


_RUNTIME_ROOT = Path("/opt/openjiuwen-runtime")
_RUNTIME_PYTHON = _RUNTIME_ROOT / "python" / "bin" / "python3.12"
_RUNTIME_SITE_PACKAGES = _RUNTIME_ROOT / "site-packages"
_RUNTIME_BIN = _RUNTIME_ROOT / "bin"
_EXPECTED_RUNTIME = {
    "format": 1,
    "openjiuwen_version": "0.1.18",
    "openjiuwen_tag_commit": "1d37ae3007f9df9a8489a7ab271141b03be08f66",
    "python_version": "3.12.12",
    "python_sha256": "3008abbb48c1e2b0fa3072ae72334b9b352e1346ea3a20d34126a455f0c7bc66",
    "ripgrep_sha256": "e62198eb19b136b88c330af83647b5a962cb99b6b1f066758568f12de1974849",
    "dependency_lock_sha256": "0cf73ec722edae0c242a58d377dcdcd30185bd465bb8ad284efbe10a106d1d7a",
    "openjiuwen_record_sha256": "8c592177f2e54d49f5a7b3104b80b09e4fc6a8fcfaa0087673078b815426176c",
}
_RUNNER_SHA256 = "1895a50f963c5275259ade7dbb09b57e44d033c1cd041a7bcfc5d52789dc0339"
_LOCK_SHA256 = "0cf73ec722edae0c242a58d377dcdcd30185bd465bb8ad284efbe10a106d1d7a"
_POLL_SECONDS = 2.0
_TERM_GRACE_SECONDS = 5.0
# ALE prompts refer to task inputs and outputs by absolute sandbox paths under
# /media/user/data/agenthle.  Keep openJiuwen's filesystem guard enabled, but
# make the outer ALE container (rather than its agent scratch directory) the
# security boundary so those benchmark-owned paths remain reachable.
_ALE_WORKSPACE_ROOT = "/"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


class StudyOpenJiuwenDeployer(BaseAgentDeployer):
    """Run the same Rails composition as the TB4 candidate inside ALE."""

    default_executor: ClassVar[str] = "sandbox"
    supported_executors: ClassVar[frozenset[str]] = frozenset({"sandbox"})
    hot_artifacts: ClassVar[tuple[str, ...]] = (
        "native-events.jsonl",
        "wire-generations.jsonl",
        "stderr.log",
    )

    @property
    def version(self) -> str:
        cfg: StudyOpenJiuwenConfig = self.config  # type: ignore[assignment]
        return cfg.version

    def _runtime_env(self) -> dict[str, str]:
        env = os.environ.copy()
        current_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(_RUNTIME_SITE_PACKAGES) + (
            os.pathsep + current_pythonpath if current_pythonpath else ""
        )
        env["PATH"] = os.pathsep.join(
            [str(_RUNTIME_BIN), str(_RUNTIME_PYTHON.parent), env.get("PATH", "")]
        )
        env["NO_COLOR"] = "1"
        return env

    async def install(self) -> None:
        cfg: StudyOpenJiuwenConfig = self.config  # type: ignore[assignment]
        sandbox = self.executor.sandbox
        if not sandbox.is_linux:
            raise NotImplementedError("study openJiuwen deployer is Linux-only")

        metadata = _read_object(_RUNTIME_ROOT / "runtime.json")
        if metadata != _EXPECTED_RUNTIME:
            raise RuntimeError(
                "mounted openJiuwen runtime metadata mismatch; prepare the frozen "
                "0.1.18-r1 runtime before launching ALE"
            )
        if not _RUNTIME_PYTHON.is_file() or _sha256(_RUNTIME_PYTHON) != _EXPECTED_RUNTIME["python_sha256"]:
            raise RuntimeError("mounted openJiuwen Python hash mismatch")

        package_dir = Path(__file__).resolve().parent
        runner = package_dir / "openjiuwen_agent.py"
        lock = package_dir / "openjiuwen-runtime.lock"
        if _sha256(runner) != _RUNNER_SHA256:
            raise RuntimeError("materialized openJiuwen runner hash mismatch")
        if _sha256(lock) != _LOCK_SHA256:
            raise RuntimeError("materialized openJiuwen dependency lock hash mismatch")

        work_dir = Path(self.executor.work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        self_test_record = work_dir / "openjiuwen-self-test.json"
        result = await asyncio.to_thread(
            subprocess.run,
            [
                str(_RUNTIME_PYTHON), str(runner), "--self-test",
                "--self-test-output", str(self_test_record),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
            env=self._runtime_env(),
            cwd=str(work_dir),
        )
        self_test = _read_object(self_test_record)
        if result.returncode != 0 or not self_test or self_test.get("status") != "passed":
            raise RuntimeError(
                "openJiuwen offline self-test failed: "
                + (result.stderr or result.stdout or "")[-1200:]
            )

        from ale_run.agents._bootstrap import ensure_cua_mcp_server

        await ensure_cua_mcp_server(sandbox)
        mcp_script = f"{sandbox.mcp_server_dir.rstrip('/ ')}/src/index.js"
        if not Path(mcp_script).is_file():
            raise RuntimeError(f"ALE CUA MCP server is missing: {mcp_script}")

        raw_budget = os.environ.get("ALE_EPISODE_TIMEOUT_SECONDS", "")
        try:
            runtime_budget = int(float(raw_budget))
        except ValueError as exc:
            raise RuntimeError(
                "ALE did not export its exact episode timeout to openJiuwen"
            ) from exc
        runner_config = cfg.safe_runner_config(
            runtime_budget_seconds=runtime_budget,
            mcp_command=sandbox.node,
            mcp_script=mcp_script,
            cua_url=self.executor.cua_bridge_url(),
            work_dir=str(work_dir),
        )
        config_path = work_dir / "openjiuwen-config.json"
        config_path.write_text(
            json.dumps(runner_config, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (work_dir / "resolved-install.json").write_text(
            json.dumps({
                "openjiuwen_version": cfg.version,
                "runtime_version": cfg.runtime_version,
                "runtime_metadata": metadata,
                "runner_sha256": _RUNNER_SHA256,
                "dependency_lock_sha256": _LOCK_SHA256,
                "self_test": self_test,
                "runtime_budget_seconds": runtime_budget,
                "runtime_budget_rail_enabled": cfg.runtime_budget_rail_enabled,
                "runtime_budget_source": "ALE_EPISODE_TIMEOUT_SECONDS",
                "mcp_policy": cfg.mcp_policy,
                "mcp_server": "cua",
                "mcp_command": sandbox.node,
                "mcp_script": mcp_script,
                "workspace_root": _ALE_WORKSPACE_ROOT,
            }, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self._python = str(_RUNTIME_PYTHON)
        self._runner = str(runner)
        self._config_path = str(config_path)

    async def launch(self, prompt: str) -> AgentRunResult:
        cfg: StudyOpenJiuwenConfig = self.config  # type: ignore[assignment]
        work_dir = Path(self.executor.work_dir)
        prompt_path = work_dir / "prompt.txt"
        stdout_path = work_dir / "stdout.log"
        stderr_path = work_dir / "stderr.log"
        pid_path = work_dir / "openjiuwen.pid"
        for path in (stdout_path, stderr_path, pid_path):
            path.unlink(missing_ok=True)
        prompt_path.write_text(prompt, encoding="utf-8")

        env = self._runtime_env()
        env.update({str(key): str(value) for key, value in (self.executor.env or {}).items()})
        api_key = cfg.api_key or env.get("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError("openJiuwen requires OPENROUTER_API_KEY")
        env["OPENROUTER_API_KEY"] = api_key
        argv = [
            self._python,
            self._runner,
            "--config", self._config_path,
            "--instruction-file", str(prompt_path),
            "--workspace", _ALE_WORKSPACE_ROOT,
            "--log-dir", str(work_dir),
        ]
        started = time.monotonic()
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = await asyncio.to_thread(
                subprocess.Popen,
                argv,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                env=env,
                cwd=str(work_dir),
                start_new_session=True,
            )
        pid_path.write_text(str(process.pid), encoding="ascii")
        try:
            while process.poll() is None:
                await asyncio.sleep(_POLL_SECONDS)
        except asyncio.CancelledError:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(process.wait), timeout=_TERM_GRACE_SECONDS
                )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            raise

        required = [
            work_dir / "native-events.jsonl",
            work_dir / "native-transcript.json",
            work_dir / "result.json",
        ]
        status = (
            "completed"
            if process.returncode == 0 and all(path.is_file() for path in required)
            else "failed"
        )
        error = None
        if status != "completed":
            stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.is_file() else ""
            error = (
                f"openJiuwen failed (rc={process.returncode}); "
                f"artifacts={[path.is_file() for path in required]}; "
                f"stderr tail={stderr_text[-800:]}"
            )
        return AgentRunResult(
            status=status,
            pid=process.pid,
            exit_code=process.returncode,
            transcript_path=str(work_dir / "native-transcript.json"),
            stderr_path=str(stderr_path),
            duration_s=time.monotonic() - started,
            error=error,
        )

    @classmethod
    def parse_artifacts(
        cls,
        *,
        work_dir: Path,
        config: StudyOpenJiuwenConfig,
        run_result: AgentRunResult,
        builder: TrajectoryBuilder,
    ) -> None:
        transcript_path = work_dir / "native-transcript.json"
        try:
            transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            builder.add_step(
                "system",
                message="openjiuwen: native transcript missing or malformed",
                extra={"reason": "no_transcript"},
            )
            return
        if not isinstance(transcript, list):
            builder.add_step(
                "system", message="openjiuwen: native transcript is not a list"
            )
            return

        role_counts: Counter[str] = Counter()
        totals = Counter()
        total_cost = 0.0
        for message in transcript:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "unknown")
            role_counts[role] += 1
            if role == "assistant":
                usage = message.get("usage_metadata")
                usage = usage if isinstance(usage, dict) else {}
                input_tokens = _as_int(usage.get("input_tokens"))
                output_tokens = _as_int(usage.get("output_tokens"))
                cache_read = _as_int(usage.get("cache_read_tokens"))
                cache_write = _as_int(
                    usage.get("cache_creation_input_tokens")
                    if usage.get("cache_creation_input_tokens") is not None
                    else usage.get("cache_write_tokens")
                )
                reasoning_tokens = _as_int(usage.get("reasoning_tokens"))
                cost = _as_float(usage.get("total_cost"))
                totals.update({
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_read_tokens": cache_read,
                    "cache_write_tokens": cache_write,
                    "reasoning_tokens": reasoning_tokens,
                })
                total_cost += cost
                calls = []
                for call in message.get("tool_calls") or []:
                    if not isinstance(call, dict):
                        continue
                    function = call.get("function")
                    function = function if isinstance(function, dict) else {}
                    arguments = function.get("arguments")
                    if isinstance(arguments, str):
                        try:
                            arguments = json.loads(arguments)
                        except json.JSONDecodeError:
                            arguments = {"_raw": arguments}
                    if not isinstance(arguments, dict):
                        arguments = {"_value": arguments}
                    calls.append(ToolCall(
                        id=str(call.get("id") or ""),
                        name=str(function.get("name") or "unknown"),
                        arguments=arguments,
                    ))
                metadata = message.get("metadata")
                metadata = metadata if isinstance(metadata, dict) else {}
                details = metadata.get("_openrouter_reasoning_details")
                details = details if isinstance(details, list) else []
                builder.add_step(
                    "agent",
                    message=cls._message_text(message.get("content")) or None,
                    reasoning=str(message.get("reasoning_content") or "") or None,
                    tool_calls=calls,
                    metrics=StepMetrics(
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cache_read_tokens=cache_read,
                        cache_creation_tokens=cache_write,
                        cost_usd=cost,
                    ),
                    extra={
                        "finish_reason": message.get("finish_reason"),
                        "response_id": (metadata.get("_study_wire_response") or {}).get("id")
                        if isinstance(metadata.get("_study_wire_response"), dict) else None,
                        "response_model": (metadata.get("_study_wire_response") or {}).get("model")
                        if isinstance(metadata.get("_study_wire_response"), dict) else None,
                        "reasoning_tokens": reasoning_tokens,
                        "reasoning_details_count": len(details),
                        "reasoning_details_sha256": hashlib.sha256(
                            json.dumps(details, sort_keys=True).encode("utf-8")
                        ).hexdigest() if details else None,
                    },
                )
            elif role == "tool":
                builder.add_step(
                    "environment",
                    observation=Observation(results=[ToolResult(
                        tool_call_id=str(message.get("tool_call_id") or ""),
                        content=cls._content_parts(message.get("content")),
                        is_error=bool(message.get("is_error")),
                    )]),
                    extra={"tool_name": message.get("name")},
                )

        events = work_dir / "native-events.jsonl"
        event_counts: Counter[str] = Counter()
        malformed_events = 0
        if events.is_file():
            with events.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        malformed_events += 1
                        continue
                    if isinstance(event, dict):
                        event_counts[str(event.get("event") or "unknown")] += 1

        builder.override_final_metrics(
            total_input_tokens=totals["input_tokens"],
            total_output_tokens=totals["output_tokens"],
            total_cache_read_tokens=totals["cache_read_tokens"],
            total_cache_creation_tokens=totals["cache_write_tokens"],
            total_cost_usd=total_cost,
        )
        install = _read_object(work_dir / "resolved-install.json")
        builder.trajectory.extra["openjiuwen"] = {
            "exit_code": run_result.exit_code,
            "native_transcript_path": str(transcript_path),
            "native_events_path": str(events),
            "wire_generations_path": str(work_dir / "wire-generations.jsonl"),
            "result_path": str(work_dir / "result.json"),
            "role_counts": dict(role_counts),
            "event_counts": dict(event_counts),
            "malformed_event_lines": malformed_events,
            "model": config.model,
            "route": config.model_spec["route"],
            "reasoning_effort": config.reasoning_effort,
            "context_window": config.model_spec["context"],
            "max_tokens_wire": None,
            "temperature_wire": None,
            "top_p_wire": None,
            "seed_wire": None,
            "max_outer_rounds": config.max_outer_rounds,
            "skills": [],
            "memory": None,
            "subagents": [],
            "context_compression": None,
            "benchmark_mcp": ["cua"],
            "reasoning_tokens": totals["reasoning_tokens"],
            "native_usage_cost_usd": total_cost,
            "native_usage_is_secondary_to_provider_ledger": True,
            "install": install,
        }

    @staticmethod
    def _message_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)

    @staticmethod
    def _content_parts(content: Any) -> list[ContentPart]:
        if isinstance(content, str):
            return [ContentPart(type="text", text=content)]
        if not isinstance(content, list):
            return [ContentPart(type="text", text=json.dumps(content, default=str))]
        parts: list[ContentPart] = []
        for item in content:
            if isinstance(item, str):
                parts.append(ContentPart(type="text", text=item))
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(ContentPart(type="text", text=str(item.get("text") or "")))
            elif isinstance(item, dict) and item.get("type") == "image":
                data = item.get("data")
                if isinstance(data, str) and data:
                    parts.append(ContentPart(
                        type="image",
                        image=ImageSource(
                            type="base64", data=data,
                            media_type=str(item.get("mimeType") or "image/png"),
                        ),
                    ))
            else:
                parts.append(ContentPart(type="text", text=json.dumps(item, default=str)))
        return parts
