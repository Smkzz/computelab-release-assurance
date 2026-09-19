from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator

import pytest

from computelab_release.models import canonical_json
from computelab_release.transport import (
    MAX_LINE_BYTES,
    OpenAICompatibleClient,
    ProtocolError,
    StreamParser,
)


def event(delta: dict[str, Any], finish: str | None = None) -> bytes:
    return (
        "data: "
        + canonical_json({"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})
        + "\n\n"
    ).encode()


def test_nonstream_does_not_invent_ttft(server: str) -> None:
    result = OpenAICompatibleClient({"endpoint": server + "/baseline"}).complete({"messages": []})
    assert result.ok
    assert result.ttft_ms is None
    assert result.output_tokens == 8


def test_stream_usage_and_first_content(server: str) -> None:
    result = OpenAICompatibleClient({"endpoint": server + "/baseline"}).complete(
        {"stream": True, "messages": []}
    )
    assert result.ok and result.output_tokens == 8
    assert result.ttft_ms is not None and 0 <= result.ttft_ms <= result.latency_ms


def test_role_and_empty_events_not_first_token() -> None:
    parser = StreamParser(time.perf_counter())
    parser.feed(event({"role": "assistant"}))
    assert parser.ttft_ms is None
    parser.feed(event({"content": "hello"}))
    assert parser.ttft_ms is not None
    parser.feed(event({}, "stop") + b"data: [DONE]\n\n")
    assert parser.finalize() == ("hello", [])


def test_multibyte_split_and_tool_reconstruction() -> None:
    data = event({"content": "café"}) + event(
        {
            "tool_calls": [
                {
                    "index": 0,
                    "type": "function",
                    "function": {"name": "check", "arguments": '{"ok":'},
                }
            ]
        }
    )
    data += event({"tool_calls": [{"index": 0, "function": {"arguments": "true}"}}]})
    data += event({}, "tool_calls") + b"data: [DONE]\n\n"
    parser = StreamParser(time.perf_counter())
    for byte in data:
        parser.feed(bytes([byte]))
    content, tools = parser.finalize()
    assert content == "café" and tools[0]["function"]["arguments"] == '{"ok":true}'


@pytest.mark.parametrize(
    "data",
    [
        b"data: {}\n\n",
        b"data: nope\n\n",
        b"data: [DONE]\n\n",
        b'data: {"error":{"secret":"DO_NOT_ECHO"}}\n\n',
        b'data: {"choices":[{"index":1,"delta":{}}]}\n\n',
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":true}]}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":17}}]}\n\n',
        b'data: {"choices":[],"usage":{"completion_tokens":true}}\n\n',
    ],
)
def test_invalid_stream_fails(data: bytes) -> None:
    p = StreamParser(time.perf_counter())
    with pytest.raises(ProtocolError):
        p.feed(data)
        p.finalize()


def test_truncated_stream_rejected() -> None:
    p = StreamParser(time.perf_counter())
    p.feed(event({"content": "partial"}))
    with pytest.raises(ProtocolError, match="truncated_stream"):
        p.finalize()


def test_line_limit() -> None:
    p = StreamParser(time.perf_counter())
    with pytest.raises(ProtocolError, match="limit"):
        p.feed(b"x" * (MAX_LINE_BYTES + 1))


def test_content_after_finish_rejected() -> None:
    p = StreamParser(time.perf_counter())
    p.feed(event({}, "stop"))
    with pytest.raises(ProtocolError):
        p.feed(event({"content": "extra"}))


@contextmanager
def response_server(mode: str) -> Iterator[tuple[str, list[str]]]:
    hits: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            hits.append(self.path)
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if mode == "redirect":
                self.send_response(302)
                self.send_header("Location", "/stolen")
                self.end_headers()
                return
            self.send_response(200)
            if mode == "oversize":
                self.send_header("Content-Length", "99999999")
                self.end_headers()
                return
            if mode == "drip":
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                for _ in range(20):
                    try:
                        self.wfile.write(b" ")
                        self.wfile.flush()
                    except OSError:
                        break
                    time.sleep(0.03)
                return
            if mode == "unbounded":
                self.end_headers()
                try:
                    self.wfile.write(b"x" * 200)
                except OSError:
                    pass
                return
            payload = {
                "choices": [
                    {"message": {"content": "many words are not tokens"}, "finish_reason": "stop"}
                ]
            }
            if mode == "missing_finish":
                del payload["choices"][0]["finish_reason"]
            elif mode in {"length", "content_filter", "tool_calls", "invalid_finish"}:
                payload["choices"][0]["finish_reason"] = mode
            body = json.dumps(payload).encode()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", hits
    finally:
        server.shutdown()
        server.server_close()
        t.join(timeout=2)


def test_redirect_not_followed() -> None:
    with response_server("redirect") as (url, hits):
        r = OpenAICompatibleClient({"endpoint": url}).complete({})
        assert not r.ok and r.error_kind == "redirect_rejected"
        assert hits == ["/chat/completions"]


@pytest.mark.parametrize("mode", ["oversize", "unbounded"])
def test_response_bounded(mode: str) -> None:
    with response_server(mode) as (url, _):
        r = OpenAICompatibleClient({"endpoint": url}, max_response_bytes=100).complete({})
        assert not r.ok and r.error_kind == "response_size_limit"


def test_wall_deadline_even_when_server_drips() -> None:
    with response_server("drip") as (url, _):
        start = time.perf_counter()
        r = OpenAICompatibleClient({"endpoint": url}, timeout_s=0.1).complete({})
        assert not r.ok
        assert time.perf_counter() - start < 0.5


def test_missing_usage_does_not_become_wordcount() -> None:
    with response_server("no_usage") as (url, _):
        r = OpenAICompatibleClient({"endpoint": url}).complete({})
        assert r.ok and r.output_tokens is None and r.ttft_ms is None


def test_plaintext_bearer_never_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_API_KEY", "PRIVATE_BEARER_SENTINEL")
    with response_server("no_usage") as (url, hits):
        r = OpenAICompatibleClient({"endpoint": url, "api_key_env": "TEST_API_KEY"}).complete({})
        assert not r.ok and r.error_kind == "unsafe_credential_transport" and hits == []
        assert "PRIVATE_BEARER_SENTINEL" not in repr(r)


def test_missing_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ABSENT_API_KEY", raising=False)
    r = OpenAICompatibleClient(
        {"endpoint": "https://example.invalid", "api_key_env": "ABSENT_API_KEY"}
    ).complete({})
    assert r.error_kind == "missing_api_key"


@pytest.mark.parametrize(
    "mode", ["missing_finish", "length", "content_filter", "tool_calls", "invalid_finish"]
)
def test_incomplete_nonstream_completion_is_rejected(mode: str) -> None:
    with response_server(mode) as (url, _):
        result = OpenAICompatibleClient({"endpoint": url}).complete({})
        assert not result.ok
        assert result.error_kind in {
            "incomplete_completion",
            "missing_or_unsupported_finish_reason",
            "missing_finished_tool_calls",
        }


@pytest.mark.parametrize("finish", ["length", "content_filter", "unknown"])
def test_incomplete_stream_finish_is_rejected(finish: str) -> None:
    parser = StreamParser(time.perf_counter())
    parser.feed(event({"content": "READY"}))
    with pytest.raises(ProtocolError):
        parser.feed(event({}, finish) + b"data: [DONE]\n\n")


def test_tool_finish_requires_a_tool() -> None:
    parser = StreamParser(time.perf_counter())
    with pytest.raises(ProtocolError, match="missing_finished_tool_calls"):
        parser.feed(event({}, "tool_calls"))


def test_duplicate_stream_finish_is_rejected() -> None:
    parser = StreamParser(time.perf_counter())
    parser.feed(event({}, "stop"))
    with pytest.raises(ProtocolError, match="duplicate_finish_reason"):
        parser.feed(event({}, "stop"))
