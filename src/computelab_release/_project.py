"""Project initialization, input registration, and exclusive mutation guards."""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .models import (
    DEPLOYMENT_SCHEMA,
    PRODUCT_VERSION,
    PROJECT_SCHEMA,
    ReleaseAssuranceError,
    default_contract,
    default_environment,
    deployment_fingerprint,
    environment_fingerprint,
    read_json,
    reject_links,
    safe_path,
    utc_now,
    validate_contract,
    validate_deployment,
    validate_endpoint,
    validate_project,
    write_json,
)


def project_root(path: Path) -> Path:
    path = path.expanduser().absolute()
    reject_links(path)
    return path.resolve()


@contextmanager
def _lock(root: Path) -> Iterator[None]:
    path = safe_path(root, ".release-assurance.lock")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ReleaseAssuranceError(
            "project is locked; inspect for a live writer before removing a stale lock"
        ) from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
        yield
    finally:
        path.unlink(missing_ok=True)


def initialize_project(project_dir: Path, name: str | None = None) -> dict[str, Any]:
    root = project_root(project_dir)
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise ReleaseAssuranceError("initialization requires a new or empty directory")
    root.mkdir(parents=True, exist_ok=True)
    project = {
        "schema_version": PROJECT_SCHEMA,
        "product_version": PRODUCT_VERSION,
        "project_id": uuid.uuid4().hex,
        "name": name or root.name,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "latest_run_id": None,
    }
    validate_project(project)
    with _lock(root):
        write_json(root / "project.json", project)
        write_json(root / "contract.json", default_contract())
        for part in ("deployments", "runs"):
            safe_path(root, part).mkdir(exist_ok=True)
    return project


def _load(root: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, dict[str, Any]]]:
    project = read_json(safe_path(root, "project.json"))
    validate_project(project)
    contract = read_json(safe_path(root, "contract.json"))
    validate_contract(contract)
    deployments = {}
    for role in ("baseline", "candidate"):
        value = read_json(safe_path(root, f"deployments/{role}.json"))
        validate_deployment(value, role)
        deployments[role] = value
    return project, contract, deployments


def _assert_no_active(root: Path) -> None:
    path = safe_path(root, "runs/active.json")
    if path.exists() and read_json(path).get("status") != "complete":
        raise ReleaseAssuranceError("an incomplete run exists; do not change its inputs")


def define_contract(project_dir: Path, input_path: Path | None = None) -> dict[str, Any]:
    root = project_root(project_dir)
    validate_project(read_json(safe_path(root, "project.json")))
    contract = default_contract() if input_path is None else read_json(input_path)
    validate_contract(contract)
    with _lock(root):
        _assert_no_active(root)
        write_json(safe_path(root, "contract.json"), contract)
    return contract


def register_deployment(
    project_dir: Path,
    role: str,
    endpoint: str,
    model: str,
    api_key_env: str | None = None,
    environment_path: Path | None = None,
) -> dict[str, Any]:
    if role not in {"baseline", "candidate"}:
        raise ReleaseAssuranceError("invalid deployment role")
    root = project_root(project_dir)
    validate_project(read_json(safe_path(root, "project.json")))
    environment = (
        read_json(environment_path) if environment_path is not None else default_environment()
    )
    deployment = {
        "schema_version": DEPLOYMENT_SCHEMA,
        "role": role,
        "endpoint": validate_endpoint(endpoint),
        "model": model,
        "api_key_env": api_key_env,
        "environment": environment,
        "environment_hash": environment_fingerprint(environment),
        "created_at": utc_now(),
    }
    deployment["deployment_hash"] = deployment_fingerprint(deployment)
    validate_deployment(deployment, role)
    with _lock(root):
        _assert_no_active(root)
        other_path = safe_path(
            root, f"deployments/{'candidate' if role == 'baseline' else 'baseline'}.json"
        )
        if other_path.exists():
            other = read_json(other_path)
            if (other.get("endpoint"), other.get("model")) == (deployment["endpoint"], model):
                raise ReleaseAssuranceError(
                    "baseline and candidate must use distinct endpoint/model identities"
                )
        write_json(safe_path(root, f"deployments/{role}.json"), deployment)
    return deployment
