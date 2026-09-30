#!/usr/bin/env python3
"""Offline Codex -> LiteLLM Responses-to-Chat bridge contract check.

Run this inside the pinned provider-smoke image. Both HTTP peers are local mocks;
the check cannot call a provider or incur model cost.
"""

from __future__ import annotations

import http.server
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
from typing import Any
import urllib.error
import urllib.request


MARKER = "BRIDGE_TOOL_OK"
FINAL = "BRIDGE_LOOP_OK"
MISSING_AGENT_ID = "00000000-0000-0000-0000-000000000000"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.codex_requests: list[dict[str, Any]] = []
        self.chat_requests: list[dict[str, Any]] = []
        self.called_chat_tools: list[str] = []

    def append(self, target: str, value: dict[str, Any]) -> None:
        with self.lock:
            getattr(self, target).append(value)


def read_json(handler: http.server.BaseHTTPRequestHandler) -> dict[str, Any]:
    size = int(handler.headers.get("Content-Length", "0"))
    value = json.loads(handler.rfile.read(size))
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def send_bytes(
    handler: http.server.BaseHTTPRequestHandler,
    status: int,
    content_type: str,
    payload: bytes,
) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def chat_chunk(delta: dict[str, Any], finish_reason: str | None = None) -> dict[str, Any]:
    return {
        "id": "chatcmpl_bridge_check",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "fake-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def sse(chunks: list[dict[str, Any]]) -> bytes:
    lines = [f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n" for chunk in chunks]
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def response_events(path: Path) -> list[dict[str, Any]]:
    body = json.loads(path.read_text())["body"]
    events: list[dict[str, Any]] = []
    for line in str(body).splitlines():
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        event = json.loads(line.removeprefix("data: "))
        if isinstance(event, dict):
            events.append(event)
    return events


def assert_reasoning_event_lifecycle(path: Path) -> None:
    events = response_events(path)
    reasoning_added = [
        event
        for event in events
        if event.get("type") == "response.output_item.added"
        and (event.get("item") or {}).get("type") == "reasoning"
    ]
    if len(reasoning_added) != 1:
        raise AssertionError(f"expected one reasoning item in {path.name}, got {len(reasoning_added)}")
    item = reasoning_added[0]["item"]
    item_id = item["id"]
    if item.get("summary") != []:
        raise AssertionError(f"reasoning item did not start with an empty summary in {path.name}")

    relevant_types = {
        "response.output_item.added",
        "response.reasoning_summary_part.added",
        "response.reasoning_summary_text.delta",
        "response.reasoning_summary_text.done",
        "response.reasoning_summary_part.done",
        "response.output_item.done",
    }
    relevant = [
        event
        for event in events
        if event.get("type") in relevant_types
        and (
            (event.get("item") or {}).get("id") == item_id
            or event.get("item_id") == item_id
        )
    ]
    types = [str(event.get("type")) for event in relevant]
    required_order = [
        "response.output_item.added",
        "response.reasoning_summary_part.added",
        "response.reasoning_summary_text.delta",
        "response.reasoning_summary_text.done",
        "response.reasoning_summary_part.done",
        "response.output_item.done",
    ]
    positions: list[int] = []
    for event_type in required_order:
        try:
            positions.append(types.index(event_type))
        except ValueError as exc:
            raise AssertionError(f"missing {event_type} in {path.name}: {types}") from exc
    if positions != sorted(positions):
        raise AssertionError(f"invalid reasoning event order in {path.name}: {types}")

    deltas = [
        event for event in relevant if event.get("type") == "response.reasoning_summary_text.delta"
    ]
    if len(deltas) < 2:
        raise AssertionError(f"multi-chunk reasoning fixture collapsed in {path.name}")
    if any(event.get("item_id") != item_id for event in relevant if "item_id" in event):
        raise AssertionError(f"reasoning events changed item_id in {path.name}")
    if any(event.get("summary_index") != 0 for event in relevant if "summary_index" in event):
        raise AssertionError(f"reasoning events changed summary_index in {path.name}")
    sequence_numbers = [event.get("sequence_number") for event in relevant]
    if any(not isinstance(value, int) for value in sequence_numbers):
        missing_sequence = [
            event.get("type")
            for event in relevant
            if not isinstance(event.get("sequence_number"), int)
        ]
        raise AssertionError(
            f"reasoning event omitted sequence_number in {path.name}: {missing_sequence}"
        )
    if sequence_numbers != sorted(set(sequence_numbers)):
        raise AssertionError(f"reasoning sequence numbers are not strictly increasing in {path.name}")


def tool_arguments(tool: dict[str, Any]) -> dict[str, Any] | None:
    function = tool.get("function") or {}
    name = str(function.get("name") or "").lower()
    if not any(part in name for part in ("shell", "exec", "command")):
        return None
    properties = (function.get("parameters") or {}).get("properties") or {}
    if "cmd" in properties:
        return {"cmd": f"printf {MARKER}"}
    if "command" in properties:
        command_type = properties["command"].get("type")
        command: Any = ["sh", "-lc", f"printf {MARKER}"] if command_type == "array" else f"printf {MARKER}"
        return {"command": command}
    if "content" in properties:
        return {"content": f"printf {MARKER}"}
    return None


def choose_shell_tool(tools: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    for tool in tools:
        arguments = tool_arguments(tool)
        if arguments is not None:
            return str(tool["function"]["name"]), arguments
    names = [str((tool.get("function") or {}).get("name")) for tool in tools]
    raise AssertionError(f"bridge exposed no callable shell tool; chat tools={names}")


def choose_namespace_tool(tools: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    """Choose a harmless namespace call that fails fast without spawning work."""
    expected = "multi_agent_v1__close_agent"
    for tool in tools:
        if str((tool.get("function") or {}).get("name") or "") == expected:
            return expected, {"target": MISSING_AGENT_ID}
    names = [str((tool.get("function") or {}).get("name")) for tool in tools]
    raise AssertionError(f"bridge did not flatten the expected namespace child; chat tools={names}")


def make_upstream_handler(state: State):
    class UpstreamHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            return

        def do_POST(self) -> None:
            if not self.path.endswith("/chat/completions"):
                send_bytes(self, 404, "application/json", b'{"error":"not found"}')
                return
            request = read_json(self)
            state.append("chat_requests", request)
            messages = request.get("messages") or []
            tool_results = [
                message
                for message in messages
                if isinstance(message, dict) and message.get("role") == "tool"
            ]

            if len(tool_results) >= 2:
                chunks = [
                    chat_chunk({"role": "assistant"}),
                    chat_chunk({"reasoning_content": "mock "}),
                    chat_chunk({"reasoning_content": "reasoning"}),
                    chat_chunk({"content": FINAL}),
                    chat_chunk({}, "stop"),
                ]
                nonstream_message: dict[str, Any] = {
                    "role": "assistant",
                    "reasoning_content": "mock reasoning",
                    "content": FINAL,
                }
            else:
                if not tool_results:
                    name, arguments = choose_namespace_tool(request.get("tools") or [])
                    reasoning = "mock namespace reasoning"
                else:
                    name, arguments = choose_shell_tool(request.get("tools") or [])
                    reasoning = "mock shell reasoning"
                state.called_chat_tools.append(name)
                call = {
                    "index": 0,
                    "id": f"call_bridge_check_{len(tool_results)}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments, separators=(",", ":"))},
                }
                chunks = [
                    chat_chunk({"role": "assistant"}),
                    chat_chunk({"role": "assistant", "reasoning_content": reasoning[:5]}),
                    chat_chunk({"reasoning_content": reasoning[5:]}),
                    chat_chunk({"tool_calls": [call]}),
                    chat_chunk({}, "tool_calls"),
                ]
                nonstream_message = {"role": "assistant", "content": None, "tool_calls": [call]}

            if request.get("stream"):
                send_bytes(self, 200, "text/event-stream", sse(chunks))
                return
            response = {
                "id": "chatcmpl_bridge_check",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": "fake-model",
                "choices": [{"index": 0, "message": nonstream_message, "finish_reason": "stop" if len(tool_results) >= 2 else "tool_calls"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
            send_bytes(self, 200, "application/json", json.dumps(response).encode())

    return UpstreamHandler


def make_capture_handler(state: State, proxy_port: int):
    class CaptureHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            return

        def do_POST(self) -> None:
            raw_size = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(raw_size)
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                state.append("codex_requests", parsed)
            target = f"http://127.0.0.1:{proxy_port}{self.path}"
            request = urllib.request.Request(
                target,
                data=raw,
                headers={"Authorization": self.headers.get("Authorization", "Bearer dummy"), "Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=45) as response:
                    send_bytes(self, response.status, response.headers.get_content_type(), response.read())
            except urllib.error.HTTPError as exc:
                send_bytes(self, exc.code, exc.headers.get_content_type(), exc.read())

    return CaptureHandler


def serve(handler: type[http.server.BaseHTTPRequestHandler], port: int) -> http.server.ThreadingHTTPServer:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def wait_for_proxy(port: int, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("LiteLLM proxy exited during startup")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health/liveliness", timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.2)
    raise TimeoutError("LiteLLM proxy did not become ready")


def wait_for_tcp(port: int, process: subprocess.Popen[str], label: str) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"{label} exited during startup")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.2)
    raise TimeoutError(f"{label} did not become ready")


def start_codex_recorder(
    *, port: int, upstream_url: str, dump_dir: Path, log_path: Path
) -> tuple[subprocess.Popen[str], Any]:
    log = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            "codex",
            "responses-api-proxy",
            "--port",
            str(port),
            "--upstream-url",
            upstream_url,
            "--dump-dir",
            str(dump_dir),
        ],
        stdin=subprocess.PIPE,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert process.stdin is not None
    process.stdin.write("pilot-local-recorder\n")
    process.stdin.close()
    wait_for_tcp(port, process, "Codex Responses recorder")
    return process, log


def expected_chat_names(tools: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
    expected: set[str] = set()
    unsupported: set[str] = set()
    for tool in tools:
        kind = str(tool.get("type") or "")
        name = str(tool.get("name") or kind)
        if kind in {"function", "custom"}:
            expected.add(name)
        elif kind == "namespace":
            nested = tool.get("tools")
            if isinstance(nested, list):
                expected.update(
                    f"{name}__{child.get('name')}"
                    for child in nested
                    if isinstance(child, dict) and child.get("type") == "function"
                )
            else:
                expected.add(name)
        elif kind in {"web_search", "web_search_preview"}:
            continue
        elif kind in {"computer_use", "image_generation", "shell"}:
            unsupported.add(kind)
        else:
            unsupported.add(kind or "<missing>")
    return expected, unsupported


def main() -> int:
    state = State()
    upstream_port, proxy_port, capture_port, recorder_port = (
        free_port(),
        free_port(),
        free_port(),
        free_port(),
    )
    upstream = serve(make_upstream_handler(state), upstream_port)
    capture = serve(make_capture_handler(state, proxy_port), capture_port)

    with tempfile.TemporaryDirectory(prefix="codex-bridge-check-") as tmp_name:
        tmp = Path(tmp_name)
        codex_home = tmp / "codex-home"
        workspace = tmp / "workspace"
        codex_home.mkdir()
        workspace.mkdir()
        dump_dir = tmp / "codex-responses-dumps"
        config = {
            "model_list": [
                {
                    "model_name": "pilot-model",
                    "litellm_params": {
                        "model": "openai/fake-model",
                        "api_key": "dummy",
                        "api_base": f"http://127.0.0.1:{upstream_port}/v1",
                        "use_chat_completions_api": True,
                        "allowed_openai_params": ["reasoning_effort"],
                        "num_retries": 0,
                        "timeout": 30,
                    },
                }
            ],
            "litellm_settings": {"num_retries": 0, "request_timeout": 30},
        }
        proxy_config = tmp / "litellm.json"
        proxy_config.write_text(json.dumps(config, indent=2) + "\n")
        proxy_log_path = tmp / "litellm.log"
        codex_config = f'''model = "pilot-model"
model_provider = "bridge-check"
model_reasoning_effort = "low"
model_reasoning_summary = "none"

[model_providers.bridge-check]
name = "bridge-check"
base_url = "http://127.0.0.1:{recorder_port}/v1"
env_key = "BRIDGE_DUMMY_KEY"
wire_api = "responses"
request_max_retries = 0
stream_max_retries = 0
stream_idle_timeout_ms = 30000
'''
        (codex_home / "config.toml").write_text(codex_config)

        with proxy_log_path.open("w", encoding="utf-8") as proxy_log:
            proxy = subprocess.Popen(
                [
                    "/opt/litellm/bin/litellm",
                    "--config",
                    str(proxy_config),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(proxy_port),
                ],
                stdout=proxy_log,
                stderr=subprocess.STDOUT,
                text=True,
            )
            recorder: subprocess.Popen[str] | None = None
            recorder_log: Any = None
            try:
                wait_for_proxy(proxy_port, proxy)
                recorder, recorder_log = start_codex_recorder(
                    port=recorder_port,
                    upstream_url=f"http://127.0.0.1:{capture_port}/v1/responses",
                    dump_dir=dump_dir,
                    log_path=tmp / "codex-recorder.log",
                )
                environment = os.environ.copy()
                environment.update({"CODEX_HOME": str(codex_home), "BRIDGE_DUMMY_KEY": "dummy"})
                completed = subprocess.run(
                    [
                        "codex",
                        "exec",
                        "--skip-git-repo-check",
                        "--ephemeral",
                        "--json",
                        "--dangerously-bypass-approvals-and-sandbox",
                        "-C",
                        str(workspace),
                        f"Use the terminal once to run `printf {MARKER}`, then reply exactly {FINAL}.",
                    ],
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=60,
                    check=False,
                )
            finally:
                if recorder is not None:
                    recorder.terminate()
                    try:
                        recorder.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        recorder.kill()
                    if recorder_log is not None:
                        recorder_log.close()
                proxy.terminate()
                try:
                    proxy.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proxy.kill()

        upstream.shutdown()
        capture.shutdown()

        if (
            completed.returncode != 0
            or FINAL not in completed.stdout
            or "OutputTextDelta without active item" in completed.stdout
            or "ReasoningSummaryDelta without active item" in completed.stdout
        ):
            print(
                json.dumps(
                    {
                        "captured_codex_tool_types": [
                            str(tool.get("type"))
                            for request in state.codex_requests
                            for tool in (request.get("tools") or [])
                            if isinstance(tool, dict)
                        ],
                        "captured_chat_tool_names": [
                            str((tool.get("function") or {}).get("name"))
                            for request in state.chat_requests
                            for tool in (request.get("tools") or [])
                            if isinstance(tool, dict)
                        ],
                    },
                    indent=2,
                )
            )
            print(completed.stdout[-8000:])
            print(proxy_log_path.read_text(errors="replace")[-8000:])
            raise AssertionError(f"Codex bridge loop failed with rc={completed.returncode}")
        if len(state.codex_requests) < 3 or len(state.chat_requests) < 3:
            raise AssertionError(
                f"expected three-turn loop, got responses={len(state.codex_requests)} chat={len(state.chat_requests)}"
            )

        original_tools = state.codex_requests[0].get("tools") or []
        chat_tools = state.chat_requests[0].get("tools") or []
        expected, unsupported = expected_chat_names(original_tools)
        actual = {str((tool.get("function") or {}).get("name")) for tool in chat_tools}
        missing = expected - actual
        has_search = any(tool.get("type") in {"web_search", "web_search_preview"} for tool in original_tools)
        if has_search and "web_search_options" not in state.chat_requests[0]:
            unsupported.add("web_search-not-mapped")
        if unsupported or missing:
            raise AssertionError(f"tool translation loss: unsupported={sorted(unsupported)} missing={sorted(missing)}")

        namespace_tool_messages = [
            message
            for message in state.chat_requests[1].get("messages") or []
            if isinstance(message, dict) and message.get("role") == "tool"
        ]
        if not namespace_tool_messages:
            raise AssertionError("namespace tool result did not survive Responses-to-Chat roundtrip")
        first_replay_input = state.codex_requests[1].get("input") or []
        if not any(
            isinstance(item, dict) and item.get("type") == "reasoning"
            for item in first_replay_input
        ):
            raise AssertionError("Codex did not replay the streamed reasoning item")
        if any(
            isinstance(item, dict)
            and item.get("type") == "message"
            and item.get("role") == "assistant"
            and isinstance(item.get("content"), list)
            and all(
                isinstance(part, dict) and not part.get("text")
                for part in item.get("content") or []
            )
            for item in first_replay_input
        ):
            raise AssertionError("Codex replay contained an empty assistant text item")
        if "mock namespace reasoning" not in json.dumps(state.chat_requests[1]):
            codex_input = state.codex_requests[1].get("input") or []
            codex_shapes = [
                {
                    "type": item.get("type"),
                    "keys": sorted(item),
                    **(
                        {
                            "content": item.get("content"),
                            "summary": item.get("summary"),
                            "encrypted_content": item.get("encrypted_content"),
                        }
                        if item.get("type") == "reasoning"
                        else {}
                    ),
                }
                for item in codex_input
                if isinstance(item, dict)
            ]
            chat_shapes = [
                {"role": item.get("role"), "keys": sorted(item)}
                for item in state.chat_requests[1].get("messages") or []
                if isinstance(item, dict)
            ]
            raise AssertionError(
                "reasoning content did not survive the namespace tool-call turn; "
                f"codex_input={codex_shapes} chat_messages={chat_shapes}"
            )

        shell_tool_messages = [
            message
            for message in state.chat_requests[2].get("messages") or []
            if isinstance(message, dict) and message.get("role") == "tool"
        ]
        if len(shell_tool_messages) < 2 or MARKER not in json.dumps(shell_tool_messages):
            raise AssertionError("shell tool result did not survive Responses-to-Chat roundtrip")
        if "mock shell reasoning" not in json.dumps(state.chat_requests[2]):
            raise AssertionError("reasoning content did not survive the shell tool-call turn")

        request_dumps = sorted(dump_dir.glob("*-request.json"))
        response_dumps = sorted(dump_dir.glob("*-response.json"))
        if len(request_dumps) < 3 or len(response_dumps) < 3:
            raise AssertionError(
                f"Codex recorder missed exchanges: requests={len(request_dumps)} responses={len(response_dumps)}"
            )
        if any("pilot-local-recorder" in path.read_text(errors="replace") for path in request_dumps):
            raise AssertionError("Codex recorder did not redact its local authorization header")
        for response_dump in response_dumps:
            assert_reasoning_event_lifecycle(response_dump)

        types: dict[str, int] = {}
        for tool in original_tools:
            kind = str(tool.get("type") or "<missing>")
            types[kind] = types.get(kind, 0) + 1
        print(
            json.dumps(
                {
                    "status": "passed",
                    "paid_api_calls": 0,
                    "codex_version": "0.150.1",
                    "litellm_version": "1.98.0",
                    "responses_requests": len(state.codex_requests),
                    "chat_requests": len(state.chat_requests),
                    "codex_tool_types": types,
                    "chat_tool_count": len(chat_tools),
                    "codex_namespaces": {
                        str(tool.get("name")): [
                            str(child.get("name"))
                            for child in tool.get("tools") or []
                            if isinstance(child, dict)
                        ]
                        for tool in original_tools
                        if tool.get("type") == "namespace"
                    },
                    "called_chat_tools": state.called_chat_tools,
                    "namespace_dispatch_roundtrip": state.called_chat_tools[0]
                    == "multi_agent_v1__close_agent",
                    "raw_recorder_request_dumps": len(request_dumps),
                    "raw_recorder_response_dumps": len(response_dumps),
                    "web_search_mapped_to_chat_options": has_search,
                    "tool_result_roundtrip": True,
                    "reasoning_roundtrip": True,
                    "reasoning_then_content_stream_order": FINAL in completed.stdout,
                },
                indent=2,
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
