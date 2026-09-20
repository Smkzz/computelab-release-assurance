"""HTTP request lifecycle, bounded response reading, and non-stream decoding."""

from __future__ import annotations

import hashlib
import http.client
import os
import socket
import ssl
import threading
import time
from typing import Any
from urllib.parse import urlsplit

from .models import canonical_json, finite_number, strict_json, validate_endpoint
from .transport import (
    MAX_RESPONSE_BYTES,
    CompletionResult,
    ProtocolError,
    StreamParser,
    _text,
    _tools,
    _usage,
)


class _RequestState:
    """Per-request timing and hash state, including partially received failures."""

    def __init__(self, timeout_s: float) -> None:
        self.started = time.perf_counter()
        self.deadline = self.started + timeout_s
        self.status: int | None = None
        self.digest = hashlib.sha256()
        self.received = 0

    def error(self, kind: str) -> CompletionResult:
        return CompletionResult(
            False,
            status_code=self.status,
            latency_ms=round((time.perf_counter() - self.started) * 1000, 6),
            response_sha256=self.digest.hexdigest() if self.received else None,
            error_kind=kind,
        )


def _deadline_watchdog(sock: socket.socket, deadline: float) -> threading.Timer:
    def expire() -> None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    remaining = deadline - time.perf_counter()
    if remaining <= 0:
        raise ProtocolError("deadline_exceeded")
    watchdog = threading.Timer(remaining, expire)
    watchdog.daemon = True
    return watchdog


def _nonstream_response(raw: bytearray) -> tuple[str, list[dict[str, Any]], dict[str, int]]:
    decoded = strict_json(raw.decode("utf-8"))
    if not isinstance(decoded, dict) or "error" in decoded:
        raise ProtocolError("invalid_response_shape")
    choices = decoded.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise ProtocolError("missing_choices")
    content, tools = _nonstream_choice(choices[0])
    usage = _usage(decoded.get("usage"))
    return content, tools, usage


def _nonstream_choice(choice: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """Validate completion status before interpreting the completed message."""
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
    return content, tools


def _check_status(status: int) -> None:
    """Reject redirects and unsuccessful responses before examining their headers."""
    if 300 <= status < 400:
        raise ProtocolError("redirect_rejected")
    if status != 200:
        raise ProtocolError(f"http_{status}")


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
        state = _RequestState(self.timeout_s)
        conn: http.client.HTTPConnection | None = None
        watchdog: threading.Timer | None = None
        try:
            conn, path, body, headers = self._prepare_request(payload)
            conn.connect()
            sock = conn.sock
            if sock is None:
                raise ProtocolError("connection_failed")
            watchdog = _deadline_watchdog(sock, state.deadline)
            watchdog.start()
            conn.request("POST", path, body, headers)
            with conn.getresponse() as response:
                return self._read_response(response, sock, payload.get("stream") is True, state)
        except ProtocolError as exc:
            return state.error(str(exc))
        except (ValueError, UnicodeError, RecursionError):
            return state.error("invalid_response_or_request")
        except (TimeoutError, socket.timeout):
            return state.error("deadline_exceeded")
        except (OSError, http.client.HTTPException):
            return state.error("endpoint_io_error")
        finally:
            if watchdog is not None:
                watchdog.cancel()
            if conn is not None:
                conn.close()

    def _prepare_request(
        self, payload: dict[str, Any]
    ) -> tuple[http.client.HTTPConnection, str, bytes, dict[str, str]]:
        parsed = urlsplit(self.endpoint)
        host = parsed.hostname
        if host is None:
            raise ProtocolError("invalid_endpoint")
        key = self._api_key(parsed.scheme)
        body = canonical_json(payload).encode("utf-8")
        if len(body) > self.max_response_bytes:
            raise ProtocolError("request_size_limit")
        path = parsed.path.rstrip("/")
        if not path.endswith("/chat/completions"):
            path += "/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if payload.get("stream") else "application/json",
        }
        if key:
            headers["Authorization"] = f"Bearer {key}"
        conn: http.client.HTTPConnection
        if parsed.scheme == "https":
            conn = http.client.HTTPSConnection(
                host, parsed.port, timeout=self.timeout_s, context=ssl.create_default_context()
            )
        else:
            conn = http.client.HTTPConnection(host, parsed.port, timeout=self.timeout_s)
        return conn, path, body, headers

    def _api_key(self, scheme: str) -> str:
        """Resolve the configured credential and validate its transport constraints."""
        key_name = self.deployment.get("api_key_env")
        key = os.environ.get(key_name, "") if isinstance(key_name, str) else ""
        if key_name and not key:
            raise ProtocolError("missing_api_key")
        if key and (scheme != "https" or "\r" in key or "\n" in key):
            raise ProtocolError("unsafe_credential_transport")
        return key

    def _read_body(
        self,
        response: http.client.HTTPResponse,
        sock: socket.socket,
        parser: StreamParser | None,
        state: _RequestState,
    ) -> bytearray:
        raw = bytearray()
        while True:
            if response.isclosed():
                break
            remaining = state.deadline - time.perf_counter()
            if remaining <= 0:
                raise ProtocolError("deadline_exceeded")
            sock.settimeout(remaining)
            chunk = response.read1(min(65536, self.max_response_bytes - state.received + 1))
            if not chunk:
                break
            state.received += len(chunk)
            if state.received > self.max_response_bytes:
                raise ProtocolError("response_size_limit")
            state.digest.update(chunk)
            if parser is not None:
                parser.feed(chunk)
                if parser.done:
                    break
            else:
                raw.extend(chunk)
        if time.perf_counter() >= state.deadline:
            raise ProtocolError("deadline_exceeded")
        return raw

    def _read_response(
        self,
        response: http.client.HTTPResponse,
        sock: socket.socket,
        streaming: bool,
        state: _RequestState,
    ) -> CompletionResult:
        state.status = response.status
        _check_status(state.status)
        length = self._response_headers(response, streaming)
        parser = StreamParser(state.started) if streaming else None
        raw = self._read_body(response, sock, parser, state)
        if parser is not None:
            content, tools = parser.finalize()
            usage = parser.usage
            ttft = parser.ttft_ms
        else:
            if length is not None and state.received != int(length):
                raise ProtocolError("truncated_response")
            content, tools, usage = _nonstream_response(raw)
            ttft = None  # non-streaming cannot observe first-token time
        return CompletionResult(
            True,
            content,
            tools,
            usage,
            state.status,
            round((time.perf_counter() - state.started) * 1000, 6),
            round(ttft, 6) if ttft is not None else None,
            usage.get("completion_tokens"),
            state.digest.hexdigest(),
            None,
        )

    def _response_headers(self, response: http.client.HTTPResponse, streaming: bool) -> str | None:
        """Check encoding, size, and stream framing before reading response bytes."""
        encoding = (response.getheader("Content-Encoding") or "identity").lower()
        if encoding not in {"", "identity"}:
            raise ProtocolError("unsupported_content_encoding")
        length = response.getheader("Content-Length")
        if length is not None and (not length.isdigit() or int(length) > self.max_response_bytes):
            raise ProtocolError("response_size_limit")
        if streaming and not (response.getheader("Content-Type") or "").lower().startswith(
            "text/event-stream"
        ):
            raise ProtocolError("unexpected_stream_content_type")
        return length
