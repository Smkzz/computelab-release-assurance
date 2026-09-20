"""Contract case inputs, output expectations, and inline schema validation."""

from __future__ import annotations

import re
from typing import Any, Mapping

from ._model_core import (
    ContractValidationError,
    _keys,
    finite_number,
    require_string,
)


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


def _validate_messages(case: Mapping[str, Any]) -> None:
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


def _validate_sampling_options(case: Mapping[str, Any]) -> None:
    if "temperature" in case and not finite_number(case["temperature"], 0, 2):
        raise ContractValidationError("temperature must be finite and between 0 and 2")
    if "top_p" in case and not (finite_number(case["top_p"], 0, 1) and case["top_p"] > 0):
        raise ContractValidationError("top_p must be in (0,1]")
    if "seed" in case and type(case["seed"]) is not int:
        raise ContractValidationError("seed must be an integer")


def _validate_case_options(case: Mapping[str, Any]) -> None:
    for flag in ("stream", "safe_to_store"):
        if flag in case and type(case[flag]) is not bool:
            raise ContractValidationError(f"{flag} must be boolean")
    _validate_sampling_options(case)
    if "max_tokens" in case and (
        type(case["max_tokens"]) is not int or not 1 <= case["max_tokens"] <= 32768
    ):
        raise ContractValidationError("max_tokens must be an integer from 1 to 32768")


def _validate_output_expectations(case: Mapping[str, Any]) -> None:
    if case.get("expected_json_schema") is not None:
        _validate_schema(case["expected_json_schema"])
    fragments = case.get("expected_text_contains", [])
    if (
        not isinstance(fragments, list)
        or len(fragments) > 100
        or any(not isinstance(x, str) or not x for x in fragments)
    ):
        raise ContractValidationError("expected_text_contains must contain non-empty strings")


def _validate_required_tools(case: Mapping[str, Any]) -> None:
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


def _validate_case(case: Any, seen: set[str]) -> None:
    if not isinstance(case, dict):
        raise ContractValidationError("case must be an object")
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
    _keys(case, allowed, "case")
    case_id = require_string(case.get("id"), "case.id", 80)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", case_id) or case_id in seen:
        raise ContractValidationError("case identifiers must be unique portable identifiers")
    seen.add(case_id)
    _validate_messages(case)
    _validate_case_options(case)
    _validate_output_expectations(case)
    _validate_required_tools(case)
    if "tools" in case and (not isinstance(case["tools"], list) or len(case["tools"]) > 32):
        raise ContractValidationError("tools must be a bounded list")
