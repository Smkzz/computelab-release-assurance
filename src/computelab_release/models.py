"""Validated contracts, deterministic JSON, and bounded local persistence.

This module is derived from Computelab's release-assurance implementation.
Contracts are local operator input, not an untrusted remote configuration API.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Mapping
from urllib.parse import urlsplit

VERSION = "0.1.0"
PRODUCT_VERSION = VERSION
PROJECT_SCHEMA = "release-assurance-project/v1"
DEPLOYMENT_SCHEMA = "release-assurance-deployment/v1"
CONTRACT_SCHEMA = "release-assurance-contract/v1"
RECEIPT_SCHEMA = "release-assurance-receipt/v1"
MANIFEST_SCHEMA = "release-assurance-manifest/v1"
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_CASES = 1000
MAX_JOBS = 10000


class ReleaseAssuranceError(Exception):
    """An expected, safe-to-display product error."""


class ContractValidationError(ReleaseAssuranceError):
    """The requested contract cannot be evaluated unambiguously."""


class EvidenceValidationError(ReleaseAssuranceError):
    """Evidence is missing, stale, or inconsistent."""


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def pretty_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate JSON member")
        obj[key] = value
    return obj


def _nonfinite(_: str) -> None:
    raise ValueError("non-finite JSON number")


def strict_json(text: str | bytes) -> Any:
    """Reject ambiguous duplicate members, non-finite numbers and deep input."""
    if len(text) > MAX_JSON_BYTES:
        raise ValueError("JSON exceeds size limit")
    value = json.loads(text, object_pairs_hook=_pairs, parse_constant=_nonfinite)
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if depth > 64:
            raise ValueError("JSON exceeds nesting limit")
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError("non-finite JSON number")
        if isinstance(item, str) and any(0xD800 <= ord(c) <= 0xDFFF for c in item):
            raise ValueError("JSON contains invalid Unicode scalar values")
        if isinstance(item, dict):
            stack.extend((v, depth + 1) for v in item.values())
            stack.extend((key, depth + 1) for key in item)
        elif isinstance(item, list):
            stack.extend((v, depth + 1) for v in item)
    return value


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def read_bounded_bytes(path: Path, limit: int = MAX_JSON_BYTES) -> bytes:
    """Read a bounded regular file; refuse links and special files before opening.

    Nonblocking/no-follow flags provide defense in depth where the OS supports
    them. This does not sandbox a concurrent malicious local filesystem owner.
    """
    reject_links(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise EvidenceValidationError("artifact is not a bounded regular file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if not stat.S_ISREG(opened.st_mode) or opened.st_size > limit:
            raise EvidenceValidationError("artifact is not a bounded regular file")
        chunks: list[bytes] = []
        total = 0
        while chunk := handle.read(min(65536, limit + 1 - total)):
            total += len(chunk)
            if total > limit:
                raise EvidenceValidationError("artifact exceeds size limit")
            chunks.append(chunk)
        return b"".join(chunks)


def sha256_file(path: Path) -> str:
    """Hash one bounded regular evidence artifact, never a FIFO/device."""
    return sha256_bytes(read_bounded_bytes(path))


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def reject_links(path: Path) -> None:
    """Refuse symlinks in existing path components (not a local-user sandbox)."""
    for part in (path, *path.parents):
        if part.is_symlink():
            raise EvidenceValidationError("symlink paths are not supported")


def safe_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative or "\x00" in relative:
        raise EvidenceValidationError("invalid evidence path")
    parts = relative.split("/")
    if any(p in {"", ".", ".."} for p in parts) or ":" in relative:
        raise EvidenceValidationError("invalid evidence path")
    if Path(relative).is_absolute() or PureWindowsPath(relative).drive:
        raise EvidenceValidationError("absolute evidence path is forbidden")
    reject_links(root)
    path = root / relative
    reject_links(path)
    if not path.resolve().is_relative_to(root.resolve()):
        raise EvidenceValidationError("evidence path escapes project")
    return path


def write_text(path: Path, text: str) -> None:
    reject_links(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_json(path: Path, value: Any) -> None:
    text = pretty_json(value)
    if len(text.encode("utf-8")) > MAX_JSON_BYTES:
        raise EvidenceValidationError("JSON artifact exceeds the persistence limit")
    write_text(path, text)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = strict_json(read_bounded_bytes(path, MAX_JSON_BYTES))
    except (OSError, ValueError, RecursionError) as exc:
        raise EvidenceValidationError("cannot read valid bounded JSON artifact") from exc
    if not isinstance(value, dict):
        raise EvidenceValidationError("JSON root must be an object")
    return value


def finite_number(value: Any, low: float = 0, high: float | None = None) -> bool:
    try:
        return bool(
            type(value) in (int, float)
            and math.isfinite(value)
            and value >= low
            and (high is None or value <= high)
        )
    except OverflowError:
        return False


def require_string(value: Any, field: str, max_length: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise ContractValidationError(f"{field} must be a non-empty bounded string")
    if any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise ContractValidationError(f"{field} contains control characters")
    return value


def _keys(obj: Mapping[str, Any], allowed: set[str], field: str) -> None:
    if set(obj) - allowed:
        raise ContractValidationError(f"{field} contains unsupported fields")


def validate_endpoint(endpoint: Any) -> str:
    value = require_string(endpoint, "endpoint", 2048).rstrip("/")
    if any(c.isspace() for c in value) or "\\" in value:
        raise ContractValidationError("endpoint contains whitespace or backslashes")
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise ContractValidationError("invalid endpoint") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ContractValidationError("endpoint must be an absolute HTTP(S) URL")
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ContractValidationError("endpoint must not contain credentials, query or fragment")
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


def contract_hash(contract: Mapping[str, Any]) -> str:
    return sha256_json(dict(contract))


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
    env_key = deployment.get("api_key_env")
    if env_key is not None and (
        not isinstance(env_key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", env_key)
    ):
        raise ContractValidationError("api_key_env must name an environment variable")
    environment = deployment.get("environment")
    if not isinstance(environment, dict) or _contains_secret_fields(environment):
        raise ContractValidationError("environment must be an object without credential fields")
    if deployment.get("environment_hash") != environment_fingerprint(environment):
        raise EvidenceValidationError("environment fingerprint mismatch")
    if deployment.get("deployment_hash") != deployment_fingerprint(deployment):
        raise EvidenceValidationError("deployment fingerprint mismatch")


METRIC_LIMITS = {
    "max_latency_regression_pct": "latency_ms",
    "max_ttft_regression_pct": "ttft_ms",
    "max_tpot_regression_pct": "tpot_ms",
    "max_p95_latency_ms": "latency_ms",
    "max_p95_ttft_ms": "ttft_ms",
    "max_p95_tpot_ms": "tpot_ms",
}


def _validate_schema(schema: Any) -> None:
    if not isinstance(schema, dict):
        raise ContractValidationError("expected_json_schema must be an object")
    stack = [schema]
    while stack:
        obj = stack.pop()
        if isinstance(obj, dict):
            if any(key in obj for key in ("$ref", "$dynamicRef", "$recursiveRef")):
                raise ContractValidationError(
                    "JSON Schema references are unsupported; inline the schema"
                )
            if "format" in obj:
                raise ContractValidationError(
                    "format assertions are unsupported in v0.1; use explicit structural constraints"
                )
            stack.extend(obj.values())
        elif isinstance(obj, list):
            stack.extend(obj)
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError

    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ContractValidationError("invalid Draft 2020-12 JSON Schema") from exc


def validate_contract(contract: Mapping[str, Any]) -> None:
    try:
        strict_json(canonical_json(dict(contract)))
    except (ValueError, TypeError, RecursionError) as exc:
        raise ContractValidationError("contract must be bounded finite JSON") from exc
    _keys(
        contract,
        {"schema_version", "name", "description", "cases", "repeats", "acceptance", "environment"},
        "contract",
    )
    if contract.get("schema_version") != CONTRACT_SCHEMA:
        raise ContractValidationError("unsupported contract schema")
    require_string(contract.get("name"), "contract.name")
    cases = contract.get("cases")
    if not isinstance(cases, list) or not 1 <= len(cases) <= MAX_CASES:
        raise ContractValidationError("contract must contain 1 to 1000 cases")
    repeats = contract.get("repeats", 1)
    if type(repeats) is not int or not 1 <= repeats <= 100 or len(cases) * repeats * 2 > MAX_JOBS:
        raise ContractValidationError("invalid repeats or request budget")
    seen: set[str] = set()
    allowed = {
        "id",
        "messages",
        "temperature",
        "max_tokens",
        "stream",
        "safe_to_store",
        "expected_json_schema",
        "exact_json",
        "expected_text_contains",
        "required_tool_calls",
        "tools",
        "tool_choice",
        "response_format",
        "top_p",
        "seed",
    }
    for case in cases:
        if not isinstance(case, dict):
            raise ContractValidationError("case must be an object")
        _keys(case, allowed, "case")
        case_id = require_string(case.get("id"), "case.id", 80)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", case_id) or case_id in seen:
            raise ContractValidationError("case identifiers must be unique portable identifiers")
        seen.add(case_id)
        messages = case.get("messages")
        if not isinstance(messages, list) or not 1 <= len(messages) <= 100:
            raise ContractValidationError("case.messages must contain 1 to 100 messages")
        for message in messages:
            if not isinstance(message, dict):
                raise ContractValidationError("message must be an object")
            _keys(message, {"role", "content", "name", "tool_call_id"}, "message")
            if message.get("role") not in {"system", "user", "assistant", "tool"}:
                raise ContractValidationError("unsupported message role")
            require_string(message.get("content"), "message.content", 65536)
        for flag in ("stream", "safe_to_store"):
            if flag in case and type(case[flag]) is not bool:
                raise ContractValidationError(f"{flag} must be boolean")
        if "temperature" in case and not finite_number(case["temperature"], 0, 2):
            raise ContractValidationError("temperature must be finite and between 0 and 2")
        if "top_p" in case and not (finite_number(case["top_p"], 0, 1) and case["top_p"] > 0):
            raise ContractValidationError("top_p must be in (0,1]")
        if "seed" in case and type(case["seed"]) is not int:
            raise ContractValidationError("seed must be an integer")
        if "max_tokens" in case and (
            type(case["max_tokens"]) is not int or not 1 <= case["max_tokens"] <= 32768
        ):
            raise ContractValidationError("max_tokens must be an integer from 1 to 32768")
        if case.get("expected_json_schema") is not None:
            _validate_schema(case["expected_json_schema"])
        fragments = case.get("expected_text_contains", [])
        if (
            not isinstance(fragments, list)
            or len(fragments) > 100
            or any(not isinstance(x, str) or not x for x in fragments)
        ):
            raise ContractValidationError("expected_text_contains must contain non-empty strings")
        tools = case.get("required_tool_calls", [])
        if not isinstance(tools, list) or len(tools) > 32:
            raise ContractValidationError("required_tool_calls must be a bounded list")
        names: set[str] = set()
        for tool in tools:
            if not isinstance(tool, dict):
                raise ContractValidationError("required tool must be an object")
            _keys(tool, {"name", "arguments"}, "required tool")
            name = require_string(tool.get("name"), "tool.name", 128)
            if name in names:
                raise ContractValidationError("duplicate required tool")
            names.add(name)
            if "arguments" in tool and not isinstance(tool["arguments"], dict):
                raise ContractValidationError("required tool arguments must be an object")
        if "tools" in case and (not isinstance(case["tools"], list) or len(case["tools"]) > 32):
            raise ContractValidationError("tools must be a bounded list")
    acceptance = contract.get("acceptance", {})
    if not isinstance(acceptance, dict):
        raise ContractValidationError("acceptance must be an object")
    _keys(
        acceptance,
        set(METRIC_LIMITS)
        | {
            "min_samples",
            "max_failure_rate",
            "max_malformed_output_rate",
            "min_requests_per_second",
        },
        "acceptance",
    )
    for key, value in acceptance.items():
        if value is None:
            continue
        if key == "min_samples":
            if type(value) is not int or not 1 <= value <= len(cases) * repeats:
                raise ContractValidationError("min_samples exceeds available samples")
        elif key in {"max_failure_rate", "max_malformed_output_rate"}:
            if not finite_number(value, 0, 0):
                raise ContractValidationError(
                    "v0.1 requires zero failed compatibility checks; nonzero failure allowances are unsupported"
                )
        elif not finite_number(value):
            raise ContractValidationError("acceptance limits must be finite nonnegative numbers")
    environment = contract.get("environment", {})
    if not isinstance(environment, dict):
        raise ContractValidationError("environment must be an object")
    _keys(environment, {"candidate_must_match", "notes"}, "environment")
    fields = environment.get("candidate_must_match", [])
    if (
        not isinstance(fields, list)
        or len(fields) != len(set(str(f) for f in fields))
        or any(not isinstance(f, str) or not f for f in fields)
    ):
        raise ContractValidationError("candidate_must_match must be a unique list of field names")


def json_schema_errors(schema: Mapping[str, Any], value: Any) -> list[str]:
    """Return value-free error codes; model text must not leak via validators."""
    import itertools

    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(dict(schema))
    return [
        f"json_schema:{error.validator}"
        for error in itertools.islice(validator.iter_errors(value), 20)
    ]


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(quantile * len(ordered)) - 1)], 6)


def default_environment() -> dict[str, Any]:
    return {"runtime": "unknown", "runtime_version": "unknown", "model_revision": "unknown"}


def default_contract() -> dict[str, Any]:
    return {
        "schema_version": CONTRACT_SCHEMA,
        "name": "Structured-output compatibility",
        "description": "Replace this synthetic case with operator-approved test inputs.",
        "repeats": 2,
        "cases": [
            {
                "id": "structured-json-001",
                "messages": [
                    {"role": "user", "content": "Return JSON with answer equal to healthy."}
                ],
                "temperature": 0,
                "max_tokens": 64,
                "stream": False,
                "safe_to_store": False,
                "expected_json_schema": {
                    "type": "object",
                    "required": ["answer"],
                    "properties": {"answer": {"type": "string"}},
                    "additionalProperties": False,
                },
            }
        ],
        "acceptance": {"min_samples": 2, "max_failure_rate": 0, "max_malformed_output_rate": 0},
        "environment": {"candidate_must_match": []},
    }
