from __future__ import annotations

import copy
import http.client
import runpy
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from computelab_release import cli, demo, models, report, runner, transport
from computelab_release.models import read_json, sha256_file, write_json
from computelab_release.report import render_report
from computelab_release.transport import ProtocolError


def _run_path(project: Path) -> Path:
    return project / "runs" / read_json(project / "project.json")["latest_run_id"]


def _rehash(run: Path) -> None:
    manifest = read_json(run / "manifest.json")
    manifest["files"] = {name: sha256_file(run / name) for name in manifest["files"]}
    write_json(run / "manifest.json", manifest)


def _write_receipt_bundle(run: Path, receipt: dict[str, Any], progress: dict[str, Any]) -> None:
    write_json(run / "receipt.json", receipt)
    write_json(run / "progress.json", progress)
    write_json(run / "report.json", receipt)
    (run / "report.md").write_text(render_report(receipt), encoding="utf-8")
    _rehash(run)


def test_cli_remaining_dispatch_and_error_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "cli"
    assert cli.main(["init", str(project)]) == 0
    assert cli.main(["define-contract", str(project)]) == 0
    assert cli.main(["demo", "--output", str(tmp_path / "demo")]) == 0
    assert cli._dispatch(SimpleNamespace(command="unknown")) == 2

    def fail(_: Any) -> int:
        raise ValueError("synthetic")

    monkeypatch.setattr(cli, "_dispatch", fail)
    assert cli.main(["init", str(tmp_path / "unused")]) == 2
    assert "input or filesystem operation failed" in capsys.readouterr().err


def test_module_entrypoint_executes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["computelab-release", "--version"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("computelab_release.__main__", run_name="__main__")
    assert exc.value.code == 0


def test_demo_rejects_missing_length_and_invalid_json(server: str) -> None:
    parsed = server.removeprefix("http://")
    host, port = parsed.rsplit(":", 1)

    conn = http.client.HTTPConnection(host, int(port), timeout=2)
    conn.putrequest("POST", "/baseline/chat/completions")
    conn.endheaders()
    response = conn.getresponse()
    assert response.status == 400
    response.read()
    conn.close()

    conn = http.client.HTTPConnection(host, int(port), timeout=2)
    body = b"{"
    conn.request("POST", "/baseline/chat/completions", body, {"Content-Length": str(len(body))})
    response = conn.getresponse()
    assert response.status == 400
    response.read()
    conn.close()


def test_demo_detects_internal_fixture_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(demo, "verify_project", lambda _: {"valid": False})
    with pytest.raises(models.ReleaseAssuranceError, match="synthetic demo failed"):
        demo.run_demo(tmp_path / "broken-demo")


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ("manifest_schema", "manifest_identity_invalid"),
        ("manifest_file_set", "manifest_required_file_set_mismatch"),
        ("manifest_bad_hash", "artifact_hash_mismatch"),
        ("receipt_identity", "receipt_identity_invalid"),
        ("progress_state", "progress_not_complete"),
        ("run_parameters", "run_parameters_invalid"),
        ("input_identity", "receipt_input_identity_mismatch"),
        ("deployment_identity", "receipt_deployment_identity_mismatch"),
        ("policy", "unsupported_evaluation_policy"),
        ("progress_receipt", "progress_receipt_mismatch"),
        ("decision", "decision_recomputation_mismatch"),
        ("reported_deployment", "reported_deployment_mismatch"),
        ("scope", "scope_mismatch"),
        ("json_report", "json_report_mismatch"),
        ("markdown_report", "markdown_report_mismatch"),
    ],
)
def test_verifier_rejects_each_integrity_class(project: Path, mutation: str, expected: str) -> None:
    runner.qualify(project)
    run = _run_path(project)
    receipt = read_json(run / "receipt.json")
    progress = read_json(run / "progress.json")

    if mutation == "manifest_schema":
        manifest = read_json(run / "manifest.json")
        manifest["schema_version"] = "future"
        write_json(run / "manifest.json", manifest)
    elif mutation == "manifest_file_set":
        manifest = read_json(run / "manifest.json")
        del manifest["files"]["report.md"]
        write_json(run / "manifest.json", manifest)
    elif mutation == "manifest_bad_hash":
        manifest = read_json(run / "manifest.json")
        manifest["files"]["receipt.json"] = "not-a-hash"
        write_json(run / "manifest.json", manifest)
    elif mutation == "receipt_identity":
        receipt["product_version"] = "future"
        _write_receipt_bundle(run, receipt, progress)
    elif mutation == "progress_state":
        progress["status"] = "running"
        _write_receipt_bundle(run, receipt, progress)
    elif mutation == "run_parameters":
        receipt["identity"]["repeats"] = 0
        progress["identity"] = copy.deepcopy(receipt["identity"])
        _write_receipt_bundle(run, receipt, progress)
    elif mutation == "input_identity":
        receipt["identity"]["project_hash"] = "0" * 64
        progress["identity"] = copy.deepcopy(receipt["identity"])
        _write_receipt_bundle(run, receipt, progress)
    elif mutation == "deployment_identity":
        receipt["identity"]["deployment_hashes"]["candidate"] = "0" * 64
        progress["identity"] = copy.deepcopy(receipt["identity"])
        _write_receipt_bundle(run, receipt, progress)
    elif mutation == "policy":
        receipt["identity"]["runtime"]["policy"] = "future"
        progress["identity"] = copy.deepcopy(receipt["identity"])
        _write_receipt_bundle(run, receipt, progress)
    elif mutation == "progress_receipt":
        progress["jobs"] = progress["jobs"][:-1]
        _write_receipt_bundle(run, receipt, progress)
    elif mutation == "decision":
        receipt["summary"]["verdict"] = "FAIL"
        _write_receipt_bundle(run, receipt, progress)
    elif mutation == "reported_deployment":
        receipt["deployments"]["candidate"]["model"] = "forged"
        _write_receipt_bundle(run, receipt, progress)
    elif mutation == "scope":
        receipt["scope"]["repeats"] = 999
        _write_receipt_bundle(run, receipt, progress)
    elif mutation == "json_report":
        data = read_json(run / "report.json")
        data["contract_name"] = "different"
        write_json(run / "report.json", data)
        _rehash(run)
    else:
        (run / "report.md").write_text("different", encoding="utf-8")
        _rehash(run)

    result = report.verify_project(project)
    assert result == {"valid": False, "errors": [expected]}


def test_verifier_rejects_invalid_deployment_set(project: Path) -> None:
    runner.qualify(project)
    run = _run_path(project)
    inputs = read_json(run / "inputs.json")
    del inputs["deployments"]["candidate"]
    write_json(run / "inputs.json", inputs)
    _rehash(run)
    assert report.verify_project(project)["errors"] == ["deployment_set_invalid"]


def test_verifier_masks_unexpected_exception(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import computelab_release.runner as runner_module

    monkeypatch.setattr(
        runner_module, "project_root", lambda _: (_ for _ in ()).throw(RuntimeError("private"))
    )
    assert report.verify_project(project) == {
        "valid": False,
        "errors": ["evidence_verification_failed"],
    }


@pytest.mark.parametrize(
    "value,error",
    [
        ("bad", "invalid_usage"),
        ({"completion_tokens": -1}, "invalid_usage"),
    ],
)
def test_usage_rejects_invalid_shapes(value: Any, error: str) -> None:
    with pytest.raises(ProtocolError, match=error):
        transport._usage(value)


@pytest.mark.parametrize(
    "value,error",
    [
        ("bad", "invalid_tools"),
        ([{"type": "other", "function": {}}], "unsupported_tool_type"),
        ([{"function": None}], "invalid_tool_function"),
        ([{"function": {"name": "", "arguments": "{}"}}], "invalid_tool_function"),
        ([{"function": {"name": "x", "arguments": 1}}], "invalid_tool_arguments"),
    ],
)
def test_tools_reject_invalid_shapes(value: Any, error: str) -> None:
    with pytest.raises(ProtocolError, match=error):
        transport._tools(value)


def test_stream_parser_remaining_bounds_and_shapes(monkeypatch: pytest.MonkeyPatch) -> None:
    parser = transport.StreamParser(time.perf_counter())
    with pytest.raises(ProtocolError, match="stream_line_limit"):
        parser.feed(b"x" * (transport.MAX_LINE_BYTES + 1))

    parser = transport.StreamParser(time.perf_counter())
    parser.data_lines = [b"x" * transport.MAX_LINE_BYTES]
    parser.event_bytes = transport.MAX_LINE_BYTES
    with pytest.raises(ProtocolError, match="stream_event_limit"):
        parser.feed(b"data: x\n")

    parser = transport.StreamParser(time.perf_counter())
    monkeypatch.setattr(transport, "MAX_EVENTS", 0)
    with pytest.raises(ProtocolError, match="stream_event_count_limit"):
        parser._event(b"{}")


@pytest.mark.parametrize(
    "data,error",
    [
        (b"[]", "invalid_stream_shape"),
        (b'{"choices":{}}', "missing_choices"),
        (b'{"choices":[{},{}]}', "unsupported_choices"),
        (b'{"choices":[{"delta":null}]}', "missing_delta"),
        (b'{"choices":[{"delta":{"tool_calls":{}}}]}', "invalid_tools"),
    ],
)
def test_stream_event_remaining_shapes(data: bytes, error: str) -> None:
    parser = transport.StreamParser(time.perf_counter())
    with pytest.raises(ProtocolError, match=error):
        parser._event(data)


@pytest.mark.parametrize(
    "item,error",
    [
        ("bad", "invalid_tools"),
        ({"index": -1}, "invalid_tool_index"),
        ({"index": 0, "type": "other"}, "unsupported_tool_type"),
        ({"index": 0, "function": []}, "invalid_tool_function"),
        ({"index": 0, "function": {"name": 1}}, "invalid_tool_fragment"),
    ],
)
def test_tool_delta_remaining_shapes(item: Any, error: str) -> None:
    parser = transport.StreamParser(time.perf_counter())
    with pytest.raises(ProtocolError, match=error):
        parser._tool_delta(item)


def test_data_after_done_rejected() -> None:
    parser = transport.StreamParser(time.perf_counter())
    parser.feed(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
    with pytest.raises(ProtocolError, match="data_after_done"):
        parser.feed(b"data: {}\n")


@pytest.mark.parametrize(
    "raw,error",
    [
        (b"[]", "invalid_response_shape"),
        (b'{"error":{}}', "invalid_response_shape"),
        (b'{"choices":[]}', "missing_choices"),
        (
            b'{"choices":[{"index":1,"finish_reason":"stop","message":{}}]}',
            "unsupported_choice_index",
        ),
        (b'{"choices":[{"finish_reason":"stop","message":null}]}', "missing_message"),
        (
            b'{"choices":[{"finish_reason":"tool_calls","message":{"tool_calls":[]}}]}',
            "missing_finished_tool_calls",
        ),
    ],
)
def test_nonstream_remaining_shapes(raw: bytes, error: str) -> None:
    with pytest.raises(ProtocolError, match=error):
        transport._nonstream_response(bytearray(raw))


def test_request_state_partial_hash() -> None:
    state = transport._RequestState(1)
    assert state.error("x").response_sha256 is None
    state.digest.update(b"x")
    state.received = 1
    assert state.error("x").response_sha256 == models.sha256_bytes(b"x")


def test_deadline_watchdog_expired_and_shutdown_error(monkeypatch: pytest.MonkeyPatch) -> None:
    sock = SimpleNamespace(shutdown=lambda _: (_ for _ in ()).throw(OSError()))
    with pytest.raises(ProtocolError, match="deadline_exceeded"):
        transport._deadline_watchdog(sock, time.perf_counter() - 1)

    timer = transport._deadline_watchdog(sock, time.perf_counter() + 0.01)
    timer.start()
    timer.join(1)
    assert not timer.is_alive()


def test_client_constructor_and_request_preparation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="timeout"):
        transport.OpenAICompatibleClient({"endpoint": "http://127.0.0.1"}, timeout_s=0)
    with pytest.raises(ValueError, match="response byte"):
        transport.OpenAICompatibleClient({"endpoint": "http://127.0.0.1"}, max_response_bytes=0)

    client = transport.OpenAICompatibleClient(
        {"endpoint": "http://127.0.0.1/chat/completions"}, max_response_bytes=8
    )
    with pytest.raises(ProtocolError, match="request_size_limit"):
        client._prepare_request({"long": "xxxxxxxx"})

    monkeypatch.setenv("SYNTHETIC_KEY", "secret")  # pragma: allowlist secret
    secure = transport.OpenAICompatibleClient(
        {
            "endpoint": "https://example.invalid/v1",
            "api_key_env": "SYNTHETIC_KEY",  # pragma: allowlist secret
        }  # pragma: allowlist secret
    )
    conn, path, _, headers = secure._prepare_request({})
    assert isinstance(conn, http.client.HTTPSConnection)
    assert path == "/v1/chat/completions"
    assert headers["Authorization"] == "Bearer secret"
    conn.close()


class _FakeResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        chunks: list[bytes] | None = None,
        closed: bool = False,
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self.chunks = list(chunks or [])
        self.closed = closed

    def getheader(self, name: str) -> str | None:
        return self.headers.get(name)

    def isclosed(self) -> bool:
        return self.closed

    def read1(self, _: int) -> bytes:
        if not self.chunks:
            return b""
        return self.chunks.pop(0)


class _FakeSocket:
    def settimeout(self, _: float) -> None:
        return None


@pytest.mark.parametrize(
    "response,streaming,error",
    [
        (_FakeResponse(status=500), False, "http_500"),
        (
            _FakeResponse(headers={"Content-Encoding": "gzip"}),
            False,
            "unsupported_content_encoding",
        ),
        (_FakeResponse(headers={"Content-Length": "bad"}), False, "response_size_limit"),
        (
            _FakeResponse(headers={"Content-Type": "application/json"}),
            True,
            "unexpected_stream_content_type",
        ),
    ],
)
def test_read_response_header_rejections(
    response: _FakeResponse, streaming: bool, error: str
) -> None:
    client = transport.OpenAICompatibleClient({"endpoint": "http://127.0.0.1"})
    with pytest.raises(ProtocolError, match=error):
        client._read_response(response, _FakeSocket(), streaming, transport._RequestState(1))


def test_read_response_truncated_content_length() -> None:
    client = transport.OpenAICompatibleClient({"endpoint": "http://127.0.0.1"})
    response = _FakeResponse(headers={"Content-Length": "10"}, chunks=[b'{"x":1}'])
    with pytest.raises(ProtocolError, match="truncated_response"):
        client._read_response(response, _FakeSocket(), False, transport._RequestState(1))


def test_read_body_deadline_and_closed_response(monkeypatch: pytest.MonkeyPatch) -> None:
    client = transport.OpenAICompatibleClient({"endpoint": "http://127.0.0.1"})
    state = transport._RequestState(1)
    state.deadline = time.perf_counter() - 1
    with pytest.raises(ProtocolError, match="deadline_exceeded"):
        client._read_body(_FakeResponse(), _FakeSocket(), None, state)

    state = transport._RequestState(1)
    assert client._read_body(_FakeResponse(closed=True), _FakeSocket(), None, state) == bytearray()


def test_complete_maps_socketless_and_io_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    class NoSocketConnection:
        sock = None

        def connect(self) -> None:
            return None

        def close(self) -> None:
            return None

    client = transport.OpenAICompatibleClient({"endpoint": "http://127.0.0.1"})
    monkeypatch.setattr(
        client,
        "_prepare_request",
        lambda _: (NoSocketConnection(), "/", b"{}", {}),
    )
    result = client.complete({})
    assert result.error_kind == "connection_failed"

    monkeypatch.setattr(
        client,
        "_prepare_request",
        lambda _: (_ for _ in ()).throw(OSError("synthetic")),
    )
    assert client.complete({}).error_kind == "endpoint_io_error"
