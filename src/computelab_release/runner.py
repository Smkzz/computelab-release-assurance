"""Bounded baseline/candidate evaluation with atomic, identity-bound checkpoints."""

from __future__ import annotations

import os
import platform
import re
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .models import (
    DEPLOYMENT_SCHEMA,
    MANIFEST_SCHEMA,
    MAX_JOBS,
    METRIC_LIMITS,
    PRODUCT_VERSION,
    PROJECT_SCHEMA,
    RECEIPT_SCHEMA,
    EvidenceValidationError,
    ReleaseAssuranceError,
    canonical_json,
    contract_hash,
    default_contract,
    default_environment,
    deployment_fingerprint,
    environment_fingerprint,
    finite_number,
    json_schema_errors,
    percentile,
    project_hash,
    public_endpoint,
    read_json,
    reject_links,
    safe_path,
    sha256_file,
    sha256_json,
    strict_json,
    utc_now,
    validate_contract,
    validate_deployment,
    validate_endpoint,
    validate_project,
    write_json,
    write_text,
)
from .transport import CompletionResult, OpenAICompatibleClient

POLICY = "bounded-http-paired-v2"


def project_root(path: Path) -> Path:
    path = path.expanduser().absolute()
    reject_links(path)
    return path.resolve()


@contextmanager
def _lock(root: Path) -> Iterator[None]:
    path = safe_path(root, ".release-assurance.lock")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ReleaseAssuranceError(
            "project is locked; inspect for a live writer before removing a stale lock"
        ) from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
        yield
    finally:
        path.unlink(missing_ok=True)


def initialize_project(project_dir: Path, name: str | None = None) -> dict[str, Any]:
    root = project_root(project_dir)
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise ReleaseAssuranceError("initialization requires a new or empty directory")
    root.mkdir(parents=True, exist_ok=True)
    project = {
        "schema_version": PROJECT_SCHEMA,
        "product_version": PRODUCT_VERSION,
        "project_id": uuid.uuid4().hex,
        "name": name or root.name,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "latest_run_id": None,
    }
    validate_project(project)
    with _lock(root):
        write_json(root / "project.json", project)
        write_json(root / "contract.json", default_contract())
        for part in ("deployments", "runs"):
            safe_path(root, part).mkdir(exist_ok=True)
    return project


def _load(root: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, dict[str, Any]]]:
    project = read_json(safe_path(root, "project.json"))
    validate_project(project)
    contract = read_json(safe_path(root, "contract.json"))
    validate_contract(contract)
    deployments = {}
    for role in ("baseline", "candidate"):
        value = read_json(safe_path(root, f"deployments/{role}.json"))
        validate_deployment(value, role)
        deployments[role] = value
    return project, contract, deployments


def _assert_no_active(root: Path) -> None:
    path = safe_path(root, "runs/active.json")
    if path.exists() and read_json(path).get("status") != "complete":
        raise ReleaseAssuranceError("an incomplete run exists; do not change its inputs")


def define_contract(project_dir: Path, input_path: Path | None = None) -> dict[str, Any]:
    root = project_root(project_dir)
    validate_project(read_json(safe_path(root, "project.json")))
    contract = default_contract() if input_path is None else read_json(input_path)
    validate_contract(contract)
    with _lock(root):
        _assert_no_active(root)
        write_json(safe_path(root, "contract.json"), contract)
    return contract


def register_deployment(
    project_dir: Path,
    role: str,
    endpoint: str,
    model: str,
    api_key_env: str | None = None,
    environment_path: Path | None = None,
) -> dict[str, Any]:
    if role not in {"baseline", "candidate"}:
        raise ReleaseAssuranceError("invalid deployment role")
    root = project_root(project_dir)
    validate_project(read_json(safe_path(root, "project.json")))
    environment = (
        read_json(environment_path) if environment_path is not None else default_environment()
    )
    deployment = {
        "schema_version": DEPLOYMENT_SCHEMA,
        "role": role,
        "endpoint": validate_endpoint(endpoint),
        "model": model,
        "api_key_env": api_key_env,
        "environment": environment,
        "environment_hash": environment_fingerprint(environment),
        "created_at": utc_now(),
    }
    deployment["deployment_hash"] = deployment_fingerprint(deployment)
    validate_deployment(deployment, role)
    with _lock(root):
        _assert_no_active(root)
        other_path = safe_path(
            root, f"deployments/{'candidate' if role == 'baseline' else 'baseline'}.json"
        )
        if other_path.exists():
            other = read_json(other_path)
            if (other.get("endpoint"), other.get("model")) == (deployment["endpoint"], model):
                raise ReleaseAssuranceError(
                    "baseline and candidate must use distinct endpoint/model identities"
                )
        write_json(safe_path(root, f"deployments/{role}.json"), deployment)
    return deployment


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


def evaluate_case(
    case: dict[str, Any], payload: dict[str, Any], result: CompletionResult
) -> dict[str, Any]:
    errors: list[str] = []
    parsed: Any = None
    malformed = False
    if not result.ok:
        errors.append("transport_failure")
    else:
        if case.get("expected_json_schema") is not None or "exact_json" in case:
            try:
                parsed = strict_json(result.content)
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
        for fragment in case.get("expected_text_contains", []):
            if fragment not in result.content:
                errors.append("required_text_missing")
        for required in case.get("required_tool_calls", []):
            matches = [
                x["function"]
                for x in (result.tool_calls or [])
                if x["function"]["name"] == required["name"]
            ]
            valid = False
            for call in matches:
                try:
                    arguments = strict_json(call["arguments"])
                except (ValueError, UnicodeError, RecursionError):
                    continue
                if isinstance(arguments, dict) and (
                    "arguments" not in required or _match(required["arguments"], arguments)
                ):
                    valid = True
            if not valid:
                errors.append("required_tool_invalid_or_missing")
    tpot = None
    if (
        result.ok
        and case.get("stream")
        and result.ttft_ms is not None
        and result.output_tokens is not None
        and result.output_tokens > 1
    ):
        tpot = round(max(0, result.latency_ms - result.ttft_ms) / (result.output_tokens - 1), 6)
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
        if not isinstance(row, dict) or any(row.get(key) != value for key, value in job.items()):
            raise EvidenceValidationError("result identity/order mismatch")
        if type(row.get("repeat")) is not int:
            raise EvidenceValidationError("invalid repeat identity")
        if row.get("request_sha256") != sha256_json(
            _request_payload(cases[job["case_id"]], deployments[job["role"]])
        ):
            raise EvidenceValidationError("request hash mismatch")
        if row.get("status") not in {"ok", "error"}:
            raise EvidenceValidationError("invalid result status")
        if (
            type(row.get("compatibility_passed")) is not bool
            or type(row.get("malformed_output")) is not bool
        ):
            raise EvidenceValidationError("invalid result flags")
        errors = row.get("compatibility_errors")
        if (
            not isinstance(errors, list)
            or len(errors) > 20
            or any(
                not isinstance(x, str) or not re.fullmatch(r"[a-zA-Z0-9_:.-]{1,100}", x)
                for x in errors
            )
        ):
            raise EvidenceValidationError("invalid value-free error codes")
        if row["compatibility_passed"] != (row["status"] == "ok" and not errors):
            raise EvidenceValidationError("contradictory compatibility result")
        if row["malformed_output"] != ("invalid_json_content" in errors):
            raise EvidenceValidationError("contradictory malformed-output result")
        if row["status"] == "error" and errors != ["transport_failure"]:
            raise EvidenceValidationError("contradictory transport-failure result")
        if not finite_number(row.get("latency_ms")):
            raise EvidenceValidationError("invalid latency")
        for metric in ("ttft_ms", "tpot_ms"):
            value = row.get(metric)
            if value is not None and not finite_number(value, 0, row["latency_ms"]):
                raise EvidenceValidationError("invalid timing value")
        tokens = row.get("output_tokens")
        if tokens is not None and (type(tokens) is not int or tokens < 0):
            raise EvidenceValidationError("invalid token count")
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
        case = cases[job["case_id"]]
        if not case.get("stream") and (
            row.get("ttft_ms") is not None or row.get("tpot_ms") is not None
        ):
            raise EvidenceValidationError("non-streaming token timings are not observable")
        if ("synthetic_response" in row or "synthetic_response_omitted" in row) and not case.get(
            "safe_to_store"
        ):
            raise EvidenceValidationError("unexpected retained response")


def aggregate(results: list[dict[str, Any]], role: str) -> dict[str, Any]:
    rows = [r for r in results if r["role"] == role]
    successful = [r for r in rows if r["status"] == "ok"]
    stats: dict[str, Any] = {
        "planned_samples": len(rows),
        "successful_samples": len(successful),
        "transport_errors": len(rows) - len(successful),
        "compatibility_failures": sum(not r["compatibility_passed"] for r in successful),
        "malformed_outputs": sum(r["malformed_output"] for r in rows),
    }
    for metric in ("latency_ms", "ttft_ms", "tpot_ms"):
        values = [float(r[metric]) for r in successful if r.get(metric) is not None]
        stats[metric] = {
            "count": len(values),
            "p50": percentile(values, 0.5),
            "p95": percentile(values, 0.95),
        }
    elapsed = sum(r["latency_ms"] for r in rows) / 1000
    stats["requests_per_second"] = len(successful) / elapsed if elapsed > 0 else None
    return stats


def decide(
    contract: dict[str, Any],
    deployments: dict[str, dict[str, Any]],
    aggregates: dict[str, dict[str, Any]],
) -> tuple[str, list[str]]:
    base, candidate = aggregates["baseline"], aggregates["candidate"]
    for field in contract.get("environment", {}).get("candidate_must_match", []):
        a, b = (deployments[role]["environment"].get(field) for role in ("baseline", "candidate"))
        if a in (None, "", "unknown") or b in (None, "", "unknown") or a != b:
            return "INCONCLUSIVE", ["environment_identity_unverified_or_mismatched"]
    limits = contract.get("acceptance", {})
    minimum = limits.get("min_samples") or 1
    if min(base["successful_samples"], candidate["successful_samples"]) < minimum:
        return "INCONCLUSIVE", ["insufficient_successful_samples"]
    if base["transport_errors"] or base["compatibility_failures"] or base["malformed_outputs"]:
        return "INCONCLUSIVE", ["baseline_not_qualified"]
    if candidate["transport_errors"]:
        return "INCONCLUSIVE", ["candidate_transport_incomplete"]
    if candidate["compatibility_failures"] or candidate["malformed_outputs"]:
        return "FAIL", ["candidate_compatibility_failure"]
    failures: list[str] = []
    for key, metric in METRIC_LIMITS.items():
        limit = limits.get(key)
        if limit is None:
            continue
        a, b = base[metric], candidate[metric]
        # Missing requested metrics cannot silently waive a contract requirement.
        if (
            a["count"] != base["successful_samples"]
            or b["count"] != candidate["successful_samples"]
        ):
            return "INCONCLUSIVE", ["required_metric_unavailable"]
        if key.endswith("regression_pct"):
            if a["p95"] is None or a["p95"] <= 0:
                return "INCONCLUSIVE", ["invalid_performance_reference"]
            value = (b["p95"] / a["p95"] - 1) * 100
        else:
            value = b["p95"]
        if value is None:
            return "INCONCLUSIVE", ["required_metric_unavailable"]
        if value > limit:
            failures.append(key)
    if limits.get("min_requests_per_second") is not None:
        rps = candidate["requests_per_second"]
        if rps is None:
            return "INCONCLUSIVE", ["required_metric_unavailable"]
        if rps < limits["min_requests_per_second"]:
            failures.append("min_requests_per_second")
    return ("FAIL" if failures else "PASS"), failures


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


def qualify(
    project_dir: Path,
    *,
    repeats: int | None = None,
    timeout_s: float = 30.0,
    resume: bool = False,
    stop_after: int | None = None,
) -> dict[str, Any]:
    root = project_root(project_dir)
    if not finite_number(timeout_s, 0.05, 600):
        raise ReleaseAssuranceError("timeout must be finite and between 0.05 and 600 seconds")
    if stop_after is not None and (type(stop_after) is not int or stop_after < 1):
        raise ReleaseAssuranceError("stop-after must be a positive integer")
    with _lock(root):
        project, contract, deployments = _load(root)
        if (deployments["baseline"]["endpoint"], deployments["baseline"]["model"]) == (
            deployments["candidate"]["endpoint"],
            deployments["candidate"]["model"],
        ):
            raise EvidenceValidationError("baseline and candidate are identical")
        count = contract.get("repeats", 1) if repeats is None else repeats
        if (
            type(count) is not int
            or not 1 <= count <= 100
            or count * len(contract["cases"]) * 2 > MAX_JOBS
        ):
            raise ReleaseAssuranceError("invalid repeats or request budget")
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
                    raise ReleaseAssuranceError(
                        "run checkpointed; resume with unchanged parameters"
                    )
        except KeyboardInterrupt:
            progress["status"] = "interrupted"
            write_json(safe_path(run_dir, "progress.json"), progress)
            write_json(active_path, {"run_id": progress["run_id"], "status": "interrupted"})
            raise ReleaseAssuranceError(
                "interrupted; inspect in-flight state before resuming"
            ) from None
        validate_results(progress["results"], jobs, contract, deployments)
        # Refuse input mutation during the experiment, even by another tool.
        current_project, current_contract, current_deployments = _load(root)
        if (
            project_hash(current_project) != identity["project_hash"]
            or contract_hash(current_contract) != identity["contract_hash"]
            or {r: d["deployment_hash"] for r, d in current_deployments.items()}
            != identity["deployment_hashes"]
        ):
            raise EvidenceValidationError("project inputs changed during qualification")
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
                "cases": list(cases),
                "repeats": count,
                "protocol": "chat/completions",
                "measurement": "client-side sequential observations; no statistical confidence claim",
            },
        }
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
        write_json(active_path, {"run_id": progress["run_id"], "status": "complete"})
        return receipt


def load_latest_receipt(project_dir: Path) -> dict[str, Any]:
    from .report import verify_project

    result = verify_project(project_dir)
    if not result["valid"]:
        raise EvidenceValidationError("latest evidence did not verify")
    root = project_root(project_dir)
    project = read_json(safe_path(root, "project.json"))
    return read_json(safe_path(_run_dir(root, project["latest_run_id"]), "receipt.json"))
