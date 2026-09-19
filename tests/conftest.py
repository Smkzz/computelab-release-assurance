from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import pytest

from computelab_release.demo import DemoHandler
from computelab_release.runner import initialize_project, register_deployment


@pytest.fixture
def server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), DemoHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def project(tmp_path: Path, server: str) -> Path:
    path = tmp_path / "project"
    initialize_project(path)
    register_deployment(path, "baseline", server + "/baseline", "synthetic")
    register_deployment(path, "candidate", server + "/candidate", "synthetic")
    return path
