"""PI 0.84.4 deployer for ALE's in-sandbox lifecycle."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
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

from .config import StudyPiConfig


_PACKAGE = "@earendil-works/pi-coding-agent"
_POLL_SECONDS = 2.0
_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")
_CUA_TOOLS = (
    "key", "key_down", "key_up", "type", "hold_key", "mouse_move",
    "click", "drag", "mouse_down", "mouse_up", "scroll", "wait",
    "screenshot", "cursor_position",
)


def _version(binary: str) -> str | None:
    try:
        result = subprocess.run(
            [binary, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = _VERSION_RE.search((result.stdout or "") + (result.stderr or ""))
    return match.group(1) if match else None


class StudyPiDeployer(BaseAgentDeployer):
    default_executor: ClassVar[str] = "sandbox"
    supported_executors: ClassVar[frozenset[str]] = frozenset({"sandbox"})
    hot_artifacts: ClassVar[tuple[str, ...]] = ("transcript.jsonl", "stderr.log")

    @property
    def version(self) -> str:
        cfg: StudyPiConfig = self.config  # type: ignore[assignment]
        return cfg.cli_version

    async def install(self) -> None:
        cfg: StudyPiConfig = self.config  # type: ignore[assignment]
        npm = shutil.which("npm") or shutil.which("npm.cmd")
        if not npm:
            from ale_run.agents._bootstrap import ensure_npm

            npm = await ensure_npm()
        home = Path.home()
        prefix = home / ".local"
        expected_binary = prefix / "bin" / "pi"
        binary = str(expected_binary) if expected_binary.is_file() else shutil.which("pi")
        installed = await asyncio.to_thread(_version, binary) if binary else None
        if installed != cfg.cli_version:
            env = {**os.environ, "npm_config_cache": str(home / ".npm-ale")}
            result = await asyncio.to_thread(
                subprocess.run,
                [
                    npm,
                    "install",
                    "-g",
                    "--ignore-scripts",
                    "--force",
                    "--prefix",
                    str(prefix),
                    f"{_PACKAGE}@{cfg.cli_version}",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=900,
                env=env,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"PI install failed (rc={result.returncode}): {(result.stderr or '')[-500:]}"
                )
            binary = str(expected_binary)
            installed = await asyncio.to_thread(_version, binary)
        if not binary or installed != cfg.cli_version:
            raise RuntimeError(
                f"PI version mismatch: expected {cfg.cli_version}, got {installed}"
            )
        self._pi_path = binary

        work_dir = Path(self.executor.work_dir)
        config_dir = work_dir / "pi-config"
        session_dir = work_dir / "sessions"
        config_dir.mkdir(parents=True, exist_ok=True)
        session_dir.mkdir(parents=True, exist_ok=True)
        models_path = config_dir / "models.json"
        models_path.write_text(
            json.dumps(cfg.models_json(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        models_path.chmod(0o600)
        self._config_dir = config_dir
        self._session_dir = session_dir
        extension = Path(__file__).with_name("cua-extension.mjs")
        if not extension.is_file():
            raise RuntimeError(f"ALE PI CUA extension missing: {extension}")
        self._cua_extension = extension

    async def launch(self, prompt: str) -> AgentRunResult:
        cfg: StudyPiConfig = self.config  # type: ignore[assignment]
        work_dir = Path(self.executor.work_dir)
        transcript = work_dir / "transcript.jsonl"
        stderr = work_dir / "stderr.log"
        pid_file = work_dir / "pi.pid"
        for path in (transcript, stderr, pid_file):
            path.unlink(missing_ok=True)

        env = os.environ.copy()
        env.update({str(k): str(v) for k, v in (self.executor.env or {}).items()})
        token = cfg.api_key or env.get("OPENROUTER_API_KEY")
        if not token:
            raise RuntimeError("PI requires OPENROUTER_API_KEY")
        env.update({
            "OPENROUTER_API_KEY": token,
            "PI_CODING_AGENT_DIR": str(self._config_dir),
            "PI_OFFLINE": "1",
            "PI_SKIP_VERSION_CHECK": "1",
            "PI_TELEMETRY": "0",
            "CUA_SERVER_URL": self.executor.cua_bridge_url(),
        })
        argv = [
            self._pi_path,
            "--print",
            "--mode", "json",
            "--session-dir", str(self._session_dir),
            "--provider", cfg.provider,
            "--model", cfg.model,
            "--thinking", cfg.thinking,
            "--offline",
            "--no-extensions",
            "--extension", str(self._cua_extension),
            prompt,
        ]
        started = time.monotonic()
        with transcript.open("wb") as output, stderr.open("wb") as error_output:
            process = await asyncio.to_thread(
                subprocess.Popen,
                argv,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=error_output,
                env=env,
                cwd=str(work_dir),
                start_new_session=True if hasattr(os, "setsid") else False,
            )
        pid_file.write_text(str(process.pid), encoding="ascii")
        try:
            while process.poll() is None:
                await asyncio.sleep(_POLL_SECONDS)
        except asyncio.CancelledError:
            process.terminate()
            try:
                await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                process.kill()
            raise
        status = "completed" if process.returncode == 0 else "failed"
        return AgentRunResult(
            status=status,
            pid=process.pid,
            exit_code=process.returncode,
            transcript_path=str(transcript),
            stderr_path=str(stderr),
            duration_s=time.monotonic() - started,
            error=None if status == "completed" else cls_failure(stderr, transcript, process.returncode),
        )

    @classmethod
    def parse_artifacts(
        cls,
        *,
        work_dir: Path,
        config: StudyPiConfig,
        run_result: AgentRunResult,
        builder: TrajectoryBuilder,
    ) -> None:
        transcript = work_dir / "transcript.jsonl"
        if not transcript.is_file():
            builder.add_step(
                "system", message="pi: transcript missing", extra={"reason": "no_transcript"}
            )
            return
        event_counts: Counter[str] = Counter()
        for raw in transcript.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            event_type = str(event.get("type"))
            event_counts[event_type] += 1
            if event_type != "message_end":
                continue
            message = event.get("message") or {}
            role = message.get("role")
            content = message.get("content") or []
            if role == "assistant":
                cls._assistant(message, content, builder)
            elif role == "toolResult":
                cls._tool_result(message, content, builder)
        native_sessions = sorted((work_dir / "sessions").glob("*.jsonl"))
        builder.trajectory.extra["pi"] = {
            "exit_code": run_result.exit_code,
            "transcript_path": str(transcript),
            "native_session_paths": [str(path) for path in native_sessions],
            "event_counts": dict(event_counts),
            "provider": config.provider,
            "model": config.model,
            "thinking": config.thinking,
            "route": config.model_spec["route"],
            "catalog_context_tokens": config.model_spec["context"],
            "catalog_max_output_tokens": config.model_spec["max_output"],
            "ale_cua_extension": {
                "path": str(Path(__file__).with_name("cua-extension.mjs")),
                "sha256": hashlib.sha256(
                    Path(__file__).with_name("cua-extension.mjs").read_bytes()
                ).hexdigest(),
                "tools": list(_CUA_TOOLS),
                "backend": "ALE CUA HTTP /cmd",
            },
        }

    @staticmethod
    def _assistant(
        message: dict[str, Any], content: list[Any], builder: TrajectoryBuilder
    ) -> None:
        text: list[str] = []
        reasoning: list[str] = []
        calls: list[ToolCall] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text.append(str(block.get("text") or ""))
            elif block.get("type") == "thinking":
                reasoning.append(str(block.get("thinking") or ""))
            elif block.get("type") == "toolCall":
                calls.append(ToolCall(
                    id=str(block.get("id") or ""),
                    name=str(block.get("name") or "unknown"),
                    arguments=block.get("arguments") if isinstance(block.get("arguments"), dict) else {},
                ))
        usage = message.get("usage") or {}
        cost = usage.get("cost") or {}
        builder.add_step(
            "agent",
            message="\n".join(item for item in text if item) or None,
            reasoning="\n".join(item for item in reasoning if item) or None,
            tool_calls=calls,
            metrics=StepMetrics(
                input_tokens=int(usage.get("input") or 0),
                output_tokens=int(usage.get("output") or 0),
                cache_read_tokens=int(usage.get("cacheRead") or 0),
                cache_creation_tokens=int(usage.get("cacheWrite") or 0),
                cost_usd=float(cost.get("total") or 0.0),
            ),
            extra={
                "api": message.get("api"),
                "provider": message.get("provider"),
                "model": message.get("model"),
                "stop_reason": message.get("stopReason"),
                "raw_stop_reason": message.get("rawStopReason"),
                "response_id": message.get("responseId"),
                "reasoning_tokens": usage.get("reasoning"),
            },
        )

    @staticmethod
    def _tool_result(
        message: dict[str, Any], content: list[Any], builder: TrajectoryBuilder
    ) -> None:
        results: list[ToolResult] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            raw_content = block.get("content")
            parts: list[ContentPart] = []
            if isinstance(raw_content, str):
                parts.append(ContentPart(type="text", text=raw_content))
            elif isinstance(raw_content, list):
                for item in raw_content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        parts.append(ContentPart(type="text", text=str(item.get("text") or "")))
                    elif isinstance(item, dict) and item.get("type") == "image":
                        data = item.get("data")
                        if isinstance(data, str) and data:
                            parts.append(ContentPart(
                                type="image",
                                image=ImageSource(
                                    type="base64",
                                    data=data,
                                    media_type=str(item.get("mimeType") or "image/png"),
                                ),
                            ))
            results.append(ToolResult(
                tool_call_id=str(block.get("toolCallId") or message.get("toolCallId") or ""),
                content=parts,
                is_error=bool(block.get("isError") or message.get("isError")),
            ))
        builder.add_step("environment", observation=Observation(results=results))


def cls_failure(stderr: Path, transcript: Path, returncode: int | None) -> str:
    err = stderr.read_text(encoding="utf-8", errors="replace") if stderr.is_file() else ""
    raw = transcript.read_text(encoding="utf-8", errors="replace") if transcript.is_file() else ""
    return (
        f"PI failed (rc={returncode}); stderr={len(err)}B transcript={len(raw)}B; "
        f"stderr tail={err[-600:]}"
    )
