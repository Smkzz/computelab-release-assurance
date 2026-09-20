"""Bounded request construction, response evaluation, and paired job ordering."""

from __future__ import annotations

from typing import Any

from .models import canonical_json, json_schema_errors, sha256_json, strict_json
from .transport import CompletionResult


def _request_payload(case: dict[str, Any], deployment: dict[str, Any]) -> dict[str, Any]:
    payload = {"model": deployment["model"], "messages": case["messages"]}
    for field in (
        "temperature",
        "max_tokens",
        "stream",
        "tools",
        "tool_choice",
        "response_format",
        "top_p",
        "seed",
    ):
        if field in case:
            payload[field] = case[field]
    if payload.get("stream"):
        payload["stream_options"] = {"include_usage": True}
    return payload


def _match(expected: Any, actual: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            k in actual and _match(v, actual[k]) for k, v in expected.items()
        )
    if isinstance(expected, bool) or isinstance(actual, bool):
        return bool(type(expected) is type(actual) and expected == actual)
    return canonical_json(expected) == canonical_json(actual)


def _evaluate_json(case: dict[str, Any], content: str, errors: list[str]) -> bool:
    malformed = False
    if case.get("expected_json_schema") is not None or "exact_json" in case:
        try:
            parsed = strict_json(content)
        except (ValueError, UnicodeError, RecursionError):
            errors.append("invalid_json_content")
            malformed = True
        else:
            if case.get("expected_json_schema") is not None:
                errors.extend(json_schema_errors(case["expected_json_schema"], parsed))
            if "exact_json" in case and canonical_json(parsed) != canonical_json(
                case["exact_json"]
            ):
                errors.append("exact_json_mismatch")
    return malformed


def _evaluate_tools(case: dict[str, Any], result: CompletionResult, errors: list[str]) -> None:
    for required in case.get("required_tool_calls", []):
        matches = [
            x["function"]
            for x in (result.tool_calls or [])
            if x["function"]["name"] == required["name"]
        ]
        valid = False
        for call in matches:
            if _tool_arguments_match(required, call):
                valid = True
        if not valid:
            errors.append("required_tool_invalid_or_missing")


def _tool_arguments_match(required: dict[str, Any], call: dict[str, Any]) -> bool:
    """Require an object argument payload before applying subset matching."""
    try:
        arguments = strict_json(call["arguments"])
    except (ValueError, UnicodeError, RecursionError):
        return False
    return isinstance(arguments, dict) and (
        "arguments" not in required or _match(required["arguments"], arguments)
    )


def _time_per_output_token(case: dict[str, Any], result: CompletionResult) -> float | None:
    """Token timing is observable only for successful multi-token streams."""
    if (
        result.ok
        and case.get("stream")
        and result.ttft_ms is not None
        and result.output_tokens is not None
        and result.output_tokens > 1
    ):
        return round(max(0, result.latency_ms - result.ttft_ms) / (result.output_tokens - 1), 6)
    return None


def evaluate_case(
    case: dict[str, Any], payload: dict[str, Any], result: CompletionResult
) -> dict[str, Any]:
    errors: list[str] = []
    malformed = False
    if not result.ok:
        errors.append("transport_failure")
    else:
        malformed = _evaluate_json(case, result.content, errors)
        for fragment in case.get("expected_text_contains", []):
            if fragment not in result.content:
                errors.append("required_text_missing")
        _evaluate_tools(case, result, errors)
    tpot = _time_per_output_token(case, result)
    row: dict[str, Any] = {
        "status": "ok" if result.ok else "error",
        "error_kind": result.error_kind,
        "request_sha256": sha256_json(payload),
        "response_sha256": result.response_sha256,
        "latency_ms": result.latency_ms,
        "ttft_ms": result.ttft_ms,
        "tpot_ms": tpot,
        "output_tokens": result.output_tokens,
        "status_code": result.status_code,
        "compatibility_passed": result.ok and not errors,
        "malformed_output": malformed,
        "compatibility_errors": errors[:20],
    }
    # Opt-in applies to synthetic fixtures only. Credentials are never copied.
    if case.get("safe_to_store"):
        retained = {"content": result.content, "tool_calls": result.tool_calls or []}
        if len(canonical_json(retained).encode("utf-8")) <= 65536:
            row["synthetic_response"] = retained
        else:
            row["synthetic_response_omitted"] = "retention_size_limit"
    return row


def _jobs(contract: dict[str, Any], repeats: int) -> list[dict[str, Any]]:
    jobs = []
    for repeat in range(repeats):
        for index, case in enumerate(contract["cases"]):
            roles = (
                ("baseline", "candidate")
                if (repeat + index) % 2 == 0
                else ("candidate", "baseline")
            )
            for role in roles:
                jobs.append(
                    {
                        "job_id": f"{repeat}:{case['id']}:{role}",
                        "repeat": repeat,
                        "case_id": case["id"],
                        "role": role,
                    }
                )
    return jobs
