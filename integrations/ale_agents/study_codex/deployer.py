"""ALE deployer retaining upstream CUA/trajectory logic with stock Codex."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import ClassVar
import urllib.request

from ale_run.agents.codex.deployer import CodexDeployer
from .config import StudyCodexConfig


class StudyCodexDeployer(CodexDeployer):
    """Stock 0.150.1 plus a request-only OpenRouter route injector."""

    _PINNED_VERSION: ClassVar[str] = "0.150.1"
    hot_artifacts: ClassVar[tuple[str, ...]] = CodexDeployer.hot_artifacts + (
        "openrouter-injector.jsonl",
        "openrouter-injector.stderr.log",
        "resolved-openrouter-route.json",
    )

    @property
    def version(self) -> str | None:
        return self._PINNED_VERSION

    async def install(self) -> None:
        cfg: StudyCodexConfig = self.config  # type: ignore[assignment]
        sandbox = self.executor.sandbox
        if not sandbox.is_linux:
            raise RuntimeError("stock StudyCodexDeployer is qualified only on ALE Linux")
        self._is_windows = False
        from ale_run.agents._bootstrap import ensure_npm, ensure_cua_mcp_server

        self._npm_path = await ensure_npm()
        codex_path = shutil.which("codex")
        if not codex_path or not await self._version_matches(codex_path, cfg.codex_version):
            await self._npm_install_codex(cfg.codex_version)
            codex_path = shutil.which("codex")
        if not codex_path:
            raise RuntimeError("codex is missing after the pinned npm install")
        probe = await asyncio.to_thread(
            subprocess.run, [codex_path, "--version"], capture_output=True,
            text=True, timeout=30,
        )
        if probe.returncode != 0 or cfg.codex_version not in (probe.stdout or ""):
            raise RuntimeError(
                f"refusing unpinned Codex: expected {cfg.codex_version}, "
                f"got {(probe.stdout or probe.stderr).strip()!r}"
            )
        self._codex_path = codex_path
        Path(self.executor.work_dir).mkdir(parents=True, exist_ok=True)
        await ensure_cua_mcp_server(sandbox)
        await self._write_codex_config(cfg)

    async def _write_codex_config(
        self, cfg: StudyCodexConfig, *, otel_endpoint: str | None = None,
    ) -> None:
        await super()._write_codex_config(cfg, otel_endpoint=otel_endpoint)
        path = Path(os.path.expanduser("~/.codex/config.toml"))
        text = path.read_text(encoding="utf-8")
        if "model_reasoning_summary" not in text:
            text = 'model_reasoning_summary = "none"\n' + text
        marker = f'base_url = "{cfg.base_url}"\n'
        if marker not in text:
            raise RuntimeError("could not locate the frozen provider base_url")
        if 'wire_api = "responses"' not in text:
            text = text.replace(marker, marker + 'wire_api = "responses"\n', 1)
        path.write_text(text, encoding="utf-8")

    async def _start_injector(self, cfg: StudyCodexConfig) -> subprocess.Popen:
        work = Path(self.executor.work_dir)
        route_path = work / "resolved-openrouter-route.json"
        route_path.write_text(
            json.dumps({
                "model": cfg.upstream_model,
                "provider": {
                    "only": list(cfg.provider_only),
                    "allow_fallbacks": cfg.allow_fallbacks,
                    "require_parameters": cfg.require_parameters,
                },
            }, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        route_path.chmod(0o600)
        stderr = (work / "openrouter-injector.stderr.log").open("wb")
        process = subprocess.Popen(
            [
                sys.executable, "-m", "ale_run.agents.study_codex.injector",
                "--config", str(route_path),
                "--log", str(work / "openrouter-injector.jsonl"),
                "--port", str(cfg.injector_port),
                "--upstream", cfg.injector_upstream,
            ],
            stdout=stderr, stderr=subprocess.STDOUT, start_new_session=True,
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
                if response.status == 204:
                    response.close()
                    return process
            except Exception:  # noqa: BLE001
                await asyncio.sleep(0.2)
        process.terminate()
        stderr.close()
        raise TimeoutError("OpenRouter injector health check timed out")

    async def launch(self, prompt: str):
        cfg: StudyCodexConfig = self.config  # type: ignore[assignment]
        injector = await self._start_injector(cfg)
        try:
            return await super().launch(prompt)
        finally:
            if injector.poll() is None:
                injector.terminate()
                try:
                    await asyncio.wait_for(asyncio.to_thread(injector.wait), timeout=5)
                except asyncio.TimeoutError:
                    injector.kill()
                    await asyncio.to_thread(injector.wait)
            injector._study_stderr.close()  # type: ignore[attr-defined]
