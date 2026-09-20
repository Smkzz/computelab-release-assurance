"""Contract structure, acceptance policy, defaults, and value-free schema errors."""

from __future__ import annotations

from typing import Any, Mapping

from ._contract_cases import _validate_case
from ._model_core import (
    CONTRACT_SCHEMA,
    ContractValidationError,
    _keys,
    finite_number,
    require_string,
    sha256_json,
)

METRIC_LIMITS = {
    "max_latency_regression_pct": "latency_ms",
    "max_ttft_regression_pct": "ttft_ms",
    "max_tpot_regression_pct": "tpot_ms",
    "max_p95_latency_ms": "latency_ms",
    "max_p95_ttft_ms": "ttft_ms",
    "max_p95_tpot_ms": "tpot_ms",
}


def contract_hash(contract: Mapping[str, Any]) -> str:
    return sha256_json(dict(contract))


def _validate_acceptance(acceptance: Any, samples: int) -> None:
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
            if type(value) is not int or not 1 <= value <= samples:
                raise ContractValidationError("min_samples exceeds available samples")
        elif key in {"max_failure_rate", "max_malformed_output_rate"}:
            if not finite_number(value, 0, 0):
                raise ContractValidationError(
                    "v0.1 requires zero failed compatibility checks; nonzero failure allowances are unsupported"
                )
        elif not finite_number(value):
            raise ContractValidationError("acceptance limits must be finite nonnegative numbers")


def _validate_contract_environment(environment: Any) -> None:
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


def _validate_contract_structure(
    contract: Mapping[str, Any], max_cases: int, max_jobs: int
) -> None:
    _keys(
        contract,
        {"schema_version", "name", "description", "cases", "repeats", "acceptance", "environment"},
        "contract",
    )
    if contract.get("schema_version") != CONTRACT_SCHEMA:
        raise ContractValidationError("unsupported contract schema")
    require_string(contract.get("name"), "contract.name")
    cases = contract.get("cases")
    if not isinstance(cases, list) or not 1 <= len(cases) <= max_cases:
        raise ContractValidationError("contract must contain 1 to 1000 cases")
    repeats = contract.get("repeats", 1)
    if type(repeats) is not int or not 1 <= repeats <= 100 or len(cases) * repeats * 2 > max_jobs:
        raise ContractValidationError("invalid repeats or request budget")
    seen: set[str] = set()
    for case in cases:
        _validate_case(case, seen)
    _validate_acceptance(contract.get("acceptance", {}), len(cases) * repeats)
    _validate_contract_environment(contract.get("environment", {}))


def json_schema_errors(schema: Mapping[str, Any], value: Any) -> list[str]:
    """Return value-free error codes; model text must not leak via validators."""
    import itertools

    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(dict(schema))
    return [
        f"json_schema:{error.validator}"
        for error in itertools.islice(validator.iter_errors(value), 20)
    ]


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
