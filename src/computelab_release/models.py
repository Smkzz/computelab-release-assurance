"""Public model facade and bounded local JSON persistence.

Private validation modules depend only on shared primitives, never on this facade.
Contracts are local operator input, not an untrusted remote configuration API.
"""

from __future__ import annotations

import json
import math
import os
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Mapping

from ._contract import METRIC_LIMITS as METRIC_LIMITS
from ._contract import _validate_acceptance as _validate_acceptance
from ._contract import _validate_contract_environment as _validate_contract_environment
from ._contract import _validate_contract_structure
from ._contract import contract_hash as contract_hash
from ._contract import default_contract as default_contract
from ._contract import json_schema_errors as json_schema_errors
from ._contract_cases import _validate_case as _validate_case
from ._contract_cases import _validate_case_options as _validate_case_options
from ._contract_cases import _validate_messages as _validate_messages
from ._contract_cases import _validate_output_expectations as _validate_output_expectations
from ._contract_cases import _validate_required_tools as _validate_required_tools
from ._contract_cases import _validate_schema as _validate_schema
from ._deployment import _contains_secret_fields as _contains_secret_fields
from ._deployment import default_environment as default_environment
from ._deployment import deployment_fingerprint as deployment_fingerprint
from ._deployment import environment_fingerprint as environment_fingerprint
from ._deployment import project_hash as project_hash
from ._deployment import public_endpoint as public_endpoint
from ._deployment import validate_deployment as validate_deployment
from ._deployment import validate_endpoint as validate_endpoint
from ._deployment import validate_project as validate_project
from ._model_core import CONTRACT_SCHEMA as CONTRACT_SCHEMA
from ._model_core import DEPLOYMENT_SCHEMA as DEPLOYMENT_SCHEMA
from ._model_core import MANIFEST_SCHEMA as MANIFEST_SCHEMA
from ._model_core import MAX_CASES as MAX_CASES
from ._model_core import MAX_JOBS as MAX_JOBS
from ._model_core import MAX_JSON_BYTES as MAX_JSON_BYTES
from ._model_core import PRODUCT_VERSION as PRODUCT_VERSION
from ._model_core import PROJECT_SCHEMA as PROJECT_SCHEMA
from ._model_core import RECEIPT_SCHEMA as RECEIPT_SCHEMA
from ._model_core import VERSION as VERSION
from ._model_core import ContractValidationError as ContractValidationError
from ._model_core import EvidenceValidationError as EvidenceValidationError
from ._model_core import ReleaseAssuranceError as ReleaseAssuranceError
from ._model_core import _keys as _keys
from ._model_core import _nonfinite as _nonfinite
from ._model_core import _pairs as _pairs
from ._model_core import canonical_json as canonical_json
from ._model_core import finite_number as finite_number
from ._model_core import percentile as percentile
from ._model_core import pretty_json as pretty_json
from ._model_core import require_string as require_string
from ._model_core import sha256_bytes as sha256_bytes
from ._model_core import sha256_json as sha256_json


def _validate_json_scalar(item: Any) -> None:
    if isinstance(item, float) and not math.isfinite(item):
        raise ValueError("non-finite JSON number")
    if isinstance(item, str) and any(0xD800 <= ord(c) <= 0xDFFF for c in item):
        raise ValueError("JSON contains invalid Unicode scalar values")


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
        _validate_json_scalar(item)
        if isinstance(item, dict):
            stack.extend((v, depth + 1) for v in item.values())
            stack.extend((key, depth + 1) for key in item)
        elif isinstance(item, list):
            stack.extend((v, depth + 1) for v in item)
    return value


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


def _validate_relative_path(relative: str) -> None:
    if not isinstance(relative, str) or not relative or "\\" in relative or "\x00" in relative:
        raise EvidenceValidationError("invalid evidence path")
    parts = relative.split("/")
    if any(p in {"", ".", ".."} for p in parts) or ":" in relative:
        raise EvidenceValidationError("invalid evidence path")
    if Path(relative).is_absolute() or PureWindowsPath(relative).drive:
        raise EvidenceValidationError("absolute evidence path is forbidden")


def safe_path(root: Path, relative: str) -> Path:
    _validate_relative_path(relative)
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


def validate_contract(contract: Mapping[str, Any]) -> None:
    try:
        strict_json(canonical_json(dict(contract)))
    except (ValueError, TypeError, RecursionError) as exc:
        raise ContractValidationError("contract must be bounded finite JSON") from exc
    _validate_contract_structure(contract, MAX_CASES, MAX_JOBS)
