"""Identity, consistency, and observability validation for persisted results."""

from __future__ import annotations

import re
from typing import Any

from ._evaluation import _request_payload
from .models import EvidenceValidationError, finite_number, sha256_json


def _validate_error_codes(errors: Any) -> list[str]:
    """Reject retained values and bound the count and size of error codes."""
    if (
        not isinstance(errors, list)
        or len(errors) > 20
        or any(
            not isinstance(x, str) or not re.fullmatch(r"[a-zA-Z0-9_:.-]{1,100}", x) for x in errors
        )
    ):
        raise EvidenceValidationError("invalid value-free error codes")
    return errors


def _validate_result_status(row: dict[str, Any]) -> None:
    if row.get("status") not in {"ok", "error"}:
        raise EvidenceValidationError("invalid result status")
    if (
        type(row.get("compatibility_passed")) is not bool
        or type(row.get("malformed_output")) is not bool
    ):
        raise EvidenceValidationError("invalid result flags")
    errors = _validate_error_codes(row.get("compatibility_errors"))
    if row["compatibility_passed"] != (row["status"] == "ok" and not errors):
        raise EvidenceValidationError("contradictory compatibility result")
    if row["malformed_output"] != ("invalid_json_content" in errors):
        raise EvidenceValidationError("contradictory malformed-output result")
    if row["status"] == "error" and errors != ["transport_failure"]:
        raise EvidenceValidationError("contradictory transport-failure result")


def _validate_result_metrics(row: dict[str, Any]) -> None:
    if not finite_number(row.get("latency_ms")):
        raise EvidenceValidationError("invalid latency")
    for metric in ("ttft_ms", "tpot_ms"):
        value = row.get(metric)
        if value is not None and not finite_number(value, 0, row["latency_ms"]):
            raise EvidenceValidationError("invalid timing value")
    tokens = row.get("output_tokens")
    if tokens is not None and (type(tokens) is not int or tokens < 0):
        raise EvidenceValidationError("invalid token count")
    _validate_result_transport(row)


def _validate_result_transport(row: dict[str, Any]) -> None:
    """Check response hashes and successful transport metadata together."""
    if row.get("response_sha256") is not None and not re.fullmatch(
        r"[0-9a-f]{64}", str(row["response_sha256"])
    ):
        raise EvidenceValidationError("invalid response hash")
    if row["status"] == "ok" and (
        row.get("error_kind") is not None
        or row.get("status_code") != 200
        or row.get("response_sha256") is None
    ):
        raise EvidenceValidationError("invalid successful transport result")


def _validate_result_observability(row: dict[str, Any], case: dict[str, Any]) -> None:
    if not case.get("stream") and (
        row.get("ttft_ms") is not None or row.get("tpot_ms") is not None
    ):
        raise EvidenceValidationError("non-streaming token timings are not observable")
    if ("synthetic_response" in row or "synthetic_response_omitted" in row) and not case.get(
        "safe_to_store"
    ):
        raise EvidenceValidationError("unexpected retained response")


def validate_results(
    results: Any,
    jobs: list[dict[str, Any]],
    contract: dict[str, Any],
    deployments: dict[str, dict[str, Any]],
    complete: bool = True,
) -> None:
    if (
        not isinstance(results, list)
        or len(results) > len(jobs)
        or (complete and len(results) != len(jobs))
    ):
        raise EvidenceValidationError("result coverage does not match planned jobs")
    cases = {case["id"]: case for case in contract["cases"]}
    for row, job in zip(results, jobs, strict=False):
        _validate_result_identity(row, job)
        if row.get("request_sha256") != sha256_json(
            _request_payload(cases[job["case_id"]], deployments[job["role"]])
        ):
            raise EvidenceValidationError("request hash mismatch")
        _validate_result_status(row)
        _validate_result_metrics(row)
        _validate_result_observability(row, cases[job["case_id"]])


def _validate_result_identity(row: Any, job: dict[str, Any]) -> None:
    """Match the planned job before looking up its case or interpreting metrics."""
    if not isinstance(row, dict) or any(row.get(key) != value for key, value in job.items()):
        raise EvidenceValidationError("result identity/order mismatch")
    if type(row.get("repeat")) is not int:
        raise EvidenceValidationError("invalid repeat identity")
