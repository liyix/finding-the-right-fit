"""OpenHands Software Agent SDK 1.44.1 deployer for ALE's sandbox lifecycle."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, ClassVar

from ale_run.base_interface import (
    AgentRunResult,
    BaseAgentDeployer,
    ContentPart,
    Observation,
    ToolCall,
    ToolResult,
    TrajectoryBuilder,
)

from .config import StudyOpenHandsConfig


_POLL_SECONDS = 2.0
_TERM_GRACE_SECONDS = 5.0
_PATCH_ID = "openhands-1.44.1-openrouter-reasoning-roundtrip-v2"


def _package_versions(python: str) -> dict[str, str] | None:
    script = (
        "import json; from importlib.metadata import version; "
        "print(json.dumps({'sdk': version('openhands-sdk'), "
        "'tools': version('openhands-tools')}))"
    )
    try:
        result = subprocess.run(
            [python, "-c", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


class StudyOpenHandsDeployer(BaseAgentDeployer):
    """Run the study's SDK harness without substituting ALE's old CLI agent."""

    default_executor: ClassVar[str] = "sandbox"
    supported_executors: ClassVar[frozenset[str]] = frozenset({"sandbox"})
    hot_artifacts: ClassVar[tuple[str, ...]] = (
        "openhands-events.jsonl",
        "stderr.log",
    )

    @property
    def version(self) -> str:
        cfg: StudyOpenHandsConfig = self.config  # type: ignore[assignment]
        return cfg.sdk_version

    async def install(self) -> None:
        cfg: StudyOpenHandsConfig = self.config  # type: ignore[assignment]
        sandbox = self.executor.sandbox
        if not sandbox.is_linux:
            raise NotImplementedError("study OpenHands SDK deployer is Linux-only")

        work_dir = Path(self.executor.work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        # The pinned ALE Docker image ships ``/home/user/.cache`` as
        # root:root/0700 while the agent runs as uid 1000.  Keep the ephemeral
        # per-trial environment under ALE's guaranteed-writable work directory
        # instead.  Sandbox log gathering already excludes ``.venv`` trees.
        venv = work_dir / ".venv"
        python = venv / "bin" / "python"
        cache_root = work_dir / ".cache"
        install_env = os.environ.copy()
        install_env["UV_CACHE_DIR"] = str(cache_root / "uv")
        install_env["PIP_CACHE_DIR"] = str(cache_root / "pip")
        versions = await asyncio.to_thread(_package_versions, str(python))
        expected = {"sdk": cfg.sdk_version, "tools": cfg.tools_version}
        if versions != expected:
            venv.parent.mkdir(parents=True, exist_ok=True)
            uv = shutil.which("uv")
            if uv:
                if not python.is_file():
                    result = await asyncio.to_thread(
                        subprocess.run,
                        [uv, "venv", "--python", sys.executable, str(venv)],
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=300,
                        env=install_env,
                    )
                    if result.returncode != 0:
                        raise RuntimeError(
                            f"OpenHands venv creation failed: {(result.stderr or '')[-800:]}"
                        )
                install_argv = [
                    uv,
                    "pip",
                    "install",
                    "--python",
                    str(python),
                    f"openhands-sdk=={cfg.sdk_version}",
                    f"openhands-tools=={cfg.tools_version}",
                ]
            else:
                if not python.is_file():
                    result = await asyncio.to_thread(
                        subprocess.run,
                    [sys.executable, "-m", "venv", str(venv)],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=300,
                    env=install_env,
                    )
                    if result.returncode != 0:
                        raise RuntimeError(
                            f"OpenHands venv creation failed: {(result.stderr or '')[-800:]}"
                        )
                install_argv = [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    f"openhands-sdk=={cfg.sdk_version}",
                    f"openhands-tools=={cfg.tools_version}",
                ]
            result = await asyncio.to_thread(
                subprocess.run,
                install_argv,
                check=False,
                capture_output=True,
                text=True,
                timeout=900,
                env=install_env,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"OpenHands package install failed: {(result.stderr or '')[-1000:]}"
                )
            versions = await asyncio.to_thread(_package_versions, str(python))
        if versions != expected:
            raise RuntimeError(
                f"OpenHands package version mismatch: expected {expected}, got {versions}"
            )

        patcher = Path(__file__).with_name("reasoning_patch.py")
        if not patcher.is_file():
            raise RuntimeError("materialized OpenHands reasoning patch is missing")
        patch_record = work_dir / "openhands-reasoning-patch.json"
        result = await asyncio.to_thread(
            subprocess.run,
            [str(python), str(patcher), "--record", str(patch_record), "--self-test"],
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"OpenHands reasoning patch failed: {(result.stderr or result.stdout or '')[-1200:]}"
            )
        record = json.loads(patch_record.read_text(encoding="utf-8"))
        if (
            record.get("patch_id") != _PATCH_ID
            or record.get("self_test", {}).get("status") != "passed"
        ):
            raise RuntimeError("OpenHands reasoning patch record is not qualified")

        from ale_run.agents._bootstrap import ensure_cua_mcp_server

        await ensure_cua_mcp_server(sandbox)
        separator = "/"
        mcp_index = (
            f"{sandbox.mcp_server_dir.rstrip('/ ')}{separator}src{separator}index.js"
        )
        if not Path(mcp_index).is_file():
            raise RuntimeError(f"ALE CUA MCP server is missing: {mcp_index}")

        runner_config = cfg.safe_runner_config(
            mcp_command=sandbox.node,
            mcp_args=[mcp_index],
            cua_url=self.executor.cua_bridge_url(),
        )
        config_path = work_dir / "runner-config.json"
        config_path.write_text(
            json.dumps(runner_config, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        install_record = {
            "versions": versions,
            "python": str(python),
            "reasoning_patch_id": record["patch_id"],
            "reasoning_patch_status": record["status"],
            "reasoning_patch_self_test": record["self_test"],
            "mcp_policy": cfg.mcp_policy,
            "mcp_server": "cua",
            "mcp_command": sandbox.node,
            "mcp_script": mcp_index,
        }
        (work_dir / "resolved-install.json").write_text(
            json.dumps(install_record, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self._python = str(python)
        self._runner = str(Path(__file__).with_name("runner.py"))
        self._config_path = str(config_path)
        self._patch_record = str(patch_record)

    async def launch(self, prompt: str) -> AgentRunResult:
        cfg: StudyOpenHandsConfig = self.config  # type: ignore[assignment]
        work_dir = Path(self.executor.work_dir)
        prompt_path = work_dir / "prompt.txt"
        stdout_path = work_dir / "stdout.log"
        stderr_path = work_dir / "stderr.log"
        pid_path = work_dir / "openhands.pid"
        for path in (stdout_path, stderr_path, pid_path):
            path.unlink(missing_ok=True)
        prompt_path.write_text(prompt, encoding="utf-8")

        env = os.environ.copy()
        env.update(
            {
                str(key): str(value)
                for key, value in (self.executor.env or {}).items()
            }
        )
        api_key = cfg.api_key or env.get("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError("OpenHands SDK requires OPENROUTER_API_KEY")
        env["OPENROUTER_API_KEY"] = api_key
        env["LLM_API_KEY"] = api_key
        env["NO_COLOR"] = "1"
        argv = [
            self._python,
            self._runner,
            "--prompt-file",
            str(prompt_path),
            "--work-dir",
            str(work_dir),
            "--config",
            self._config_path,
            "--patch-record",
            self._patch_record,
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

        resolved = work_dir / "resolved-openhands.json"
        events = work_dir / "openhands-events.jsonl"
        status = (
            "completed"
            if process.returncode == 0 and resolved.is_file() and events.is_file()
            else "failed"
        )
        error = None if status == "completed" else _diagnose_failure(
            stderr_path, stdout_path, process.returncode, resolved, events
        )
        return AgentRunResult(
            status=status,
            pid=process.pid,
            exit_code=process.returncode,
            transcript_path=str(events),
            stderr_path=str(stderr_path),
            duration_s=time.monotonic() - started,
            error=error,
        )

    @classmethod
    def parse_artifacts(
        cls,
        *,
        work_dir: Path,
        config: StudyOpenHandsConfig,
        run_result: AgentRunResult,
        builder: TrajectoryBuilder,
    ) -> None:
        events_path = work_dir / "openhands-events.jsonl"
        if not events_path.is_file():
            builder.add_step(
                "system",
                message="openhands-sdk: native event stream missing",
                extra={"reason": "no_events"},
            )
            return

        counts: Counter[str] = Counter()
        malformed = 0
        for raw in events_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if not isinstance(event, dict):
                malformed += 1
                continue
            kind = str(event.get("kind") or event.get("type") or "unknown")
            counts[kind] += 1
            cls._consume_event(event, builder)

        metrics = _read_object(work_dir / "openhands-metrics.json")
        if metrics:
            prompt = int(metrics.get("prompt_tokens") or 0)
            cached = int(metrics.get("cache_read_tokens") or 0)
            builder.override_final_metrics(
                total_input_tokens=max(prompt - cached, 0),
                total_output_tokens=int(metrics.get("completion_tokens") or 0),
                total_cache_read_tokens=cached,
                total_cache_creation_tokens=int(metrics.get("cache_write_tokens") or 0),
                total_cost_usd=float(metrics.get("cost_usd") or 0.0),
            )

        resolved = _read_object(work_dir / "resolved-openhands.json")
        install = _read_object(work_dir / "resolved-install.json")
        builder.trajectory.extra["openhands_sdk"] = {
            "exit_code": run_result.exit_code,
            "native_events_path": str(events_path),
            "event_counts": dict(counts),
            "malformed_event_lines": malformed,
            "completion_logs_path": str(work_dir / "completions"),
            "metrics_path": str(work_dir / "openhands-metrics.json"),
            "resolved_config_path": str(work_dir / "resolved-openhands.json"),
            "model": config.model,
            "route": config.model_spec["route"],
            "reasoning_effort": config.reasoning_effort,
            "max_input_tokens": config.max_input_tokens,
            "max_output_tokens": config.max_output_tokens,
            "api_mode": config.api_mode,
            "max_iterations": config.max_iterations,
            "skills": [],
            "condenser": None,
            "benchmark_mcp": ["cua"],
            "resolved": resolved,
            "install": install,
        }

    @classmethod
    def _consume_event(cls, event: dict[str, Any], builder: TrajectoryBuilder) -> None:
        kind = str(event.get("kind") or event.get("type") or "")
        if kind == "MessageEvent":
            message = event.get("llm_message")
            if not isinstance(message, dict) or message.get("role") != "assistant":
                return
            text = cls._content_to_text(message.get("content"))
            if text:
                builder.add_step(
                    "agent",
                    message=text,
                    reasoning=message.get("reasoning_content"),
                    extra={"llm_response_id": event.get("llm_response_id")},
                )
            return
        if kind == "ActionEvent":
            call = (
                event.get("tool_call")
                if isinstance(event.get("tool_call"), dict)
                else {}
            )
            arguments = call.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {"raw": arguments}
            if not isinstance(arguments, dict):
                action = event.get("action")
                arguments = action if isinstance(action, dict) else {}
            reasoning = event.get("reasoning_content")
            if not isinstance(reasoning, str) or not reasoning:
                reasoning = cls._content_to_text(event.get("thought")) or None
            builder.add_step(
                "agent",
                reasoning=reasoning,
                tool_calls=[
                    ToolCall(
                        id=str(event.get("tool_call_id") or call.get("id") or ""),
                        name=str(
                            event.get("tool_name") or call.get("name") or "unknown"
                        ),
                        arguments=arguments,
                    )
                ],
                extra={
                    "llm_response_id": event.get("llm_response_id"),
                    "reasoning_details_present": (
                        event.get("reasoning_details") is not None
                    ),
                    "responses_reasoning_items": len(
                        event.get("responses_reasoning_items") or []
                    ),
                },
            )
            return
        if kind in {"ObservationEvent", "UserRejectObservation"}:
            observation = event.get("observation")
            observation = observation if isinstance(observation, dict) else {}
            text, screenshot = cls._observation_content(observation)
            parts = [ContentPart(type="text", text=text)] if text else []
            extra: dict[str, Any] = {
                "tool_name": event.get("tool_name"),
                "observation_kind": observation.get("kind"),
            }
            if screenshot:
                extra["_screenshot_b64"] = screenshot
            builder.add_step(
                "environment",
                observation=Observation(
                    results=[
                        ToolResult(
                            tool_call_id=str(event.get("tool_call_id") or ""),
                            content=parts,
                            is_error=bool(observation.get("is_error")),
                        )
                    ]
                ),
                extra=extra,
            )
            return
        if kind in {"AgentErrorEvent", "ConversationErrorEvent"}:
            builder.add_step(
                "system",
                message=str(event.get("error") or event.get("message") or kind),
                extra={"kind": kind},
            )
            return
        if kind in {"Condensation", "CondensationRequest"}:
            builder.add_step("system", message=f"<{kind}>", extra={"raw": event})

    @staticmethod
    def _content_to_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(item for item in parts if item)

    @classmethod
    def _observation_content(cls, observation: dict[str, Any]) -> tuple[str, str | None]:
        text: list[str] = []
        screenshot: str | None = None
        content = observation.get("content")
        if isinstance(content, str):
            text.append(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    text.append(block["text"])
                elif block.get("type") == "image" and screenshot is None:
                    for url in block.get("image_urls") or []:
                        parsed = _parse_data_url(url)
                        if parsed:
                            screenshot = parsed
                            break
        if not text and screenshot is None:
            text.append(json.dumps(observation, ensure_ascii=False))
        return "\n".join(text), screenshot


def _read_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _parse_data_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    marker = "base64,"
    index = value.find(marker)
    if index < 0 or "data:image/" not in value[:index]:
        return None
    payload = value[index + len(marker) :].strip()
    return payload or None


def _diagnose_failure(
    stderr: Path,
    stdout: Path,
    returncode: int | None,
    resolved: Path,
    events: Path,
) -> str:
    stderr_text = (
        stderr.read_text(encoding="utf-8", errors="replace")
        if stderr.is_file()
        else ""
    )
    stdout_text = (
        stdout.read_text(encoding="utf-8", errors="replace")
        if stdout.is_file()
        else ""
    )
    return (
        f"OpenHands SDK failed (rc={returncode}); resolved={resolved.is_file()} "
        f"events={events.is_file()}; stderr tail={stderr_text[-700:]}; "
        f"stdout tail={stdout_text[-300:]}"
    )
