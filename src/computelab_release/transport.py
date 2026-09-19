"""Bounded, redirect-free HTTP(S) chat-completion transport.

Endpoints are explicitly chosen by the local operator. This is not an SSRF
sandbox for exposing arbitrary endpoint selection to remote users.
"""

from __future__ import annotations

import hashlib
import http.client
import os
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .models import canonical_json, finite_number, strict_json, validate_endpoint

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_LINE_BYTES = 64 * 1024
MAX_EVENTS = 16384
MAX_TOOLS = 32


@dataclass(frozen=True, slots=True)
class CompletionResult:
    ok: bool
    content: str = ""
    tool_calls: list[dict[str, Any]] | None = None
    usage: dict[str, int] | None = None
    status_code: int | None = None
    latency_ms: float = 0.0
    ttft_ms: float | None = None
    output_tokens: int | None = None
    response_sha256: str | None = None
    error_kind: str | None = None


class ProtocolError(Exception):
    """Internal, value-free error code."""


def _usage(value: Any) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ProtocolError("invalid_usage")
    result: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if key in value:
            number = value[key]
            if type(number) is not int or number < 0:
                raise ProtocolError("invalid_usage")
            result[key] = number
    return result


def _text(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ProtocolError("unsupported_content_shape")
    return value


def _tools(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_TOOLS:
        raise ProtocolError("invalid_tools")
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict) or item.get("type", "function") != "function":
            raise ProtocolError("unsupported_tool_type")
        function = item.get("function")
        if (
            not isinstance(function, dict)
            or not isinstance(function.get("name"), str)
            or not function["name"]
        ):
            raise ProtocolError("invalid_tool_function")
        arguments = function.get("arguments")
        if not isinstance(arguments, str):
            raise ProtocolError("invalid_tool_arguments")
        result.append({"function": {"name": function["name"], "arguments": arguments}})
    return result


class StreamParser:
    """Incremental SSE parser with bounded lines/events and first-content timing."""

    def __init__(self, started: float) -> None:
        self.started = started
        self.pending = bytearray()
        self.data_lines: list[bytes] = []
        self.event_bytes = 0
        self.events = 0
        self.done = False
        self.finished = False
        self.seen_choice = False
        self.content: list[str] = []
        self.tools: dict[int, dict[str, str]] = {}
        self.usage: dict[str, int] = {}
        self.ttft_ms: float | None = None

    def feed(self, chunk: bytes) -> None:
        self.pending.extend(chunk)
        while b"\n" in self.pending:
            raw, _, remaining = self.pending.partition(b"\n")
            self.pending = bytearray(remaining)
            if len(raw) > MAX_LINE_BYTES:
                raise ProtocolError("stream_line_limit")
            line = raw.rstrip(b"\r")
            if not line:
                if self.data_lines:
                    self._event(b"\n".join(self.data_lines))
                self.data_lines = []
                self.event_bytes = 0
            elif line.startswith(b"data:"):
                if self.done:
                    raise ProtocolError("data_after_done")
                data = bytes(line[5:]).lstrip(b" ")
                self.event_bytes += len(data)
                if self.event_bytes > MAX_LINE_BYTES:
                    raise ProtocolError("stream_event_limit")
                self.data_lines.append(data)
        if len(self.pending) > MAX_LINE_BYTES:
            raise ProtocolError("stream_line_limit")

    def _event(self, data: bytes) -> None:
        self.events += 1
        if self.events > MAX_EVENTS:
            raise ProtocolError("stream_event_count_limit")
        if data == b"[DONE]":
            if not self.finished or not self.seen_choice:
                raise ProtocolError("premature_done")
            self.done = True
            return
        try:
            event = strict_json(data)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise ProtocolError("invalid_stream_json") from exc
        if not isinstance(event, dict) or "error" in event:
            raise ProtocolError("invalid_stream_shape")
        self.usage.update(_usage(event.get("usage")))
        choices = event.get("choices")
        if not isinstance(choices, list):
            raise ProtocolError("missing_choices")
        if not choices:  # trailing server usage event
            return
        if len(choices) != 1 or not isinstance(choices[0], dict):
            raise ProtocolError("unsupported_choices")
        choice = choices[0]
        if type(choice.get("index", 0)) is not int or choice.get("index", 0) != 0:
            raise ProtocolError("unsupported_choice_index")
        self.seen_choice = True
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            raise ProtocolError("missing_delta")
        text = _text(delta.get("content"))
        tool_deltas = delta.get("tool_calls", [])
        if not isinstance(tool_deltas, list) or len(tool_deltas) > MAX_TOOLS:
            raise ProtocolError("invalid_tools")
        if self.finished and (text or tool_deltas):
            raise ProtocolError("content_after_finish")
        output_event = bool(text)
        if text:
            self.content.append(text)
        for item in tool_deltas:
            if not isinstance(item, dict):
                raise ProtocolError("invalid_tools")
            index = item.get("index")
            if type(index) is not int or not 0 <= index < MAX_TOOLS:
                raise ProtocolError("invalid_tool_index")
            if item.get("type", "function") != "function":
                raise ProtocolError("unsupported_tool_type")
            function = item.get("function", {})
            if not isinstance(function, dict):
                raise ProtocolError("invalid_tool_function")
            current = self.tools.setdefault(index, {"name": "", "arguments": ""})
            for key in ("name", "arguments"):
                fragment = function.get(key, "")
                if not isinstance(fragment, str):
                    raise ProtocolError("invalid_tool_fragment")
                current[key] += fragment
                output_event = output_event or bool(fragment)
        if output_event and self.ttft_ms is None:
            self.ttft_ms = (time.perf_counter() - self.started) * 1000
        finish = choice.get("finish_reason")
        if finish is not None:
            if finish in {"length", "content_filter"}:
                raise ProtocolError("incomplete_completion")
            if finish not in {"stop", "tool_calls"}:
                raise ProtocolError("unsupported_finish_reason")
            if self.finished:
                raise ProtocolError("duplicate_finish_reason")
            if finish == "tool_calls" and not self.tools:
                raise ProtocolError("missing_finished_tool_calls")
            self.finished = True

    def finalize(self) -> tuple[str, list[dict[str, Any]]]:
        if not self.done or not self.finished or self.pending.strip() or self.data_lines:
            raise ProtocolError("truncated_stream")
        return "".join(self.content), _tools(
            [{"type": "function", "function": self.tools[index]} for index in sorted(self.tools)]
        )


class OpenAICompatibleClient:
    """One request at a time; no redirects, retries, proxy inheritance or logging."""

    def __init__(
        self,
        deployment: dict[str, Any],
        timeout_s: float = 30.0,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        self.endpoint = validate_endpoint(deployment.get("endpoint"))
        self.deployment = deployment
        if not finite_number(timeout_s, 0.05, 600):
            raise ValueError("timeout must be finite and between 0.05 and 600 seconds")
        if type(max_response_bytes) is not int or not 1 <= max_response_bytes <= MAX_RESPONSE_BYTES:
            raise ValueError("invalid response byte limit")
        self.timeout_s = float(timeout_s)
        self.max_response_bytes = max_response_bytes

    def complete(self, payload: dict[str, Any]) -> CompletionResult:
        started = time.perf_counter()
        deadline = started + self.timeout_s
        status: int | None = None
        digest = hashlib.sha256()
        conn: http.client.HTTPConnection | None = None
        watchdog: threading.Timer | None = None
        received = 0

        def error(kind: str) -> CompletionResult:
            return CompletionResult(
                False,
                status_code=status,
                latency_ms=round((time.perf_counter() - started) * 1000, 6),
                response_sha256=digest.hexdigest() if received else None,
                error_kind=kind,
            )

        try:
            parsed = urlsplit(self.endpoint)
            host = parsed.hostname
            if host is None:
                return error("invalid_endpoint")
            key_name = self.deployment.get("api_key_env")
            key = os.environ.get(key_name, "") if isinstance(key_name, str) else ""
            if key_name and not key:
                return error("missing_api_key")
            if key and (parsed.scheme != "https" or "\r" in key or "\n" in key):
                return error("unsafe_credential_transport")
            body = canonical_json(payload).encode("utf-8")
            if len(body) > self.max_response_bytes:
                return error("request_size_limit")
            path = parsed.path.rstrip("/")
            if not path.endswith("/chat/completions"):
                path += "/chat/completions"
            headers = {
                "Content-Type": "application/json",
                "Accept": "text/event-stream" if payload.get("stream") else "application/json",
            }
            if key:
                headers["Authorization"] = f"Bearer {key}"
            if parsed.scheme == "https":
                conn = http.client.HTTPSConnection(
                    host, parsed.port, timeout=self.timeout_s, context=ssl.create_default_context()
                )
            else:
                conn = http.client.HTTPConnection(host, parsed.port, timeout=self.timeout_s)
            conn.connect()
            sock = conn.sock
            if sock is None:
                return error("connection_failed")

            def expire() -> None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return error("deadline_exceeded")
            watchdog = threading.Timer(remaining, expire)
            watchdog.daemon = True
            watchdog.start()
            conn.request("POST", path, body, headers)
            with conn.getresponse() as response:
                status = response.status
                if 300 <= status < 400:
                    return error("redirect_rejected")
                if status != 200:
                    return error(f"http_{status}")
                encoding = (response.getheader("Content-Encoding") or "identity").lower()
                if encoding not in {"", "identity"}:
                    return error("unsupported_content_encoding")
                length = response.getheader("Content-Length")
                if length is not None and (
                    not length.isdigit() or int(length) > self.max_response_bytes
                ):
                    return error("response_size_limit")
                streaming = payload.get("stream") is True
                if streaming and not (response.getheader("Content-Type") or "").lower().startswith(
                    "text/event-stream"
                ):
                    return error("unexpected_stream_content_type")
                parser = StreamParser(started) if streaming else None
                raw = bytearray()
                while True:
                    if response.isclosed():
                        break
                    remaining = deadline - time.perf_counter()
                    if remaining <= 0:
                        raise ProtocolError("deadline_exceeded")
                    sock.settimeout(remaining)
                    chunk = response.read1(min(65536, self.max_response_bytes - received + 1))
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > self.max_response_bytes:
                        raise ProtocolError("response_size_limit")
                    digest.update(chunk)
                    if parser is not None:
                        parser.feed(chunk)
                        if parser.done:
                            break
                    else:
                        raw.extend(chunk)
                if time.perf_counter() >= deadline:
                    raise ProtocolError("deadline_exceeded")
                if parser is not None:
                    content, tools = parser.finalize()
                    usage = parser.usage
                    ttft = parser.ttft_ms
                else:
                    if length is not None and received != int(length):
                        raise ProtocolError("truncated_response")
                    decoded = strict_json(raw.decode("utf-8"))
                    if not isinstance(decoded, dict) or "error" in decoded:
                        raise ProtocolError("invalid_response_shape")
                    choices = decoded.get("choices")
                    if (
                        not isinstance(choices, list)
                        or len(choices) != 1
                        or not isinstance(choices[0], dict)
                    ):
                        raise ProtocolError("missing_choices")
                    choice = choices[0]
                    if type(choice.get("index", 0)) is not int or choice.get("index", 0) != 0:
                        raise ProtocolError("unsupported_choice_index")
                    finish = choice.get("finish_reason")
                    if finish in {"length", "content_filter"}:
                        raise ProtocolError("incomplete_completion")
                    if finish not in {"stop", "tool_calls"}:
                        raise ProtocolError("missing_or_unsupported_finish_reason")
                    message = choice.get("message")
                    if not isinstance(message, dict):
                        raise ProtocolError("missing_message")
                    content, tools = (
                        _text(message.get("content")),
                        _tools(message.get("tool_calls")),
                    )
                    if finish == "tool_calls" and not tools:
                        raise ProtocolError("missing_finished_tool_calls")
                    usage = _usage(decoded.get("usage"))
                    ttft = None  # non-streaming cannot observe first-token time
                return CompletionResult(
                    True,
                    content,
                    tools,
                    usage,
                    status,
                    round((time.perf_counter() - started) * 1000, 6),
                    round(ttft, 6) if ttft is not None else None,
                    usage.get("completion_tokens"),
                    digest.hexdigest(),
                    None,
                )
        except ProtocolError as exc:
            return error(str(exc))
        except (ValueError, UnicodeError, RecursionError):
            return error("invalid_response_or_request")
        except (TimeoutError, socket.timeout):
            return error("deadline_exceeded")
        except (OSError, http.client.HTTPException):
            return error("endpoint_io_error")
        finally:
            if watchdog is not None:
                watchdog.cancel()
            if conn is not None:
                conn.close()
