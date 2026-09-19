"""Loopback-only synthetic demo; no model, API credentials or external network."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .models import ReleaseAssuranceError, canonical_json, default_contract, strict_json, write_json
from .report import verify_project
from .runner import define_contract, initialize_project, project_root, qualify, register_deployment


class DemoHandler(BaseHTTPRequestHandler):
    """A deliberately small local test server, never a production endpoint."""

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_POST(self) -> None:
        length = self.headers.get("Content-Length", "")
        if not length.isdigit() or int(length) > 65536:
            self.send_error(400)
            return
        try:
            payload = strict_json(self.rfile.read(int(length)))
        except (ValueError, UnicodeError, RecursionError):
            self.send_error(400)
            return
        if self.path == "/unavailable/chat/completions":
            self.send_error(503)
            return
        content = '{"answer":17}' if self.path.startswith("/broken/") else '{"answer":"healthy"}'
        message = {"role": "assistant", "content": content}
        usage = {"completion_tokens": 8, "prompt_tokens": 12, "total_tokens": 20}
        if payload.get("stream"):
            events = [
                {"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                {"choices": [], "usage": usage},
            ]
            body = (
                "".join("data: " + canonical_json(event) + "\n\n" for event in events)
                + "data: [DONE]\n\n"
            ).encode()
            media = "text/event-stream"
        else:
            body = canonical_json(
                {
                    "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                    "usage": usage,
                }
            ).encode()
            media = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", media)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_demo(output: Path) -> dict[str, Any]:
    root = project_root(output)
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise ReleaseAssuranceError(
            "demo output must be new or empty; no --force deletion is supported"
        )
    root.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), DemoHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    summary: dict[str, Any] = {"synthetic": True, "external_network": False, "scenarios": {}}
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        for name, endpoint, expected in (
            ("compatible", "candidate", "PASS"),
            ("regression", "broken", "FAIL"),
            ("unavailable", "unavailable", "INCONCLUSIVE"),
        ):
            project = root / name
            initialize_project(project, "Synthetic " + name)
            contract = default_contract()
            stream_case = dict(contract["cases"][0])
            stream_case.update(id="streaming-json", stream=True)
            contract["cases"].append(stream_case)
            write_json(root / "demo-contract.json", contract)
            define_contract(project, root / "demo-contract.json")
            register_deployment(project, "baseline", url + "/baseline", "synthetic-demo")
            register_deployment(project, "candidate", url + "/" + endpoint, "synthetic-demo")
            receipt = qualify(project)
            verified = verify_project(project)
            if receipt["summary"]["verdict"] != expected or not verified["valid"]:
                raise ReleaseAssuranceError("synthetic demo failed its expected-result assertion")
            summary["scenarios"][name] = {
                "expected": expected,
                "observed": receipt["summary"]["verdict"],
                "verified": verified["valid"],
                "manifest_sha256": verified["manifest_sha256"],
                "result_rows": len(receipt["results"]),
            }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    write_json(root / "demo-summary.json", summary)
    return summary
