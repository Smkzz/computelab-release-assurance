"""Checkpointed run execution and identity-bound receipt finalization."""

from __future__ import annotations

import platform
import re
import sys
import uuid
from pathlib import Path
from typing import Any

from ._decision import aggregate, decide
from ._evaluation import _jobs, _request_payload, evaluate_case
from ._project import _assert_no_active, _load, _lock, project_root
from ._results import validate_results
from .models import (
    MANIFEST_SCHEMA,
    MAX_JOBS,
    PRODUCT_VERSION,
    RECEIPT_SCHEMA,
    EvidenceValidationError,
    ReleaseAssuranceError,
    contract_hash,
    finite_number,
    project_hash,
    public_endpoint,
    read_json,
    safe_path,
    sha256_file,
    utc_now,
    write_json,
    write_text,
)
from .transport import OpenAICompatibleClient

POLICY = "bounded-http-paired-v2"


def _run_dir(root: Path, run_id: Any) -> Path:
    if not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise EvidenceValidationError("invalid run identifier")
    return safe_path(root, f"runs/{run_id}")


def _runtime() -> dict[str, str]:
    return {
        "python": platform.python_version(),
        "platform": sys.platform,
        "policy": POLICY,
        "product_version": PRODUCT_VERSION,
    }


def _prepare_run(
    root: Path,
    project: dict[str, Any],
    contract: dict[str, Any],
    deployments: dict[str, dict[str, Any]],
    identity: dict[str, Any],
    jobs: list[dict[str, Any]],
    resume: bool,
) -> tuple[Path, dict[str, Any]]:
    active_path = safe_path(root, "runs/active.json")
    if resume:
        active = read_json(active_path)
        run_dir = _run_dir(root, active.get("run_id"))
        progress = read_json(safe_path(run_dir, "progress.json"))
        if progress.get("status") not in {"running", "interrupted"}:
            raise EvidenceValidationError("run is not resumable")
        if progress.get("identity") != identity or progress.get("jobs") != jobs:
            raise EvidenceValidationError("run inputs, runtime or policy changed")
        if progress.get("in_flight") is not None:
            raise EvidenceValidationError(
                "in-flight request outcome is unknown; automatic replay is forbidden"
            )
        validate_results(progress.get("results"), jobs, contract, deployments, complete=False)
    else:
        _assert_no_active(root)
        run_id = uuid.uuid4().hex
        run_dir = _run_dir(root, run_id)
        progress = {
            "schema_version": "release-assurance-progress/v2",
            "run_id": run_id,
            "status": "running",
            "identity": identity,
            "jobs": jobs,
            "results": [],
            "started_at": utc_now(),
            "in_flight": None,
        }
        run_dir.mkdir(parents=True)
        write_json(
            safe_path(run_dir, "inputs.json"),
            {"project": project, "contract": contract, "deployments": deployments},
        )
        write_json(safe_path(run_dir, "progress.json"), progress)
    write_json(active_path, {"run_id": progress["run_id"], "status": "running"})
    return run_dir, progress


def _execute_jobs(
    root: Path,
    run_dir: Path,
    progress: dict[str, Any],
    jobs: list[dict[str, Any]],
    contract: dict[str, Any],
    deployments: dict[str, dict[str, Any]],
    timeout_s: float,
    stop_after: int | None,
) -> None:
    active_path = safe_path(root, "runs/active.json")
    clients = {
        role: OpenAICompatibleClient(value, timeout_s) for role, value in deployments.items()
    }
    cases = {case["id"]: case for case in contract["cases"]}
    processed = 0
    try:
        for job in jobs[len(progress["results"]) :]:
            progress["in_flight"] = job["job_id"]
            write_json(safe_path(run_dir, "progress.json"), progress)
            case = cases[job["case_id"]]
            payload = _request_payload(case, deployments[job["role"]])
            result = clients[job["role"]].complete(payload)
            row = evaluate_case(case, payload, result)
            row.update(job)
            progress["results"].append(row)
            progress["in_flight"] = None
            write_json(safe_path(run_dir, "progress.json"), progress)
            processed += 1
            if (
                stop_after is not None
                and processed >= stop_after
                and len(progress["results"]) < len(jobs)
            ):
                progress["status"] = "interrupted"
                write_json(safe_path(run_dir, "progress.json"), progress)
                write_json(active_path, {"run_id": progress["run_id"], "status": "interrupted"})
                raise ReleaseAssuranceError("run checkpointed; resume with unchanged parameters")
    except KeyboardInterrupt:
        progress["status"] = "interrupted"
        write_json(safe_path(run_dir, "progress.json"), progress)
        write_json(active_path, {"run_id": progress["run_id"], "status": "interrupted"})
        raise ReleaseAssuranceError(
            "interrupted; inspect in-flight state before resuming"
        ) from None


def _build_receipt(
    contract: dict[str, Any],
    deployments: dict[str, dict[str, Any]],
    progress: dict[str, Any],
    identity: dict[str, Any],
    count: int,
) -> dict[str, Any]:
    aggregates = {r: aggregate(progress["results"], r) for r in deployments}
    verdict, findings = decide(contract, deployments, aggregates)
    receipt = {
        "schema_version": RECEIPT_SCHEMA,
        "product_version": PRODUCT_VERSION,
        "run_id": progress["run_id"],
        "identity": identity,
        "deployments": {
            role: {
                "model": value["model"],
                "endpoint_origin": public_endpoint(value["endpoint"]),
            }
            for role, value in deployments.items()
        },
        "contract_name": contract["name"],
        "summary": {"verdict": verdict, "aggregates": aggregates},
        "findings": findings,
        "results": progress["results"],
        "started_at": progress["started_at"],
        "finished_at": utc_now(),
        "scope": {
            "cases": [case["id"] for case in contract["cases"]],
            "repeats": count,
            "protocol": "chat/completions",
            "measurement": "client-side sequential observations; no statistical confidence claim",
        },
    }
    return receipt


def _complete_run(
    root: Path,
    run_dir: Path,
    project: dict[str, Any],
    progress: dict[str, Any],
    receipt: dict[str, Any],
) -> None:
    from .report import render_report

    write_json(safe_path(run_dir, "receipt.json"), receipt)
    write_json(safe_path(run_dir, "report.json"), receipt)
    write_text(safe_path(run_dir, "report.md"), render_report(receipt))
    progress["status"] = "complete"
    write_json(safe_path(run_dir, "progress.json"), progress)
    files = {
        name: sha256_file(safe_path(run_dir, name))
        for name in ("inputs.json", "progress.json", "receipt.json", "report.json", "report.md")
    }
    write_json(
        safe_path(run_dir, "manifest.json"),
        {"schema_version": MANIFEST_SCHEMA, "run_id": progress["run_id"], "files": files},
    )
    project["latest_run_id"] = progress["run_id"]
    project["updated_at"] = utc_now()
    write_json(safe_path(root, "project.json"), project)
    write_json(
        safe_path(root, "runs/active.json"), {"run_id": progress["run_id"], "status": "complete"}
    )


def _validate_runtime_options(timeout_s: float, stop_after: int | None) -> None:
    if not finite_number(timeout_s, 0.05, 600):
        raise ReleaseAssuranceError("timeout must be finite and between 0.05 and 600 seconds")
    if stop_after is not None and (type(stop_after) is not int or stop_after < 1):
        raise ReleaseAssuranceError("stop-after must be a positive integer")


def _repeat_count(contract: dict[str, Any], repeats: int | None, max_jobs: int) -> int:
    count = contract.get("repeats", 1) if repeats is None else repeats
    if (
        type(count) is not int
        or not 1 <= count <= 100
        or count * len(contract["cases"]) * 2 > max_jobs
    ):
        raise ReleaseAssuranceError("invalid repeats or request budget")
    return count


def _assert_inputs_unchanged(root: Path, identity: dict[str, Any]) -> None:
    """Refuse input mutation during the experiment, even by another tool."""
    current_project, current_contract, current_deployments = _load(root)
    if (
        project_hash(current_project) != identity["project_hash"]
        or contract_hash(current_contract) != identity["contract_hash"]
        or {r: d["deployment_hash"] for r, d in current_deployments.items()}
        != identity["deployment_hashes"]
    ):
        raise EvidenceValidationError("project inputs changed during qualification")


def qualify(
    project_dir: Path,
    *,
    repeats: int | None = None,
    timeout_s: float = 30.0,
    resume: bool = False,
    stop_after: int | None = None,
    max_jobs: int = MAX_JOBS,
) -> dict[str, Any]:
    root = project_root(project_dir)
    _validate_runtime_options(timeout_s, stop_after)
    with _lock(root):
        project, contract, deployments = _load(root)
        if (deployments["baseline"]["endpoint"], deployments["baseline"]["model"]) == (
            deployments["candidate"]["endpoint"],
            deployments["candidate"]["model"],
        ):
            raise EvidenceValidationError("baseline and candidate are identical")
        count = _repeat_count(contract, repeats, max_jobs)
        jobs = _jobs(contract, count)
        identity = {
            "project_hash": project_hash(project),
            "contract_hash": contract_hash(contract),
            "deployment_hashes": {
                role: value["deployment_hash"] for role, value in deployments.items()
            },
            "repeats": count,
            "timeout_s": timeout_s,
            "runtime": _runtime(),
        }
        run_dir, progress = _prepare_run(
            root, project, contract, deployments, identity, jobs, resume
        )
        _execute_jobs(root, run_dir, progress, jobs, contract, deployments, timeout_s, stop_after)
        validate_results(progress["results"], jobs, contract, deployments)
        _assert_inputs_unchanged(root, identity)
        receipt = _build_receipt(contract, deployments, progress, identity, count)
        _complete_run(root, run_dir, project, progress, receipt)
        return receipt


def load_latest_receipt(project_dir: Path) -> dict[str, Any]:
    from .report import verify_project

    result = verify_project(project_dir)
    if not result["valid"]:
        raise EvidenceValidationError("latest evidence did not verify")
    root = project_root(project_dir)
    project = read_json(safe_path(root, "project.json"))
    return read_json(safe_path(_run_dir(root, project["latest_run_id"]), "receipt.json"))
