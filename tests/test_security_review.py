"""Regression tests for independent review findings; all data is synthetic."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from computelab_release.cli import main
from computelab_release.models import (
    ContractValidationError,
    EvidenceValidationError,
    default_contract,
    read_bounded_bytes,
    read_json,
    sha256_file,
    strict_json,
    validate_contract,
    write_json,
)
from computelab_release.report import render_report, verify_project
from computelab_release.runner import aggregate, decide, qualify


@pytest.mark.parametrize(
    "text",
    [
        r'{"message":"\ud800"}',
        r'{"\udfff":1}',
        r'{"nested":[{"content":"\ud800"}]}',
    ],
)
def test_json_rejects_lone_surrogates_in_keys_and_values(text: str) -> None:
    with pytest.raises(ValueError, match="Unicode"):
        strict_json(text)


def test_json_accepts_valid_non_bmp_pair() -> None:
    assert strict_json(r'{"message":"\ud83d\ude00"}') == {"message": "\U0001f600"}


def test_contract_rejects_invalid_unicode_before_persistence() -> None:
    contract = default_contract()
    contract["cases"][0]["messages"][0]["content"] = "\ud800"
    with pytest.raises(ContractValidationError):
        validate_contract(contract)


def test_invalid_unicode_cli_has_no_traceback(
    project: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "invalid.json"
    path.write_text(r'{"name":"\ud800"}', encoding="utf-8")
    assert main(["define-contract", str(project), "--input", str(path)]) == 2
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert str(tmp_path) not in captured.err


def test_contradictory_malformed_flag_cannot_be_rehashed_into_pass(project: Path) -> None:
    qualify(project)
    run = project / "runs" / read_json(project / "project.json")["latest_run_id"]
    receipt = read_json(run / "receipt.json")
    inputs = read_json(run / "inputs.json")
    row = next(r for r in receipt["results"] if r["role"] == "candidate")
    row["malformed_output"] = True
    # The attacker updates every unanchored digest and aggregate. Verification
    # must still reject this contradiction rather than authenticate fabrication.
    aggregates = {role: aggregate(receipt["results"], role) for role in ("baseline", "candidate")}
    verdict, findings = decide(inputs["contract"], inputs["deployments"], aggregates)
    assert verdict == "FAIL"  # Defense in depth at decision time.
    receipt["summary"] = {"verdict": verdict, "aggregates": aggregates}
    receipt["findings"] = findings
    progress = read_json(run / "progress.json")
    progress["results"] = receipt["results"]
    write_json(run / "progress.json", progress)
    write_json(run / "receipt.json", receipt)
    write_json(run / "report.json", receipt)
    (run / "report.md").write_text(render_report(receipt), encoding="utf-8")
    manifest = read_json(run / "manifest.json")
    manifest["files"] = {name: sha256_file(run / name) for name in manifest["files"]}
    write_json(run / "manifest.json", manifest)
    result = verify_project(project)
    assert not result["valid"]
    assert result["errors"] == ["contradictory malformed-output result"]


def test_bounded_regular_file_read(tmp_path: Path) -> None:
    path = tmp_path / "bounded.txt"
    path.write_bytes(b"x" * 65)
    with pytest.raises(EvidenceValidationError):
        read_bounded_bytes(path, 64)
    path.write_bytes(b"x" * 64)
    assert read_bounded_bytes(path, 64) == b"x" * 64
    with pytest.raises(EvidenceValidationError):
        sha256_file(tmp_path)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO fixtures require POSIX")
@pytest.mark.parametrize("name", ["manifest.json", "report.md"])
def test_verification_refuses_fifo_without_blocking(project: Path, name: str) -> None:
    qualify(project)
    run = project / "runs" / read_json(project / "project.json")["latest_run_id"]
    target = run / name
    target.unlink()
    os.mkfifo(target)
    started = time.perf_counter()
    assert not verify_project(project)["valid"]
    assert time.perf_counter() - started < 1.0
