from __future__ import annotations

import copy
from pathlib import Path

import pytest

from computelab_release.models import (
    ContractValidationError,
    EvidenceValidationError,
    canonical_json,
    default_contract,
    finite_number,
    json_schema_errors,
    percentile,
    safe_path,
    strict_json,
    validate_contract,
    validate_endpoint,
)


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), -float("inf"), True, False, -1, "0", [], {}]
)
@pytest.mark.parametrize(
    "field", ["max_p95_latency_ms", "max_latency_regression_pct", "min_requests_per_second"]
)
def test_invalid_numeric_limits(value: object, field: str) -> None:
    contract = default_contract()
    contract["acceptance"][field] = value
    with pytest.raises(ContractValidationError):
        validate_contract(contract)


@pytest.mark.parametrize("value", [0, -1, True, False, 101, 1.5, "2", None])
def test_invalid_repeats(value: object) -> None:
    c = default_contract()
    c["repeats"] = value
    with pytest.raises(ContractValidationError):
        validate_contract(c)


@pytest.mark.parametrize(
    "field,value",
    [
        ("stream", "false"),
        ("safe_to_store", 1),
        ("temperature", True),
        ("temperature", -1),
        ("temperature", 3),
        ("top_p", 0),
        ("seed", True),
        ("max_tokens", 0),
        ("max_tokens", True),
        ("required_tool_calls", "tool"),
        ("expected_text_contains", "fragment"),
        ("expected_text_contains", [1]),
        ("expected_text_contains", [""]),
        ("tools", {}),
        ("misspelled_schema", {}),
        ("expected_json_schema", {"type": "nonsense"}),
        ("expected_json_schema", {"$ref": "https://example.invalid/schema"}),
        ("expected_json_schema", {"$ref": "#/definitions/test"}),
    ],
)
def test_invalid_case(field: str, value: object) -> None:
    c = default_contract()
    c["cases"][0][field] = value
    with pytest.raises(ContractValidationError):
        validate_contract(c)


@pytest.mark.parametrize(
    "identifier", ["../escape", "/root", "C:\\bad", "a:b", "", ".bad", "two words", "a\n"]
)
def test_invalid_case_ids(identifier: str) -> None:
    c = default_contract()
    c["cases"][0]["id"] = identifier
    with pytest.raises(ContractValidationError):
        validate_contract(c)


def test_duplicate_cases() -> None:
    c = default_contract()
    c["cases"].append(copy.deepcopy(c["cases"][0]))
    with pytest.raises(ContractValidationError):
        validate_contract(c)


@pytest.mark.parametrize(
    "payload",
    ['{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}', '{"a":1e999}', "[" * 70 + "0" + "]" * 70],
)
def test_ambiguous_json(payload: str) -> None:
    with pytest.raises((ValueError, RecursionError)):
        strict_json(payload)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://server/x",
        "https://u:p@host/v1",
        "https://host?key=x",
        "https://host/#key",
        "http://host\\evil",
        "http://host:bad",
        "http://host\n/x",
        "http:///missing",
    ],
)
def test_invalid_endpoint(url: str) -> None:
    with pytest.raises(ContractValidationError):
        validate_endpoint(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/v1",
        "http://[::1]:8000/v1",
        "http://10.0.0.1:8000/v1",
        "https://example.invalid/v1",
    ],
)
def test_intentional_private_endpoints_allowed(url: str) -> None:
    assert validate_endpoint(url) == url


@pytest.mark.parametrize(
    "relative",
    [
        "../file",
        "/tmp/file",
        "C:/secret",
        "C:\\secret",
        "a/../b",
        "a//b",
        "a\\b",
        "a:stream",
        "./file",
        "a/./b",
        "",
    ],
)
def test_paths_do_not_escape(tmp_path: Path, relative: str) -> None:
    with pytest.raises(EvidenceValidationError):
        safe_path(tmp_path, relative)


def test_schema_error_does_not_echo_response() -> None:
    assert "PRIVATE_RESPONSE_SENTINEL" not in str(
        json_schema_errors({"type": "integer"}, "PRIVATE_RESPONSE_SENTINEL")
    )


def test_default_contract_and_percentiles() -> None:
    validate_contract(default_contract())
    assert strict_json(canonical_json({"x": 1})) == {"x": 1}
    assert percentile([], 0.95) is None
    assert percentile([1, 2, 3, 4], 0.5) == 2
    assert finite_number(1) and not finite_number(True)


def test_nonzero_failure_allowance_rejected_not_ignored() -> None:
    c = default_contract()
    c["acceptance"]["max_failure_rate"] = 0.1
    with pytest.raises(ContractValidationError):
        validate_contract(c)


def test_huge_integer_limit_is_rejected_without_overflow() -> None:
    c = default_contract()
    c["acceptance"]["max_p95_latency_ms"] = 10**1000
    with pytest.raises(ContractValidationError):
        validate_contract(c)


def test_format_is_not_silently_ignored() -> None:
    c = default_contract()
    c["cases"][0]["expected_json_schema"] = {"type": "string", "format": "email"}
    with pytest.raises(ContractValidationError):
        validate_contract(c)


def test_json_file_limit_precedes_allocation(tmp_path, monkeypatch):
    from computelab_release import models

    monkeypatch.setattr(models, "MAX_JSON_BYTES", 64)
    path = tmp_path / "input.json"
    path.write_text('{"value":"' + "x" * 1000 + '"}', encoding="utf-8")
    with pytest.raises(models.EvidenceValidationError):
        models.read_json(path)
    path.write_text('{"value":1}', encoding="utf-8")
    assert models.read_json(path) == {"value": 1}
