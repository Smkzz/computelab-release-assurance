"""Remote protocol errors must become recorded failures, never Python crashes."""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from computelab_release.models import canonical_json, default_contract, write_json
from computelab_release.report import verify_project
from computelab_release.runner import initialize_project, qualify, register_deployment
from computelab_release.transport import ProtocolError, StreamParser, _nonstream_response, _usage


@pytest.mark.parametrize("finish", [[], {}, True, 1])
def test_nonstream_non_string_finish_reason_is_a_protocol_error(finish: Any) -> None:
    body = canonical_json(
        {"choices": [{"message": {"content": "synthetic"}, "finish_reason": finish}]}
    ).encode("utf-8")
    with pytest.raises(ProtocolError, match="finish_reason"):
        _nonstream_response(bytearray(body))


@pytest.mark.parametrize("finish", [[], {}, True, 1])
def test_stream_non_string_finish_reason_is_a_protocol_error(finish: Any) -> None:
    body = canonical_json({"choices": [{"delta": {}, "finish_reason": finish}]})
    parser = StreamParser(time.perf_counter())
    with pytest.raises(ProtocolError, match="finish_reason"):
        parser.feed(("data: " + body + "\n\n").encode("utf-8"))


@pytest.mark.parametrize("field", ["prompt_tokens", "completion_tokens", "total_tokens"])
@pytest.mark.parametrize(
    "count", [2**63, 10**400], ids=["signed-64-bit-overflow", "float-overflow"]
)
def test_unrepresentable_usage_counts_are_rejected_before_timing(field: str, count: int) -> None:
    with pytest.raises(ProtocolError, match="invalid_usage"):
        _usage({field: count})


def test_usage_allows_nonnegative_signed_64_bit_counts() -> None:
    assert _usage({"prompt_tokens": 0, "completion_tokens": 2**63 - 1}) == {
        "prompt_tokens": 0,
        "completion_tokens": 2**63 - 1,
    }


@pytest.mark.parametrize("stream", [False, True], ids=["nonstream", "stream"])
@pytest.mark.parametrize("issue", ["finish_shape", "usage_overflow"])
def test_malformed_endpoint_becomes_verifiable_inconclusive_evidence(
    tmp_path: Path, stream: bool, issue: str
) -> None:
    choice: dict[str, Any] = {"finish_reason": [] if issue == "finish_shape" else "stop"}
    choice["delta" if stream else "message"] = {"content": '{"answer":"healthy"}'}
    tokens = 10**400 if issue == "usage_overflow" else 2
    response = {"choices": [choice], "usage": {"completion_tokens": tokens}}
    encoded = canonical_json(response).encode("utf-8")
    body = b"data: " + encoded + b"\n\ndata: [DONE]\n\n" if stream else encoded

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream" if stream else "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        root = tmp_path / "project"
        initialize_project(root)
        for role in ("baseline", "candidate"):
            register_deployment(root, role, f"http://127.0.0.1:{server.server_port}", role)
        contract = default_contract()
        contract["cases"][0]["stream"] = stream
        write_json(root / "contract.json", contract)

        receipt = qualify(root)
        assert receipt["summary"]["verdict"] == "INCONCLUSIVE"
        assert len(receipt["results"]) == 4
        assert all(row["status"] == "error" for row in receipt["results"])
        expected = (
            "invalid_usage"
            if issue == "usage_overflow"
            else ("unsupported_finish_reason" if stream else "missing_or_unsupported_finish_reason")
        )
        assert all(row["error_kind"] == expected for row in receipt["results"])
        assert verify_project(root)["valid"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
