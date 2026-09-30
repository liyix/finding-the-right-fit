"""DSH rc.2 ``standard`` deployer for ALE's in-sandbox lifecycle."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import time
import urllib.request
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

from .config import StudyDeepSeekConfig


_PACKAGE = "@deepseek-ai/dsh"
_PATCH_SHA256 = "b072cb7582dcb1c6baa948f4a292d748eed75f349e878842c50791908a801622"
_INJECTOR_SHA256 = "617726fc07c07f3098e0222110c29b2ab4da3f3b361a5a04603e81dfa11fc49c"
_POLL_SECONDS = 2.0
_TERM_GRACE_SECONDS = 5.0


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _version(binary: str) -> str | None:
    try:
        result = subprocess.run(
            [binary, "--version"], check=False, capture_output=True, text=True,
            timeout=60, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"\b(\d+\.\d+\.\d+-rc\.\d+)\b", result.stdout + result.stderr)
    return match.group(1) if match else None


def _json_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"_raw": value}
        return parsed if isinstance(parsed, dict) else {"_value": parsed}
    return {}


def _tool_content(value: Any) -> list[ContentPart]:
    items = value if isinstance(value, list) else [value]
    parts: list[ContentPart] = []
    for item in items:
        if isinstance(item, str):
            parts.append(ContentPart(type="text", text=item))
            continue
        if not isinstance(item, dict):
            parts.append(ContentPart(type="text", text=json.dumps(item)))
            continue
        item_type = item.get("type")
        if item_type == "text":
            parts.append(ContentPart(type="text", text=str(item.get("text") or "")))
            continue
        if item_type == "image":
            source = item.get("source") if isinstance(item.get("source"), dict) else {}
            data = item.get("data") or source.get("data")
            url = item.get("url") or source.get("url")
            path = item.get("path") or source.get("path")
            media_type = str(
                item.get("mimeType") or item.get("media_type")
                or source.get("media_type") or "image/png"
            )
            if isinstance(data, str) and data:
                image = ImageSource(type="base64", data=data, media_type=media_type)
            elif isinstance(url, str) and url:
                image = ImageSource(type="url", url=url, media_type=media_type)
            elif isinstance(path, str) and path:
                image = ImageSource(type="path", path=path, media_type=media_type)
            else:
                parts.append(ContentPart(
                    type="text",
                    text="[DSH image reference] " + json.dumps(item, sort_keys=True),
                ))
                continue
            parts.append(ContentPart(type="image", image=image))
            continue
        # Preserve structured MCP diagnostics/resources rather than silently
        # dropping content that ATIF does not model directly.
        parts.append(ContentPart(type="text", text=json.dumps(item, sort_keys=True)))
    return parts


def build_dsh_patch(
    cfg: StudyDeepSeekConfig,
    *,
    sessions_root: Path,
    mcp_command: str,
    mcp_script: str,
    mcp_env: dict[str, str],
    mcp_cwd: str,
) -> list[dict[str, Any]]:
    """Build the runner overlay; the upstream ``standard`` preset is unchanged."""
    spec = cfg.model_spec
    provider: dict[str, Any] = {
        "displayName": "ALE OpenRouter",
        "apiKeyEnv": "OPENROUTER_API_KEY",
        "api": "openai-completions",
        "baseURL": cfg.base_url,
        "reasoning": cfg.reasoning_effort,
        "models": [{
            "id": cfg.model,
            "contextWindow": spec["context"],
            "maxTokens": spec["max_output"],
            "input": list(spec["input"]),
            "reasoningEfforts": {"off": None, "high": "high"},
            "compat": dict(spec["compat"]),
        }],
    }
    if spec.get("cache_retention") is not None:
        provider["cacheRetention"] = spec["cache_retention"]
    return [
        {
            "insert": [{
                "id": "agent-presets",
                "name": "@deepseek-ai/dsh-agent-presets",
                "config": {"default": "standard", "includeUserRoot": False},
            }]
        },
        {
            "id": "llm-pi-ai",
            "config": {"providers": {"ale-openrouter": provider}},
        },
        {
            "id": "agent-default-model",
            "config": {"provider": "ale-openrouter", "model": cfg.model},
        },
        {
            "insert": [{
                "id": "ale-cua-mcp",
                "name": "@deepseek-ai/dsh-mcp-client",
                "config": {
                    "serverName": "cua",
                    "transport": "stdio",
                    "command": mcp_command,
                    "args": [mcp_script],
                    "env": dict(mcp_env),
                    "cwd": mcp_cwd,
                    "failOnStartupError": True,
                },
            }]
        },
        {
            "id": "session-persistence-jsonl",
            "config": {
                "root": str(sessions_root),
                "compression": "none",
                "packChunks": False,
            },
        },
    ]


class StudyDeepSeekDeployer(BaseAgentDeployer):
    """Pinned DSH with strict OpenRouter routing and ALE's CUA bridge."""

    default_executor: ClassVar[str] = "sandbox"
    supported_executors: ClassVar[frozenset[str]] = frozenset({"sandbox"})
    hot_artifacts: ClassVar[tuple[str, ...]] = (
        "dsh.log", "stderr.log", "openrouter-injector.jsonl",
        "openrouter-injector.stderr.log",
    )

    @property
    def version(self) -> str:
        cfg: StudyDeepSeekConfig = self.config  # type: ignore[assignment]
        return cfg.cli_version

    async def install(self) -> None:
        cfg: StudyDeepSeekConfig = self.config  # type: ignore[assignment]
        sandbox = self.executor.sandbox
        if not sandbox.is_linux:
            raise NotImplementedError("study DSH deployer is qualified only on ALE Linux")

        package_dir = Path(__file__).resolve().parent
        patch = package_dir / "dsh-headless-standard.patch"
        injector = package_dir / "openrouter-body-injector.mjs"
        if _sha256(patch) != _PATCH_SHA256:
            raise RuntimeError("materialized DSH standard patch hash mismatch")
        if _sha256(injector) != _INJECTOR_SHA256:
            raise RuntimeError("materialized OpenRouter injector hash mismatch")

        from ale_run.agents._bootstrap import (
            cua_bridge_env,
            ensure_cua_mcp_server,
            ensure_npm,
        )

        npm = await ensure_npm()
        git = shutil.which("git")
        if git is None:
            apt = ["apt-get"] if os.geteuid() == 0 else ["sudo", "apt-get"]
            for args in (["update", "-qq"], ["install", "-y", "-qq", "git"]):
                result = await asyncio.to_thread(
                    subprocess.run, apt + args, check=False, capture_output=True,
                    text=True, timeout=300,
                )
                if result.returncode != 0:
                    raise RuntimeError(
                        "DSH setup could not install git: "
                        + (result.stderr or result.stdout or "")[-800:]
                    )
            git = shutil.which("git")
        if git is None:
            raise RuntimeError("git is missing after DSH setup")
        home = Path.home()
        expected_binary = home / ".npm-global" / "bin" / "dsh"
        binary = str(expected_binary) if expected_binary.is_file() else shutil.which("dsh")
        installed = await asyncio.to_thread(_version, binary) if binary else None
        if installed != cfg.cli_version:
            result = await asyncio.to_thread(
                subprocess.run,
                [npm, "install", "-g", "--force", f"{_PACKAGE}@{cfg.cli_version}"],
                check=False, capture_output=True, text=True, timeout=1200,
                env={**os.environ, "npm_config_cache": str(home / ".npm-ale")},
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"DSH install failed (rc={result.returncode}): "
                    f"{(result.stderr or result.stdout or '')[-1000:]}"
                )
            binary = str(expected_binary) if expected_binary.is_file() else shutil.which("dsh")
            installed = await asyncio.to_thread(_version, binary) if binary else None
        if not binary or installed != cfg.cli_version:
            raise RuntimeError(
                f"DSH version mismatch: expected {cfg.cli_version}, got {installed}"
            )

        npm_root_result = await asyncio.to_thread(
            subprocess.run, [npm, "root", "-g"], check=False,
            capture_output=True, text=True, timeout=60,
        )
        if npm_root_result.returncode != 0:
            raise RuntimeError("could not resolve DSH npm root")
        npm_root = npm_root_result.stdout.strip()
        check_argv = [
            git, "apply", "--check", "--unsafe-paths",
            f"--directory={npm_root}", str(patch),
        ]
        check = await asyncio.to_thread(
            subprocess.run, check_argv, check=False, capture_output=True, text=True,
            timeout=60,
        )
        if check.returncode == 0:
            apply = await asyncio.to_thread(
                subprocess.run,
                [item for item in check_argv if item != "--check"],
                check=False, capture_output=True, text=True, timeout=60,
            )
            if apply.returncode != 0:
                raise RuntimeError(f"DSH standard patch failed: {apply.stderr[-800:]}")
        else:
            reverse = await asyncio.to_thread(
                subprocess.run,
                [
                    git, "apply", "--reverse", "--check", "--unsafe-paths",
                    f"--directory={npm_root}", str(patch),
                ],
                check=False, capture_output=True, text=True, timeout=60,
            )
            if reverse.returncode != 0:
                raise RuntimeError(
                    "DSH standard patch is neither applicable nor already applied: "
                    + (check.stderr or reverse.stderr)[-800:]
                )

        await ensure_cua_mcp_server(sandbox)
        mcp_script = f"{sandbox.mcp_server_dir.rstrip('/ ')}/src/index.js"
        if not Path(mcp_script).is_file():
            raise RuntimeError(f"ALE CUA MCP server is missing: {mcp_script}")

        work_dir = Path(self.executor.work_dir)
        sessions = work_dir / "sessions"
        dsh_home = work_dir / "dsh-home"
        sessions.mkdir(parents=True, exist_ok=True)
        dsh_home.mkdir(parents=True, exist_ok=True)
        resolved_patch = build_dsh_patch(
            cfg,
            sessions_root=sessions,
            mcp_command=sandbox.node,
            mcp_script=mcp_script,
            mcp_env=cua_bridge_env(self.executor),
            mcp_cwd=str(work_dir),
        )
        patch_path = work_dir / "dsh-run.patch.json"
        patch_path.write_text(
            json.dumps(resolved_patch, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        route_path = work_dir / "resolved-openrouter-route.json"
        route_path.write_text(
            json.dumps({"model": cfg.model, "provider": cfg.model_spec["route"]},
                       indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        route_path.chmod(0o600)
        (work_dir / "resolved-install.json").write_text(
            json.dumps({
                "dsh_version": installed,
                "dsh_binary": binary,
                "git_binary": git,
                "standard_patch_sha256": _PATCH_SHA256,
                "injector_sha256": _INJECTOR_SHA256,
                "agent_preset": "standard",
                "profile": "headless",
                "permission_mode": cfg.permission_mode,
                "outer_sandbox": "ale-sandbox",
                "mcp_policy": cfg.mcp_policy,
                "mcp_server": "cua",
                "mcp_command": sandbox.node,
                "mcp_script": mcp_script,
            }, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self._dsh_path = binary
        self._injector_path = str(injector)
        self._patch_path = str(patch_path)
        self._route_path = str(route_path)
        self._dsh_home = str(dsh_home)

    async def _start_injector(
        self, cfg: StudyDeepSeekConfig, stderr_path: Path,
    ) -> subprocess.Popen[bytes]:
        stderr = stderr_path.open("wb")
        process = subprocess.Popen(
            [
                self.executor.sandbox.node,
                self._injector_path,
                "--config", self._route_path,
                "--upstream", cfg.injector_upstream,
                "--log", str(Path(self.executor.work_dir) / "openrouter-injector.jsonl"),
                "--port", str(cfg.injector_port),
            ],
            stdout=stderr, stderr=subprocess.STDOUT,
            cwd=str(self.executor.work_dir),
            start_new_session=True,
        )
        process._study_stderr = stderr  # type: ignore[attr-defined]
        deadline = asyncio.get_running_loop().time() + 20
        while asyncio.get_running_loop().time() < deadline:
            if process.poll() is not None:
                stderr.close()
                raise RuntimeError("OpenRouter injector exited during startup")
            try:
                response = await asyncio.to_thread(
                    urllib.request.urlopen,
                    f"http://127.0.0.1:{cfg.injector_port}/health",
                    None, 1,
                )
                status = response.status
                response.close()
                if status in (200, 204):
                    return process
            except Exception:  # noqa: BLE001 - bounded startup polling
                await asyncio.sleep(0.2)
        process.terminate()
        await asyncio.to_thread(process.wait)
        stderr.close()
        raise TimeoutError("OpenRouter injector health check timed out")

    @staticmethod
    async def _reap(process: subprocess.Popen[Any]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            process.terminate()
        try:
            await asyncio.wait_for(
                asyncio.to_thread(process.wait), timeout=_TERM_GRACE_SECONDS,
            )
        except asyncio.TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                process.kill()
            await asyncio.to_thread(process.wait)

    async def launch(self, prompt: str) -> AgentRunResult:
        cfg: StudyDeepSeekConfig = self.config  # type: ignore[assignment]
        work_dir = Path(self.executor.work_dir)
        stdout_path = work_dir / "dsh.log"
        stderr_path = work_dir / "stderr.log"
        injector_stderr = work_dir / "openrouter-injector.stderr.log"
        pid_path = work_dir / "dsh.pid"
        for path in (stdout_path, stderr_path, injector_stderr, pid_path):
            path.unlink(missing_ok=True)

        env = os.environ.copy()
        env.update({str(k): str(v) for k, v in (self.executor.env or {}).items()})
        token = cfg.api_key or env.get("OPENROUTER_API_KEY")
        if not token:
            raise RuntimeError("DeepSeek Harness requires OPENROUTER_API_KEY")
        env.update({
            "OPENROUTER_API_KEY": token,
            "DSH_HOME": self._dsh_home,
            "DSH_PERMISSION_MODE": cfg.permission_mode,
            "DSH_TELEMETRY_DISABLED": "1",
            # ALE Docker containers share the host's small inotify instance
            # quota. DSH's stock chokidar watchers can exhaust it before the
            # first model request; polling preserves hot-reload semantics
            # without changing prompts, tools, or the agent loop.
            "CHOKIDAR_USEPOLLING": "1",
        })
        env.pop("DSH_TOOLS_MODE", None)

        injector = await self._start_injector(cfg, injector_stderr)
        argv = [
            self._dsh_path,
            "--profile", "headless",
            "--patch", self._patch_path,
            prompt,
        ]
        started = time.monotonic()
        process: subprocess.Popen[bytes] | None = None
        try:
            with stdout_path.open("wb") as output, stderr_path.open("wb") as error:
                process = subprocess.Popen(
                    argv, stdin=subprocess.DEVNULL, stdout=output, stderr=error,
                    env=env, cwd=str(work_dir), start_new_session=True,
                )
            pid_path.write_text(str(process.pid), encoding="ascii")
            while process.poll() is None:
                await asyncio.sleep(_POLL_SECONDS)
        except asyncio.CancelledError:
            if process is not None:
                await self._reap(process)
            raise
        finally:
            await self._reap(injector)
            injector._study_stderr.close()  # type: ignore[attr-defined]

        assert process is not None
        status = "completed" if process.returncode == 0 else "failed"
        error = None
        if status != "completed":
            tail = stderr_path.read_text(encoding="utf-8", errors="replace")[-1000:]
            error = f"DSH failed (rc={process.returncode}); stderr tail={tail}"
        return AgentRunResult(
            status=status,
            pid=process.pid,
            exit_code=process.returncode,
            transcript_path=str(stdout_path),
            stderr_path=str(stderr_path),
            duration_s=time.monotonic() - started,
            error=error,
        )

    @classmethod
    def _parse_session(
        cls,
        path: Path,
        builder: TrajectoryBuilder,
    ) -> tuple[Counter[str], dict[str, int], list[dict[str, Any]]]:
        event_counts: Counter[str] = Counter()
        totals = {
            "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
            "reasoning": 0,
        }
        request_headers: list[dict[str, Any]] = []
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                event_counts["malformed-json"] += 1
                continue
            event_type = str(event.get("type") or "unknown")
            event_counts[event_type] += 1
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            if event_type == "request/header":
                header = data.get("header") if isinstance(data.get("header"), dict) else {}
                request_headers.append({
                    "config": header.get("config") or {},
                    "adapter_defaults": header.get("adapterDefaults") or {},
                    "tool_count": len(header.get("tools") or []),
                    "tool_names": [
                        item.get("name") for item in (header.get("tools") or [])
                        if isinstance(item, dict)
                    ],
                })
                continue
            if event_type == "assistant/message":
                message = data.get("message") if isinstance(data.get("message"), dict) else {}
                text: list[str] = []
                reasoning: list[str] = []
                calls: list[ToolCall] = []
                for block in message.get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        text.append(str(block.get("text") or ""))
                    elif block.get("type") == "reasoning":
                        reasoning.append(str(block.get("text") or ""))
                    elif block.get("type") == "tool-call":
                        calls.append(ToolCall(
                            id=str(block.get("id") or ""),
                            name=str(block.get("name") or "unknown"),
                            arguments=_json_arguments(block.get("arguments")),
                        ))
                usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
                values = {
                    "input": int(usage.get("inputTokens") or 0),
                    "output": int(usage.get("outputTokens") or 0),
                    "cache_read": int(
                        usage.get("cacheReadTokens")
                        or usage.get("cachedInputTokens") or 0
                    ),
                    "cache_write": int(usage.get("cacheWriteTokens") or 0),
                    "reasoning": int(usage.get("reasoningTokens") or 0),
                }
                for key, value in values.items():
                    totals[key] += value
                builder.add_step(
                    "agent",
                    message="\n".join(item for item in text if item) or None,
                    reasoning="\n".join(item for item in reasoning if item) or None,
                    tool_calls=calls,
                    metrics=StepMetrics(
                        input_tokens=values["input"],
                        output_tokens=values["output"],
                        cache_read_tokens=values["cache_read"],
                        cache_creation_tokens=values["cache_write"],
                        cost_usd=None,
                    ),
                    extra={
                        "dsh_turn": data.get("turn"),
                        "dsh_step": data.get("step"),
                        "reasoning_tokens": values["reasoning"],
                    },
                )
                continue
            if event_type == "tool/result":
                message = data.get("message") if isinstance(data.get("message"), dict) else {}
                results: list[ToolResult] = []
                for block in message.get("content") or []:
                    if not isinstance(block, dict) or block.get("type") != "tool-result":
                        continue
                    results.append(ToolResult(
                        tool_call_id=str(block.get("toolCallId") or ""),
                        content=_tool_content(block.get("content") or []),
                        is_error=bool(block.get("isError")),
                    ))
                builder.add_step(
                    "environment",
                    observation=Observation(results=results),
                    extra={"dsh_turn": data.get("turn"), "dsh_step": data.get("step")},
                )
        return event_counts, totals, request_headers

    @classmethod
    def parse_artifacts(
        cls,
        *,
        work_dir: Path,
        config: StudyDeepSeekConfig,
        run_result: AgentRunResult,
        builder: TrajectoryBuilder,
    ) -> None:
        session_paths = sorted((work_dir / "sessions").rglob("session.jsonl"))
        if not session_paths:
            builder.add_step(
                "system", message="deepseek-harness: native session missing",
                extra={"reason": "no_session_jsonl"},
            )
            return
        primary = next(
            (path for path in session_paths if path.parent.name.startswith("session-")),
            session_paths[0],
        )
        all_counts: Counter[str] = Counter()
        all_totals = Counter()
        primary_counts, primary_totals, primary_headers = cls._parse_session(primary, builder)
        all_counts.update(primary_counts)
        all_totals.update(primary_totals)

        status = run_result.status if run_result.status in {"completed", "timeout", "failed"} else "failed"
        subagent_paths: list[str] = []
        for index, path in enumerate(session_paths):
            if path == primary:
                continue
            child = TrajectoryBuilder(
                agent_name="deepseek-harness-subagent",
                agent_version=config.cli_version,
                model=config.model,
                task_path=builder.trajectory.task_path,
                variant_index=builder.trajectory.variant_index,
            )
            counts, totals, headers = cls._parse_session(path, child)
            child.trajectory.extra["dsh"] = {
                "session_path": str(path),
                "event_counts": dict(counts),
                "native_usage": totals,
                "request_headers": headers,
                "subagent_index": index,
            }
            builder.trajectory.subagent_trajectories.append(
                child.finalize(reward=None, status=status)
            )
            subagent_paths.append(str(path))
            all_counts.update(counts)
            all_totals.update(totals)

        builder.trajectory.extra["dsh"] = {
            "exit_code": run_result.exit_code,
            "primary_session_path": str(primary),
            "subagent_session_paths": subagent_paths,
            "event_counts_all_sessions": dict(all_counts),
            "native_usage_all_sessions": dict(all_totals),
            "native_usage_scope": "main-and-subagents; title-call usage absent",
            "cost_source": "OpenRouter settled generation records required",
            "request_headers_primary": primary_headers,
            "provider": config.provider,
            "model": config.model,
            "route": config.model_spec["route"],
            "catalog_context_tokens": config.model_spec["context"],
            "catalog_max_output_tokens": config.model_spec["max_output"],
            "reasoning_effort": config.reasoning_effort,
            "profile": "headless-one-shot",
            "agent_preset": "standard",
            "permission_mode": config.permission_mode,
            "outer_sandbox": "ale-sandbox",
            "mcp_policy": config.mcp_policy,
        }
