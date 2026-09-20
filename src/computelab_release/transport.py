"""Bounded, redirect-free HTTP(S) chat-completion transport.

Endpoints are explicitly chosen by the local operator. This is not an SSRF
sandbox for exposing arbitrary endpoint selection to remote users.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .models import strict_json

if TYPE_CHECKING:
    from ._http import (
        OpenAICompatibleClient as OpenAICompatibleClient,
    )
    from ._http import (
        _deadline_watchdog as _deadline_watchdog,
    )
    from ._http import (
        _nonstream_response as _nonstream_response,
    )
    from ._http import (
        _RequestState as _RequestState,
    )

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
    return [_tool(item) for item in value]


def _tool(item: Any) -> dict[str, Any]:
    """Validate one complete function call and retain only its executable fields."""
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
    return {"function": {"name": function["name"], "arguments": arguments}}


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
        self._choices(event.get("choices"))

    def _choices(self, choices: Any) -> None:
        """Accept a single output choice or an empty trailing usage event."""
        if not isinstance(choices, list):
            raise ProtocolError("missing_choices")
        if not choices:  # trailing server usage event
            return
        if len(choices) != 1 or not isinstance(choices[0], dict):
            raise ProtocolError("unsupported_choices")
        self._choice(choices[0])

    def _choice(self, choice: dict[str, Any]) -> None:
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
        self._append_delta(text, tool_deltas)
        self._finish_choice(choice)

    def _append_delta(self, text: str, tool_deltas: list[Any]) -> None:
        """Accumulate output before recording the first nonempty content time."""
        output_event = bool(text)
        if text:
            self.content.append(text)
        for item in tool_deltas:
            tool_output = self._tool_delta(item)
            output_event = output_event or tool_output
        if output_event and self.ttft_ms is None:
            self.ttft_ms = (time.perf_counter() - self.started) * 1000

    def _tool_delta(self, item: Any) -> bool:
        output_event = False
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
        return output_event

    def _finish_choice(self, choice: dict[str, Any]) -> None:
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


def __getattr__(name: str) -> Any:
    """Load HTTP exports after the protocol definitions, in either import order."""
    if name in {
        "OpenAICompatibleClient",
        "_RequestState",
        "_deadline_watchdog",
        "_nonstream_response",
    }:
        from . import _http

        return getattr(_http, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
