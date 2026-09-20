"""Endpoint and deployment identity validation for local operator inputs."""

from __future__ import annotations

import re
from typing import Any, Mapping
from urllib.parse import SplitResult, urlsplit

from ._model_core import (
    DEPLOYMENT_SCHEMA,
    PROJECT_SCHEMA,
    VERSION,
    ContractValidationError,
    EvidenceValidationError,
    require_string,
    sha256_json,
)


def _validate_endpoint_authority(parsed: SplitResult) -> None:
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ContractValidationError("endpoint must be an absolute HTTP(S) URL")
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ContractValidationError("endpoint must not contain credentials, query or fragment")


def validate_endpoint(endpoint: Any) -> str:
    value = require_string(endpoint, "endpoint", 2048).rstrip("/")
    if any(c.isspace() for c in value) or "\\" in value:
        raise ContractValidationError("endpoint contains whitespace or backslashes")
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise ContractValidationError("invalid endpoint") from exc
    _validate_endpoint_authority(parsed)
    return value


def public_endpoint(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    host = parsed.hostname or "unknown"
    if ":" in host:
        host = f"[{host}]"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{host}{port}"  # never include private URL paths


def validate_project(project: Mapping[str, Any]) -> None:
    if project.get("schema_version") != PROJECT_SCHEMA or project.get("product_version") != VERSION:
        raise EvidenceValidationError("unsupported project schema/version")
    require_string(project.get("project_id"), "project_id")
    require_string(project.get("name"), "name")


def project_hash(project: Mapping[str, Any]) -> str:
    return sha256_json(
        {k: v for k, v in project.items() if k not in {"latest_run_id", "updated_at"}}
    )


def environment_fingerprint(environment: Mapping[str, Any]) -> str:
    return sha256_json(dict(environment))


def deployment_fingerprint(deployment: Mapping[str, Any]) -> str:
    return sha256_json(
        {k: v for k, v in deployment.items() if k not in {"deployment_hash", "created_at"}}
    )


def _contains_secret_fields(obj: Any) -> bool:
    if isinstance(obj, dict):
        if any(
            str(k).lower()
            in {"authorization", "api_key", "access_token", "password", "client_secret", "secret"}
            for k in obj
        ):
            return True
        return any(_contains_secret_fields(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_contains_secret_fields(v) for v in obj)
    return False


def _validate_api_key_env(env_key: Any) -> None:
    if env_key is not None and (
        not isinstance(env_key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", env_key)
    ):
        raise ContractValidationError("api_key_env must name an environment variable")


def validate_deployment(deployment: Mapping[str, Any], expected_role: str | None = None) -> None:
    if deployment.get("schema_version") != DEPLOYMENT_SCHEMA:
        raise EvidenceValidationError("unsupported deployment schema")
    role = deployment.get("role")
    if role not in {"baseline", "candidate"} or (
        expected_role is not None and role != expected_role
    ):
        raise EvidenceValidationError("deployment role mismatch")
    validate_endpoint(deployment.get("endpoint"))
    require_string(deployment.get("model"), "model")
    _validate_api_key_env(deployment.get("api_key_env"))
    environment = deployment.get("environment")
    if not isinstance(environment, dict) or _contains_secret_fields(environment):
        raise ContractValidationError("environment must be an object without credential fields")
    if deployment.get("environment_hash") != environment_fingerprint(environment):
        raise EvidenceValidationError("environment fingerprint mismatch")
    if deployment.get("deployment_hash") != deployment_fingerprint(deployment):
        raise EvidenceValidationError("deployment fingerprint mismatch")


def default_environment() -> dict[str, Any]:
    return {"runtime": "unknown", "runtime_version": "unknown", "model_revision": "unknown"}
