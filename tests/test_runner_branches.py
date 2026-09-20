"""Deterministic decision, result-validation, and checkpoint regressions."""

import pytest

from computelab_release import models as m
from computelab_release import runner as r
from computelab_release.report import verify_project
from computelab_release.transport import CompletionResult


@pytest.fixture
def local_project(tmp_path, monkeypatch):
    root = tmp_path / "local"
    r.initialize_project(root)
    for role in ("baseline", "candidate"):
        r.register_deployment(root, role, f"http://example.invalid/{role}", "synthetic")
    monkeypatch.setattr(r.OpenAICompatibleClient, "complete", lambda self, payload: completion())
    return root


def completion(**changes):
    return CompletionResult(
        **dict(
            dict(
                ok=True,
                content='{"answer":"healthy"}',
                tool_calls=[],
                status_code=200,
                latency_ms=10.0,
                response_sha256="a" * 64,
            ),
            **changes,
        )
    )


def valid_rows():
    contract = m.default_contract()
    deployments = {
        role: {"model": "synthetic", "environment": {"runtime": "cpu"}}
        for role in ("baseline", "candidate")
    }
    jobs = r._jobs(contract, 2)
    rows = []
    for job in jobs:
        case = contract["cases"][0]
        row = r.evaluate_case(
            case, r._request_payload(case, deployments[job["role"]]), completion()
        )
        rows.append(dict(row, **job))
    return contract, deployments, jobs, rows


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"status": "pending"}, "invalid result status"),
        ({"compatibility_passed": 1}, "invalid result flags"),
        ({"malformed_output": 0}, "invalid result flags"),
        ({"compatibility_errors": "error"}, "value-free error codes"),
        ({"compatibility_errors": [1]}, "value-free error codes"),
        ({"compatibility_errors": ["private value"]}, "value-free error codes"),
        ({"compatibility_errors": ["error"] * 21}, "value-free error codes"),
        ({"status": "error", "compatibility_passed": False}, "transport-failure result"),
        ({"ttft_ms": 11}, "invalid timing"),
        ({"tpot_ms": -1}, "invalid timing"),
        ({"output_tokens": True}, "invalid token count"),
        ({"output_tokens": -1}, "invalid token count"),
        ({"response_sha256": "bad"}, "invalid response hash"),
        ({"response_sha256": None}, "successful transport"),
        ({"error_kind": "error"}, "successful transport"),
        ({"status_code": 201}, "successful transport"),
        ({"ttft_ms": 1}, "non-streaming"),
        ({"tpot_ms": 1}, "non-streaming"),
        ({"synthetic_response": {}}, "unexpected retained"),
        ({"synthetic_response_omitted": "retention_size_limit"}, "unexpected retained"),
        ({"repeat": False}, "invalid repeat identity"),
    ],
)
def test_result_validation_rejects_inconsistent_rows(changes, message):
    contract, deployments, jobs, rows = valid_rows()
    r.validate_results(rows, jobs, contract, deployments)
    rows[0].update(changes)
    with pytest.raises(m.EvidenceValidationError, match=message):
        r.validate_results(rows, jobs, contract, deployments)


@pytest.mark.parametrize("rows", [None, {}, [None] * 5, [None] * 4])
def test_result_coverage_and_shape(rows):
    contract, deployments, jobs, _ = valid_rows()
    with pytest.raises(m.EvidenceValidationError, match="coverage|identity/order"):
        r.validate_results(rows, jobs, contract, deployments)
    r.validate_results([], jobs, contract, deployments, complete=False)


@pytest.mark.parametrize(
    "arguments,expected,passed",
    [
        ('{"x":{"enabled":true,"extra":1}}', {"x": {"enabled": True}}, True),
        ('{"x":{"enabled":1}}', {"x": {"enabled": True}}, False),
        ('{"x":{"enabled":false}}', {"x": {"enabled": True}}, False),
        ('{"x":1}', {"x": {"enabled": True}}, False),
        ('{"x":2}', {"x": 1}, False),
        ('{"x":1}', {"x": 1}, True),
        ("{}", {"x": 1}, False),
        ("[]", {}, False),
        ("{", {}, False),
    ],
)
def test_required_tool_argument_matching(arguments, expected, passed):
    case = {"required_tool_calls": [{"name": "lookup", "arguments": expected}]}
    result = completion(tool_calls=[{"function": {"name": "lookup", "arguments": arguments}}])
    row = r.evaluate_case(case, {}, result)
    assert row["compatibility_passed"] is passed
    assert row["compatibility_errors"] == ([] if passed else ["required_tool_invalid_or_missing"])


def test_tools_without_argument_constraints_and_missing_name():
    case = {"required_tool_calls": [{"name": "lookup"}]}
    result = completion(tool_calls=[{"function": {"name": "other", "arguments": "{}"}}])
    assert r.evaluate_case(case, {}, result)["compatibility_errors"] == [
        "required_tool_invalid_or_missing"
    ]
    result = completion(
        tool_calls=[
            {"function": {"name": "lookup", "arguments": "{"}},
            {"function": {"name": "lookup", "arguments": "{}"}},
        ]
    )
    assert r.evaluate_case(case, {}, result)["compatibility_passed"]


def test_retention_limits_text_errors_and_stream_metrics():
    case = {"stream": True, "safe_to_store": True, "expected_text_contains": ["healthy", "absent"]}
    row = r.evaluate_case(case, {}, completion(ttft_ms=2, output_tokens=5))
    assert row["tpot_ms"] == 2
    assert row["compatibility_errors"] == ["required_text_missing"]
    assert row["synthetic_response"]["content"] == '{"answer":"healthy"}'
    row = r.evaluate_case(case, {}, completion(content="é" * 32768))
    assert row["synthetic_response_omitted"] == "retention_size_limit"
    assert "synthetic_response" not in row
    for tokens in (None, 0, 1):
        assert (
            r.evaluate_case(case, {}, completion(ttft_ms=2, output_tokens=tokens))["tpot_ms"]
            is None
        )


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("transport_errors", 1, "baseline_not_qualified"),
        ("compatibility_failures", 1, "baseline_not_qualified"),
        ("malformed_outputs", 1, "baseline_not_qualified"),
    ],
)
def test_baseline_must_qualify(field, value, reason):
    contract, deployments, _, rows = valid_rows()
    stats = {role: r.aggregate(rows, role) for role in deployments}
    stats["baseline"][field] = value
    assert r.decide(contract, deployments, stats) == ("INCONCLUSIVE", [reason])


def test_partial_candidate_transport_and_environment_identity():
    contract, deployments, _, rows = valid_rows()
    stats = {role: r.aggregate(rows, role) for role in deployments}
    stats["candidate"]["transport_errors"] = 1
    assert r.decide(contract, deployments, stats) == (
        "INCONCLUSIVE",
        ["candidate_transport_incomplete"],
    )
    stats["candidate"]["transport_errors"] = 0
    contract["environment"]["candidate_must_match"] = ["runtime"]
    assert r.decide(contract, deployments, stats) == ("PASS", [])
    for value in (None, "", "unknown", "gpu"):
        deployments["candidate"]["environment"]["runtime"] = value
        assert r.decide(contract, deployments, stats) == (
            "INCONCLUSIVE",
            ["environment_identity_unverified_or_mismatched"],
        )


@pytest.mark.parametrize(
    "limit,reference,observed,verdict,reason",
    [
        ("max_latency_regression_pct", 10, 20, "FAIL", "max_latency_regression_pct"),
        ("max_latency_regression_pct", 10, 11, "PASS", None),
        ("max_latency_regression_pct", 0, 10, "INCONCLUSIVE", "invalid_performance_reference"),
        ("max_latency_regression_pct", None, 10, "INCONCLUSIVE", "invalid_performance_reference"),
        ("max_p95_latency_ms", 10, 21, "FAIL", "max_p95_latency_ms"),
        ("max_p95_latency_ms", 10, 20, "PASS", None),
        ("max_p95_latency_ms", 10, None, "INCONCLUSIVE", "required_metric_unavailable"),
    ],
)
def test_performance_decisions(limit, reference, observed, verdict, reason):
    contract, deployments, _, rows = valid_rows()
    stats = {role: r.aggregate(rows, role) for role in deployments}
    contract["acceptance"] = {limit: 20}
    stats["baseline"]["latency_ms"]["p95"] = reference
    stats["candidate"]["latency_ms"]["p95"] = observed
    assert r.decide(contract, deployments, stats) == (verdict, [] if reason is None else [reason])


@pytest.mark.parametrize(
    "rate,verdict,findings",
    [
        (None, "INCONCLUSIVE", ["required_metric_unavailable"]),
        (1, "FAIL", ["min_requests_per_second"]),
        (2, "PASS", []),
    ],
)
def test_throughput_requirement(rate, verdict, findings):
    contract, deployments, _, rows = valid_rows()
    stats = {role: r.aggregate(rows, role) for role in deployments}
    contract["acceptance"] = {"min_requests_per_second": 2}
    stats["candidate"]["requests_per_second"] = rate
    assert r.decide(contract, deployments, stats) == (verdict, findings)
    assert r.aggregate([], "baseline")["requests_per_second"] is None


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"timeout_s": 0}, "timeout"),
        ({"timeout_s": 601}, "timeout"),
        ({"timeout_s": True}, "timeout"),
        ({"timeout_s": float("nan")}, "timeout"),
        ({"stop_after": 0}, "stop-after"),
        ({"stop_after": True}, "stop-after"),
        ({"stop_after": 1.5}, "stop-after"),
    ],
)
def test_invalid_runtime_options(tmp_path, kwargs, message):
    with pytest.raises(m.ReleaseAssuranceError, match=message):
        r.qualify(tmp_path, **kwargs)
    assert list(tmp_path.iterdir()) == []


def test_registration_and_contract_changes_around_checkpoint(local_project, tmp_path):
    root = local_project
    with pytest.raises(m.ReleaseAssuranceError, match="invalid deployment role"):
        r.register_deployment(root, "other", "http://example.invalid", "x")
    with pytest.raises(m.ReleaseAssuranceError, match="distinct endpoint/model"):
        r.register_deployment(root, "candidate", "http://example.invalid/baseline", "synthetic")
    environment = tmp_path / "environment.json"
    m.write_json(environment, {"runtime": "cpu"})
    record = r.register_deployment(
        root, "candidate", "http://example.invalid/candidate", "x", environment_path=environment
    )
    assert record["environment"] == {"runtime": "cpu"}
    r.define_contract(root)
    with pytest.raises(m.ReleaseAssuranceError, match="checkpointed"):
        r.qualify(root, stop_after=1)
    before = (root / "contract.json").read_bytes()
    for action in (
        lambda: r.define_contract(root),
        lambda: r.qualify(root),
        lambda: r.register_deployment(root, "candidate", "http://example.invalid/new", "x"),
    ):
        with pytest.raises(m.ReleaseAssuranceError, match="incomplete run"):
            action()
    assert (root / "contract.json").read_bytes() == before
    receipt = r.qualify(root, resume=True, stop_after=4)
    assert receipt["summary"]["verdict"] == "PASS"
    with pytest.raises(m.EvidenceValidationError, match="not resumable"):
        r.qualify(root, resume=True)
    r.define_contract(root)


def test_keyboard_interrupt_preserves_unknown_request(local_project, monkeypatch):
    def interrupt(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(r.OpenAICompatibleClient, "complete", interrupt)
    with pytest.raises(m.ReleaseAssuranceError, match="inspect in-flight"):
        r.qualify(local_project)
    active = m.read_json(local_project / "runs/active.json")
    progress = m.read_json(local_project / "runs" / active["run_id"] / "progress.json")
    assert active["status"] == progress["status"] == "interrupted"
    assert progress["in_flight"] == progress["jobs"][0]["job_id"]
    assert progress["results"] == []
    assert not (local_project / ".release-assurance.lock").exists()
    with pytest.raises(m.EvidenceValidationError, match="automatic replay is forbidden"):
        r.qualify(local_project, resume=True)


@pytest.mark.parametrize("artifact", ["project", "contract", "deployment"])
def test_mutation_during_run_cannot_publish_receipt(local_project, monkeypatch, artifact):
    def mutate(self, payload):
        if artifact == "deployment":
            path = local_project / "deployments/candidate.json"
            data = m.read_json(path)
            data["model"] = "changed"
            data["deployment_hash"] = m.deployment_fingerprint(data)
        else:
            path = local_project / f"{artifact}.json"
            data = m.read_json(path)
            data["name"] = "changed"
        m.write_json(path, data)
        return completion()

    monkeypatch.setattr(r.OpenAICompatibleClient, "complete", mutate)
    with pytest.raises(m.EvidenceValidationError, match="inputs changed during"):
        r.qualify(local_project)
    assert m.read_json(local_project / "project.json")["latest_run_id"] is None
    assert not list((local_project / "runs").glob("*/receipt.json"))


def test_identical_deployments_cannot_bypass_registration(local_project):
    data = m.read_json(local_project / "deployments/baseline.json")
    data["role"] = "candidate"
    data["deployment_hash"] = m.deployment_fingerprint(data)
    m.write_json(local_project / "deployments/candidate.json", data)
    with pytest.raises(m.EvidenceValidationError, match="are identical"):
        r.qualify(local_project)
    with pytest.raises(m.EvidenceValidationError, match="did not verify"):
        r.load_latest_receipt(local_project)


def test_runtime_budget_limit(local_project, monkeypatch):
    monkeypatch.setattr(r, "MAX_JOBS", 3)
    with pytest.raises(m.ReleaseAssuranceError, match="request budget"):
        r.qualify(local_project)


def test_repeated_completed_runs_remain_verifiable(local_project):
    first = r.qualify(local_project)
    second = r.qualify(local_project)
    assert first["run_id"] != second["run_id"]
    assert verify_project(local_project)["valid"]
    assert r.load_latest_receipt(local_project) == second
