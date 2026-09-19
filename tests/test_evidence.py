from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from computelab_release.cli import main
from computelab_release.demo import run_demo
from computelab_release.models import (
    ReleaseAssuranceError,
    canonical_json,
    default_contract,
    read_json,
    sha256_file,
    write_json,
)
from computelab_release.report import render_report, verify_project
from computelab_release.runner import evaluate_case, initialize_project, qualify
from computelab_release.transport import CompletionResult


def run_path(project: Path) -> Path:
    return project / "runs" / read_json(project / "project.json")["latest_run_id"]


def rehash(run: Path) -> None:
    manifest = read_json(run / "manifest.json")
    for name in manifest["files"]:
        manifest["files"][name] = sha256_file(run / name)
    write_json(run / "manifest.json", manifest)


def test_complete_pass_and_verification(project: Path) -> None:
    receipt = qualify(project)
    assert receipt["summary"]["verdict"] == "PASS"
    assert len(receipt["results"]) == 4
    check = verify_project(project)
    assert check["valid"]
    assert (
        verify_project(project, check["manifest_sha256"])["trust"]
        == "externally-anchored-integrity"
    )
    assert not verify_project(project, "0" * 64)["valid"]


def test_demo_all_three_results(tmp_path: Path) -> None:
    result = run_demo(tmp_path / "demo")
    assert [x["observed"] for x in result["scenarios"].values()] == ["PASS", "FAIL", "INCONCLUSIVE"]
    with pytest.raises(ReleaseAssuranceError):
        run_demo(tmp_path / "demo")
    with pytest.raises(ReleaseAssuranceError):
        initialize_project(tmp_path / "demo")
    assert (tmp_path / "demo" / "demo-summary.json").exists()


def test_resume_no_completed_request_reexecution(project: Path) -> None:
    with pytest.raises(ReleaseAssuranceError, match="checkpointed"):
        qualify(project, stop_after=1)
    run = project / "runs" / read_json(project / "runs" / "active.json")["run_id"]
    first = read_json(run / "progress.json")["results"][0]
    receipt = qualify(project, resume=True)
    assert len(receipt["results"]) == 4 and receipt["results"][0] == first
    assert verify_project(project)["valid"]


@pytest.mark.parametrize(
    "mutation", ["repeat", "timeout", "contract", "project", "jobs", "duplicate", "inflight"]
)
def test_resume_rejects_changed_state(project: Path, mutation: str) -> None:
    with pytest.raises(ReleaseAssuranceError):
        qualify(project, stop_after=1)
    run = project / "runs" / read_json(project / "runs" / "active.json")["run_id"]
    kwargs: dict[str, Any] = {"resume": True}
    if mutation == "repeat":
        kwargs["repeats"] = 3
    elif mutation == "timeout":
        kwargs["timeout_s"] = 31
    elif mutation in {"contract", "project"}:
        path = project / (mutation + ".json")
        data = read_json(path)
        data["name"] += " changed"
        write_json(path, data)
    else:
        p = read_json(run / "progress.json")
        if mutation == "jobs":
            p["jobs"] = p["jobs"][:-1]
        elif mutation == "duplicate":
            p["results"].append(copy.deepcopy(p["results"][0]))
        else:
            p["in_flight"] = "unknown-outcome"
        write_json(run / "progress.json", p)
    with pytest.raises(ReleaseAssuranceError):
        qualify(project, **kwargs)


@pytest.mark.parametrize(
    "name", ["receipt.json", "report.json", "report.md", "inputs.json", "progress.json"]
)
def test_required_file_missing_even_if_removed_from_manifest(project: Path, name: str) -> None:
    qualify(project)
    run = run_path(project)
    (run / name).unlink()
    manifest = read_json(run / "manifest.json")
    del manifest["files"][name]
    write_json(run / "manifest.json", manifest)
    assert not verify_project(project)["valid"]


@pytest.mark.parametrize(
    "mutation",
    [
        "empty",
        "duplicate",
        "swapped_roles",
        "fake_verdict",
        "fake_aggregate",
        "bad_request",
        "bad_time",
        "false_flags",
    ],
)
def test_rehashed_forgery_rejected(project: Path, mutation: str) -> None:
    qualify(project)
    run = run_path(project)
    receipt = read_json(run / "receipt.json")
    if mutation == "empty":
        receipt["results"] = []
    elif mutation == "duplicate":
        receipt["results"][1] = copy.deepcopy(receipt["results"][0])
    elif mutation == "swapped_roles":
        receipt["results"][0]["role"] = "candidate"
    elif mutation == "fake_verdict":
        receipt["summary"]["verdict"] = "FAIL"
    elif mutation == "fake_aggregate":
        receipt["summary"]["aggregates"]["baseline"]["successful_samples"] = 500
    elif mutation == "bad_request":
        receipt["results"][0]["request_sha256"] = "0" * 64
    elif mutation == "bad_time":
        receipt["results"][0]["latency_ms"] = -100
    else:
        receipt["results"][0]["compatibility_passed"] = False
    progress = read_json(run / "progress.json")
    progress["results"] = receipt["results"]
    write_json(run / "progress.json", progress)
    write_json(run / "receipt.json", receipt)
    write_json(run / "report.json", receipt)
    (run / "report.md").write_text(render_report(receipt), encoding="utf-8")
    rehash(run)
    assert not verify_project(project)["valid"]


@pytest.mark.parametrize("run_id", ["../escape", "/tmp/x", "C:\\Windows", "", "a" * 33])
def test_bad_run_pointer(project: Path, run_id: str) -> None:
    data = read_json(project / "project.json")
    data["latest_run_id"] = run_id
    write_json(project / "project.json", data)
    assert not verify_project(project)["valid"]


def test_symlink_receipt_rejected(project: Path, tmp_path: Path) -> None:
    qualify(project)
    run = run_path(project)
    target = tmp_path / "target.json"
    target.write_bytes((run / "receipt.json").read_bytes())
    (run / "receipt.json").unlink()
    try:
        (run / "receipt.json").symlink_to(target)
    except OSError:
        pytest.skip("symlink creation not permitted on this OS")
    assert not verify_project(project)["valid"]


def test_unrequested_extra_artifact_rejected(project: Path) -> None:
    qualify(project)
    (run_path(project) / "unexpected.txt").write_text("not expected")
    assert not verify_project(project)["valid"]


def test_stale_contract_invalidates_latest(project: Path) -> None:
    qualify(project)
    c = read_json(project / "contract.json")
    c["name"] = "changed"
    write_json(project / "contract.json", c)
    assert not verify_project(project)["valid"]


def test_missing_metric_is_inconclusive(project: Path) -> None:
    c = read_json(project / "contract.json")
    c["acceptance"]["max_p95_ttft_ms"] = 10000
    write_json(project / "contract.json", c)
    assert qualify(project)["summary"]["verdict"] == "INCONCLUSIVE"


def test_unknown_response_not_retained_in_validation_error() -> None:
    c = default_contract()["cases"][0]
    result = CompletionResult(
        True, '{"answer":"PRIVATE_VALUE_SENTINEL"}', [], {}, 200, 1.0, response_sha256="a" * 64
    )
    c["expected_json_schema"]["properties"]["answer"] = {"type": "integer"}
    row = evaluate_case(c, {"model": "x"}, result)
    assert not row["compatibility_passed"]
    assert "PRIVATE_VALUE_SENTINEL" not in canonical_json(row)


@pytest.mark.parametrize(
    "exact,text,passed",
    [
        (None, "null", True),
        ({"x": 1}, '{"x":1}', True),
        ({"x": 1}, '{"x":2}', False),
        (None, "not json", False),
    ],
)
def test_exact_json_without_schema(exact: object, text: str, passed: bool) -> None:
    case = {"id": "exact", "exact_json": exact}
    r = CompletionResult(True, text, [], {}, 200, 1.0, response_sha256="a" * 64)
    assert evaluate_case(case, {}, r)["compatibility_passed"] == passed


def test_report_escapes_untrusted_markup(project: Path) -> None:
    r = qualify(project)
    r["contract_name"] = "<script>alert(1)</script> [link](https://evil.invalid)\x1b[31m"
    rendered = render_report(r)
    assert "<script>" not in rendered and "\x1b" not in rendered and "[link](" not in rendered


@pytest.mark.parametrize("endpoint,expected", [("candidate", 0), ("broken", 1), ("unavailable", 3)])
def test_cli_qualify_exit_status(
    tmp_path: Path, server: str, endpoint: str, expected: int, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / endpoint
    assert main(["init", str(path)]) == 0
    assert (
        main(["register", "baseline", str(path), "--url", server + "/baseline", "--model", "x"])
        == 0
    )
    assert (
        main(["register", "candidate", str(path), "--url", server + "/" + endpoint, "--model", "x"])
        == 0
    )
    assert main(["qualify", str(path)]) == expected
    assert main(["verify", str(path)]) == 0
    assert main(["report", str(path)]) == 0
    assert main(["show", str(path)]) == 0
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("value", [0, -1, True, 101, 1.5])
def test_runtime_repeats_not_coerced(project: Path, value: object) -> None:
    with pytest.raises(ReleaseAssuranceError):
        qualify(project, repeats=value)  # type: ignore[arg-type]


def test_lock_rejects_second_writer(project: Path) -> None:
    (project / ".release-assurance.lock").write_text("999999")
    with pytest.raises(ReleaseAssuranceError, match="locked"):
        qualify(project)
    assert (project / ".release-assurance.lock").read_text() == "999999"
