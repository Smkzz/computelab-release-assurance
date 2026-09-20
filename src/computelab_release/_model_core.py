"""Shared model vocabulary, deterministic encodings, and scalar validation."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Mapping

VERSION = "0.1.1"


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


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


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


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(quantile * len(ordered)) - 1)], 6)
