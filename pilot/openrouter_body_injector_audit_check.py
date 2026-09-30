#!/usr/bin/env python3
"""Zero-cost, loopback-only qualification for injector audit mode.

The fake upstream exercises a normal streamed response, a 429, and an
interrupted stream.  No credential or external network is required.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import socket
import stat
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


MODEL = "moonshotai/kimi-k3"
ROUTE = {
    "only": ["moonshotai/mxfp4"],
    "quantizations": ["mxfp4"],
    "allow_fallbacks": False,
    "require_parameters": False,
}
PROMPT_SENTINEL = "audit-prompt-must-not-appear-in-log"
SECRET_SENTINEL = "audit-secret-must-not-appear-in-log"
BRIDGE_TOOL_OUTPUT = "BRIDGE4_TOOL_RESULT_OK"
BRIDGE_FINAL = "BRIDGE4_FINAL_OK"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_healthy(process: subprocess.Popen[str], port: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            raise RuntimeError(f"injector exited before health check: {output}")
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=0.5
            ) as response:
                if response.status == 204:
                    return
        except Exception:
            time.sleep(0.05)
    raise RuntimeError("injector did not become healthy")


def post(port: int, case: str, body: bytes) -> tuple[int | None, bytes]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions?case={case}",
        data=body,
        headers={
            "authorization": f"Bearer {SECRET_SENTINEL}",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except (http.client.IncompleteRead, http.client.RemoteDisconnected, TimeoutError):
        return None, b""


def post_path(port: int, path: str, body: bytes) -> tuple[int | None, bytes]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=body,
        headers={"authorization": "Bearer local-only", "content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def load_records(path: Path, expected_end_count: int) -> list[dict[str, Any]]:
    deadline = time.monotonic() + 3
    records: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        if path.exists():
            records = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            if sum(row.get("event") == "request_end" for row in records) >= expected_end_count:
                return records
        time.sleep(0.02)
    raise RuntimeError(f"timed out waiting for {expected_end_count} request_end rows")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def chat_chunk(
    *, response_id: str, delta: dict[str, Any], finish_reason: str | None = None
) -> dict[str, Any]:
    return {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": MODEL,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def chat_sse(chunks: list[dict[str, Any]]) -> bytes:
    return (
        "".join(
            f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n"
            for chunk in chunks
        )
        + "data: [DONE]\n\n"
    ).encode("utf-8")


def response_events(payload: bytes) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in payload.decode("utf-8").splitlines():
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        value = json.loads(line.removeprefix("data: "))
        if isinstance(value, dict):
            events.append(value)
    return events


def wait_litellm(process: subprocess.Popen[str], port: int) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            raise RuntimeError(f"LiteLLM exited before health check: {output}")
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health/liveliness", timeout=0.5
            ) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("LiteLLM did not become healthy")


def responses_post(port: int, body: dict[str, Any]) -> bytes:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/responses",
        data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
        headers={"authorization": "Bearer bridge-dummy", "content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            require(response.status == 200, f"Responses returned {response.status}")
            return response.read()
    except urllib.error.HTTPError as error:
        raise RuntimeError(
            f"Responses failed with {error.code}: {error.read().decode(errors='replace')}"
        ) from error


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--injector", type=Path, required=True)
    args = parser.parse_args()

    received: list[tuple[str, bytes]] = []

    class FakeUpstream(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            size = int(self.headers.get("content-length", "0"))
            received.append((self.path, self.rfile.read(size)))
            case = self.path.rsplit("case=", 1)[-1]

            if "case=" not in self.path:
                request = json.loads(received[-1][1])
                messages = request.get("messages") or []
                tool_results = [
                    message
                    for message in messages
                    if isinstance(message, dict) and message.get("role") == "tool"
                ]
                if tool_results:
                    require(
                        BRIDGE_TOOL_OUTPUT in json.dumps(tool_results),
                        "bridge4 dropped the tool result before fake upstream",
                    )
                    response_id = "chatcmpl-bridge-final"
                    chunks = [
                        chat_chunk(response_id=response_id, delta={"role": "assistant"}),
                        chat_chunk(
                            response_id=response_id,
                            delta={"reasoning": "reason after tool"},
                        ),
                        chat_chunk(response_id=response_id, delta={"content": BRIDGE_FINAL}),
                        chat_chunk(response_id=response_id, delta={}, finish_reason="stop"),
                    ]
                    generation_id = "gen-bridge-final"
                else:
                    response_id = "chatcmpl-bridge-tool"
                    tool_call = {
                        "index": 0,
                        "id": "call_bridge4_shell",
                        "type": "function",
                        "function": {
                            "name": "shell",
                            "arguments": '{"cmd":"printf BRIDGE4_TOOL_RESULT_OK"}',
                        },
                    }
                    chunks = [
                        chat_chunk(response_id=response_id, delta={"role": "assistant"}),
                        chat_chunk(
                            response_id=response_id,
                            delta={"reasoning": "reason before tool"},
                        ),
                        chat_chunk(
                            response_id=response_id, delta={"tool_calls": [tool_call]}
                        ),
                        chat_chunk(
                            response_id=response_id, delta={}, finish_reason="tool_calls"
                        ),
                    ]
                    generation_id = "gen-bridge-tool"
                body = chat_sse(chunks)
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("x-generation-id", generation_id)
                self.send_header("x-request-id", generation_id.replace("gen-", "req-"))
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if case == "rate-limit":
                body = b'{"error":{"message":"fake 429"}}'
                self.send_response(429)
                self.send_header("content-type", "application/json")
                self.send_header("x-request-id", "req-audit-429")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if case == "interrupted":
                partial = b'data: {"id":"partial"}\n\n'
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("x-generation-id", "gen-audit-interrupted")
                self.send_header("x-request-id", "req-audit-interrupted")
                self.send_header("content-length", str(len(partial) + 256))
                self.end_headers()
                self.wfile.write(partial)
                self.wfile.flush()
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return

            body = b'data: {"id":"gen-audit-success","choices":[]}\n\ndata: [DONE]\n\n'
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("x-generation-id", "gen-audit-success")
            self.send_header("x-request-id", "req-audit-success")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return

    fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
    fake_thread = threading.Thread(target=fake.serve_forever, daemon=True)
    fake_thread.start()

    with tempfile.TemporaryDirectory(prefix="openrouter-audit-check-") as temp_name:
        temp = Path(temp_name)
        config_path = temp / "config.json"
        log_path = temp / "audit.jsonl"
        config_path.write_text(
            json.dumps({"model": MODEL, "provider": ROUTE}), encoding="utf-8"
        )
        os.chmod(config_path, 0o600)
        port = free_port()
        process = subprocess.Popen(
            [
                "node",
                str(args.injector),
                "--config",
                str(config_path),
                "--upstream",
                f"http://127.0.0.1:{fake.server_port}/api",
                "--log",
                str(log_path),
                "--port",
                str(port),
                "--mode",
                "audit",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        litellm: subprocess.Popen[str] | None = None
        responses_injector: subprocess.Popen[str] | None = None
        try:
            wait_healthy(process, port)
            request_object = {
                "model": MODEL,
                "messages": [{"role": "user", "content": PROMPT_SENTINEL}],
                "provider": ROUTE,
                "reasoning": {"effort": "high"},
                "usage": {"include": True},
                "stream": True,
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "shell",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                ],
                "user": "audit-user-raw-value",
            }
            # Deliberately retain non-canonical whitespace to prove audit mode
            # forwards the exact incoming bytes, not merely equivalent JSON.
            request_body = json.dumps(request_object, indent=1).encode("utf-8")
            require(post(port, "success", request_body)[0] == 200, "success case failed")
            require(post(port, "rate-limit", request_body)[0] == 429, "429 was hidden")
            post(port, "interrupted", request_body)

            mismatch = dict(request_object)
            mismatch["provider"] = {**ROUTE, "allow_fallbacks": True}
            mismatch_status, _ = post(
                port, "must-not-forward", json.dumps(mismatch).encode("utf-8")
            )
            require(mismatch_status == 502, "provider mismatch was not rejected")

            litellm_port = free_port()
            litellm_config = temp / "litellm.json"
            litellm_config.write_text(
                json.dumps(
                    {
                        "model_list": [
                            {
                                "model_name": "bridge4-model",
                                "litellm_params": {
                                    "model": f"openrouter/{MODEL}",
                                    "api_key": "dummy",
                                    "api_base": f"http://127.0.0.1:{port}/v1",
                                    "use_chat_completions_api": True,
                                    "allowed_openai_params": ["reasoning_effort"],
                                    "extra_body": {
                                        "provider": ROUTE,
                                        "usage": {"include": True},
                                    },
                                    "num_retries": 0,
                                    "timeout": 30,
                                },
                            }
                        ],
                        "litellm_settings": {"num_retries": 0, "request_timeout": 30},
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            litellm_env = os.environ.copy()
            litellm_env["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
            litellm = subprocess.Popen(
                [
                    "/opt/litellm/bin/litellm",
                    "--config",
                    str(litellm_config),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(litellm_port),
                ],
                env=litellm_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            wait_litellm(litellm, litellm_port)

            user_input = {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "use shell once"}],
            }
            first_payload = responses_post(
                litellm_port,
                {
                    "model": "bridge4-model",
                    "input": [user_input],
                    "tools": [
                        {
                            "type": "function",
                            "name": "shell",
                            "description": "Run a shell command",
                            "parameters": {
                                "type": "object",
                                "properties": {"cmd": {"type": "string"}},
                                "required": ["cmd"],
                            },
                            "strict": False,
                        }
                    ],
                    "reasoning": {"effort": "high"},
                    "stream": True,
                    "store": False,
                },
            )
            first_events = response_events(first_payload)
            first_items = [
                event["item"]
                for event in first_events
                if event.get("type") == "response.output_item.done"
                and isinstance(event.get("item"), dict)
                and event["item"].get("type") in {"reasoning", "function_call"}
            ]
            reasoning_items = [item for item in first_items if item["type"] == "reasoning"]
            function_calls = [item for item in first_items if item["type"] == "function_call"]
            require(
                reasoning_items,
                "bridge4 emitted no reasoning item before tool: "
                + json.dumps(
                    [
                        {
                            "type": event.get("type"),
                            "item_type": (event.get("item") or {}).get("type")
                            if isinstance(event.get("item"), dict)
                            else None,
                        }
                        for event in first_events
                    ]
                ),
            )
            require(len(function_calls) == 1, "bridge4 emitted no single tool call")
            tool_call = function_calls[0]
            require(tool_call.get("name") == "shell", "bridge4 changed tool name")

            second_payload = responses_post(
                litellm_port,
                {
                    "model": "bridge4-model",
                    "input": [
                        user_input,
                        *first_items,
                        {
                            "type": "function_call_output",
                            "call_id": tool_call["call_id"],
                            "output": BRIDGE_TOOL_OUTPUT,
                        },
                    ],
                    "tools": [
                        {
                            "type": "function",
                            "name": "shell",
                            "description": "Run a shell command",
                            "parameters": {
                                "type": "object",
                                "properties": {"cmd": {"type": "string"}},
                                "required": ["cmd"],
                            },
                            "strict": False,
                        }
                    ],
                    "reasoning": {"effort": "high"},
                    "stream": True,
                    "store": False,
                },
            )
            second_events = response_events(second_payload)
            require(
                any(
                    event.get("type") == "response.reasoning_summary_text.delta"
                    for event in second_events
                ),
                "bridge4 emitted no reasoning after tool result",
            )
            require(BRIDGE_FINAL in second_payload.decode("utf-8"), "bridge4 lost final text")

            responses_config = temp / "responses-inject.json"
            responses_log = temp / "responses-inject.jsonl"
            upstream_model = "openai/gpt-6-astra"
            incoming_model = "gpt-6-astra"
            openai_route = {
                "only": ["openai"],
                "allow_fallbacks": False,
                "require_parameters": False,
            }
            responses_config.write_text(
                json.dumps(
                    {
                        "incoming_model": incoming_model,
                        "model": upstream_model,
                        "provider": openai_route,
                    }
                ),
                encoding="utf-8",
            )
            responses_port = free_port()
            responses_injector = subprocess.Popen(
                [
                    "node",
                    str(args.injector),
                    "--config",
                    str(responses_config),
                    "--upstream",
                    f"http://127.0.0.1:{fake.server_port}/api",
                    "--log",
                    str(responses_log),
                    "--port",
                    str(responses_port),
                    "--mode",
                    "inject",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            wait_healthy(responses_injector, responses_port)
            responses_request = {
                "model": incoming_model,
                "input": [{"type": "additional_tools", "role": "developer", "tools": []}],
                "reasoning": {"effort": "high", "context": "all_turns"},
                "parallel_tool_calls": False,
                "stream": True,
            }
            status, _ = post_path(
                responses_port,
                "/v1/responses?case=success",
                json.dumps(responses_request, separators=(",", ":")).encode(),
            )
            require(status == 200, "Responses injector failed")
            responses_records = load_records(responses_log, expected_end_count=1)
            responses_end = [
                row for row in responses_records if row["event"] == "request_end"
            ][0]
            injected_body = json.loads(received[-1][1])
            require(
                injected_body
                == {**responses_request, "model": upstream_model, "provider": openai_route},
                "Responses injector changed fields beyond model/provider",
            )
            require(
                responses_end["changed_fields"] == ["model", "provider"]
                and responses_end["provider_validation"] == "injected"
                and responses_end["request_audit"]["reasoning_effort"] == "high"
                and responses_end["request_audit"]["parallel_tool_calls"] is False
                and responses_end["request_audit"]["max_output_tokens_present"] is False,
                "Responses injector audit controls drifted",
            )

            records = load_records(log_path, expected_end_count=6)
            log_text = log_path.read_text(encoding="utf-8")
            require(PROMPT_SENTINEL not in log_text, "prompt leaked into audit log")
            require(SECRET_SENTINEL not in log_text, "credential leaked into audit log")
            require("audit-user-raw-value" not in log_text, "raw user id leaked")
            require(
                stat.S_IMODE(log_path.stat().st_mode) == 0o600,
                "audit JSONL is not mode 0600",
            )

            require(len(received) == 6, "unexpected number of fake-upstream calls")
            require(
                all(raw == request_body for _, raw in received[:3]),
                "audit mode changed request bytes",
            )

            headers = [row for row in records if row["event"] == "upstream_headers"]
            ends = [row for row in records if row["event"] == "request_end"]
            require(len(headers) == 5 and len(ends) == 6, "incomplete per-call evidence")
            by_sequence = {row["sequence"]: row for row in ends}
            header_by_sequence = {row["sequence"]: row for row in headers}

            for sequence in (1, 2, 3):
                header = header_by_sequence[sequence]
                end = by_sequence[sequence]
                audit = end["request_audit"]
                require(header["mode"] == end["mode"] == "audit", "wrong mode")
                require(
                    header["provider_validation"] == end["provider_validation"]
                    == "matched-existing",
                    "existing provider was not validated",
                )
                require(header["changed_fields"] == end["changed_fields"] == [], "request changed")
                require(
                    end["incoming_body_sha256"] == end["forwarded_body_sha256"],
                    "forwarded hash differs",
                )
                require(end["injector_retry_count"] == 0, "injector retried")
                require(end["injector_timeout_ms"] is None, "injector added timeout")
                require(audit["provider_only"] == ROUTE["only"], "route not audited")
                require(
                    audit["provider_quantizations"] == ROUTE["quantizations"],
                    "quantization not audited",
                )
                require(audit["provider_allow_fallbacks"] is False, "fallback not audited")
                require(audit["reasoning_effort"] == "high", "reasoning not audited")
                require(audit["usage_include"] is True, "usage.include not audited")
                require(audit["max_tokens_present"] is False, "unexpected output cap")
                require(audit["tools_count"] == 1, "tools not audited")

            require(
                header_by_sequence[1]["openrouter_generation_id"] == "gen-audit-success"
                and by_sequence[1]["upstream_response_completed"] is True,
                "successful stream evidence incomplete",
            )
            require(
                header_by_sequence[2]["upstream_status"] == 429
                and header_by_sequence[2]["openrouter_request_id"] == "req-audit-429"
                and by_sequence[2]["upstream_response_completed"] is True,
                "429 evidence incomplete",
            )
            require(
                header_by_sequence[3]["openrouter_generation_id"]
                == "gen-audit-interrupted"
                and by_sequence[3]["upstream_response_completed"] is False
                and by_sequence[3]["error"],
                "interrupted stream evidence incomplete",
            )
            require(
                by_sequence[4]["provider_validation"] == "mismatch"
                and by_sequence[4]["upstream_status"] is None
                and by_sequence[4]["error"],
                "provider mismatch evidence incomplete",
            )
            bridge_requests = [json.loads(raw) for _, raw in received[3:5]]
            require(
                all(request.get("model") == MODEL for request in bridge_requests),
                "LiteLLM did not send the exact OpenRouter model slug",
            )
            require(
                all(request.get("provider") == ROUTE for request in bridge_requests),
                "LiteLLM extra_body provider changed or disappeared",
            )
            require(
                all(request.get("reasoning_effort") == "high" for request in bridge_requests),
                "LiteLLM did not map Responses reasoning effort to Chat",
            )
            for sequence in (5, 6):
                bridge_audit = by_sequence[sequence]["request_audit"]
                require(
                    by_sequence[sequence]["provider_validation"] == "matched-existing"
                    and by_sequence[sequence]["changed_fields"] == []
                    and by_sequence[sequence]["incoming_body_sha256"]
                    == by_sequence[sequence]["forwarded_body_sha256"],
                    "sidecar changed a bridge4 request body",
                )
                require(
                    bridge_audit["reasoning_effort"] == "high"
                    and bridge_audit["usage_include"] is True
                    and bridge_audit["max_tokens_present"] is False
                    and by_sequence[sequence]["injector_retry_count"] == 0
                    and by_sequence[sequence]["injector_timeout_ms"] is None,
                    "bridge4 wire controls or sidecar transport controls drifted",
                )
            require(
                [header_by_sequence[index]["openrouter_generation_id"] for index in (5, 6)]
                == ["gen-bridge-tool", "gen-bridge-final"],
                "sidecar missed a bridge4 generation header",
            )

            print(
                json.dumps(
                    {
                        "status": "passed",
                        "external_api_calls": 0,
                        "forwarded_calls": len(received),
                        "header_records": len(headers),
                        "request_end_records": len(ends),
                        "log_mode": "0600",
                        "bridge4_responses_calls": 2,
                        "bridge4_tool_result_roundtrip": True,
                        "bridge4_reasoning_roundtrip": True,
                        "bridge4_final": BRIDGE_FINAL,
                        "responses_inject_changed_fields": ["model", "provider"],
                    },
                    sort_keys=True,
                )
            )
        finally:
            if responses_injector is not None:
                responses_injector.terminate()
                try:
                    responses_injector.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    responses_injector.kill()
                    responses_injector.wait(timeout=3)
            if litellm is not None:
                litellm.terminate()
                try:
                    litellm.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    litellm.kill()
                    litellm.wait(timeout=3)
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            fake.shutdown()
            fake.server_close()


if __name__ == "__main__":
    main()
