"""Boundary and persistence regressions for operator-supplied inputs."""

import copy
from types import SimpleNamespace

import pytest

from computelab_release import models as m


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("schema_version", "future", "unsupported contract"),
        ("name", " ", "non-empty"),
        ("name", "x\x00", "control characters"),
        ("name", "x" * 513, "bounded string"),
        ("cases", [], "contain 1"),
        ("cases", {}, "contain 1"),
        ("cases", [None], "case must be an object"),
        ("acceptance", [], "acceptance must be an object"),
        ("acceptance", {"min_samples": 0}, "min_samples"),
        ("acceptance", {"min_samples": True}, "min_samples"),
        ("acceptance", {"min_samples": 3}, "min_samples"),
        ("environment", [], "environment must be an object"),
        ("environment", {"candidate_must_match": "runtime"}, "unique list"),
        ("environment", {"candidate_must_match": ["x", "x"]}, "unique list"),
        ("environment", {"candidate_must_match": [1]}, "unique list"),
        ("environment", {"candidate_must_match": [""]}, "unique list"),
    ],
)
def test_contract_structure(field, value, message):
    contract = m.default_contract()
    contract[field] = value
    with pytest.raises(m.ContractValidationError, match=message):
        m.validate_contract(contract)


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("messages", [], "1 to 100"),
        ("messages", [None], "message must be an object"),
        ("messages", [{"role": "developer", "content": "x"}], "unsupported message role"),
        ("messages", [{"role": "user", "content": "x", "extra": 1}], "unsupported fields"),
        ("expected_json_schema", [], "must be an object"),
        ("required_tool_calls", [None], "required tool must be an object"),
        ("required_tool_calls", [{"name": "x"}, {"name": "x"}], "duplicate required tool"),
        ("required_tool_calls", [{"name": "x", "arguments": []}], "arguments must be an object"),
        ("required_tool_calls", [{"name": "x", "extra": 1}], "unsupported fields"),
        ("tools", [{}] * 33, "bounded list"),
        ("expected_text_contains", ["x"] * 101, "non-empty strings"),
    ],
)
def test_case_structure(field, value, message):
    contract = m.default_contract()
    contract["cases"][0][field] = value
    with pytest.raises(m.ContractValidationError, match=message):
        m.validate_contract(contract)


def test_valid_optional_contract_boundaries():
    contract = m.default_contract()
    contract["acceptance"] = {"min_samples": None, "max_p95_latency_ms": None}
    contract["cases"][0].update(
        top_p=1,
        seed=-1,
        max_tokens=32768,
        required_tool_calls=[{"name": "lookup", "arguments": {"enabled": True}}],
        expected_json_schema={"anyOf": [{"type": "string"}, {"type": "null"}]},
    )
    m.validate_contract(contract)


def test_request_budget_and_case_limit():
    contract = m.default_contract()
    contract["cases"] *= 1001
    with pytest.raises(m.ContractValidationError, match="contain 1"):
        m.validate_contract(contract)
    contract["cases"] = contract["cases"][:51]
    contract["repeats"] = 100
    with pytest.raises(m.ContractValidationError, match="request budget"):
        m.validate_contract(contract)


def deployment():
    value = dict(
        schema_version=m.DEPLOYMENT_SCHEMA,
        role="baseline",
        endpoint="https://example.invalid/private",
        model="synthetic",
        api_key_env="_TEST_KEY1",  # pragma: allowlist secret
        environment={"devices": [{"name": "cpu"}]},
    )
    value["environment_hash"] = m.environment_fingerprint(value["environment"])
    value["deployment_hash"] = m.deployment_fingerprint(value)
    return value


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("schema_version", "future", "deployment schema"),
        ("role", "other", "role mismatch"),
        ("api_key_env", 1, "environment variable"),
        ("api_key_env", "bad-key", "environment variable"),
        ("environment", [], "without credential fields"),
        (
            "environment",
            {"nested": [{"PASSWORD": "synthetic"}]},  # pragma: allowlist secret
            "without credential fields",
        ),  # pragma: allowlist secret
        ("environment_hash", "0" * 64, "environment fingerprint"),
        ("deployment_hash", "0" * 64, "deployment fingerprint"),
    ],
)
def test_deployment_validation(field, value, message):
    record = deployment()
    record[field] = value
    with pytest.raises(m.ReleaseAssuranceError, match=message):
        m.validate_deployment(record)


def test_deployment_role_and_public_origins():
    record = deployment()
    m.validate_deployment(record, "baseline")
    with pytest.raises(m.EvidenceValidationError, match="role mismatch"):
        m.validate_deployment(record, "candidate")
    assert m.public_endpoint("http://[::1]:8080/private") == "http://[::1]:8080"
    assert m.public_endpoint("https://example.invalid/private") == "https://example.invalid"
    with pytest.raises(m.EvidenceValidationError, match="schema/version"):
        m.validate_project({})
    changed = copy.deepcopy(record)
    changed["created_at"] = "later"
    assert m.deployment_fingerprint(changed) == record["deployment_hash"]


def test_json_size_and_nonobject_roots(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "MAX_JSON_BYTES", 8)
    with pytest.raises(ValueError, match="size limit"):
        m.strict_json('"' + "x" * 8 + '"')
    path = tmp_path / "value.json"
    with pytest.raises(m.EvidenceValidationError, match="persistence limit"):
        m.write_json(path, {"x": "é" * 8})
    assert not path.exists()
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(m.EvidenceValidationError, match="root must be an object"):
        m.read_json(path)
    path.write_text("{", encoding="utf-8")
    with pytest.raises(m.EvidenceValidationError, match="cannot read valid"):
        m.read_json(path)


@pytest.mark.parametrize("stage", ["opened", "growing"])
def test_file_growth_is_bounded_after_initial_stat(tmp_path, monkeypatch, stage):
    path = tmp_path / "growing.bin"
    path.write_bytes(b"12345")
    original = m.os.fstat
    if stage == "opened":
        monkeypatch.setattr(
            m.os, "fstat", lambda fd: SimpleNamespace(st_mode=original(fd).st_mode, st_size=9)
        )
    else:
        original_lstat = type(path).lstat
        monkeypatch.setattr(
            type(path),
            "lstat",
            lambda self: SimpleNamespace(st_mode=original_lstat(self).st_mode, st_size=1),
        )
        monkeypatch.setattr(
            m.os, "fstat", lambda fd: SimpleNamespace(st_mode=original(fd).st_mode, st_size=1)
        )
    with pytest.raises(m.EvidenceValidationError, match="bounded regular|size limit"):
        m.read_bounded_bytes(path, 4)


def test_atomic_write_failure_preserves_old_file_and_removes_temporary(tmp_path, monkeypatch):
    path = tmp_path / "artifact.txt"
    path.write_text("original", encoding="utf-8")

    def fail_replace(*args):
        raise OSError("injected replace failure")

    monkeypatch.setattr(m.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected"):
        m.write_text(path, "replacement")
    assert path.read_text(encoding="utf-8") == "original"
    assert list(tmp_path.iterdir()) == [path]
