"""Deterministic Markdown and structural evidence verification.

An unsigned hash manifest detects corruption, not a malicious author replacing
an entire bundle. A separately trusted manifest digest can anchor the bundle.
"""

from __future__ import annotations

import html
import re
import unicodedata
from pathlib import Path
from typing import Any

from .models import (
    MANIFEST_SCHEMA,
    PRODUCT_VERSION,
    RECEIPT_SCHEMA,
    EvidenceValidationError,
    contract_hash,
    finite_number,
    project_hash,
    public_endpoint,
    read_bounded_bytes,
    read_json,
    safe_path,
    sha256_file,
    validate_contract,
    validate_deployment,
    validate_project,
)

REQUIRED_FILES = {"inputs.json", "progress.json", "receipt.json", "report.json", "report.md"}


def _safe(value: Any) -> str:
    text = str(value)
    text = "".join(c if unicodedata.category(c) not in {"Cc", "Cf"} else " " for c in text)
    text = html.escape(text, quote=True)
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", text)


def _number(value: Any, suffix: str = "") -> str:
    return f"{value:.3f}{suffix}" if finite_number(value) else "unavailable"


def render_report(receipt: dict[str, Any]) -> str:
    summary = receipt["summary"]
    aggregates = summary["aggregates"]
    lines = [
        "# Computelab Release Assurance",
        "",
        f"## {_safe(summary['verdict'])}",
        "",
        "This verdict applies only to the recorded contract, requests and client-side observations.",
        "It is not a safety certification, proof of model correctness, or statistically qualified speedup claim.",
        "",
        "## Tested scope",
        "",
        f"Contract: {_safe(receipt['contract_name'])}",
        f"Run: `{_safe(receipt['run_id'])}`",
        f"Cases: {len(receipt['scope']['cases'])}",
        f"Repeats per case: {receipt['scope']['repeats']}",
        "",
        "## Findings",
        "",
    ]
    for role in ("baseline", "candidate"):
        deployment = receipt["deployments"][role]
        lines.insert(
            lines.index("## Findings") - 1,
            f"{role.title()}: {_safe(deployment['model'])} at {_safe(deployment['endpoint_origin'])} (URL path omitted).",
        )
    lines.extend(f"- {_safe(x)}" for x in receipt["findings"])
    if not receipt["findings"]:
        lines.append("No recorded contract failures.")
    lines.extend(
        [
            "",
            "## Compatibility",
            "",
            "| Endpoint | Planned | Completed | Transport errors | Compatibility failures |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for role in ("baseline", "candidate"):
        a = aggregates[role]
        lines.append(
            f"| {role} | {a['planned_samples']} | {a['successful_samples']} | {a['transport_errors']} | {a['compatibility_failures']} |"
        )
    lines.extend(
        [
            "",
            "## Observed client performance",
            "",
            "| Metric | Baseline p95 | Candidate p95 |",
            "|---|---:|---:|",
        ]
    )
    for metric, label in (
        ("latency_ms", "Response latency"),
        ("ttft_ms", "Time to first content event (streaming)"),
        ("tpot_ms", "Streamed time/token estimate"),
    ):
        lines.append(
            f"| {label} | {_number(aggregates['baseline'][metric]['p95'], ' ms')} | {_number(aggregates['candidate'][metric]['p95'], ' ms')} |"
        )
    lines.extend(
        [
            "",
            "Non-streaming TTFT is unavailable. Token counts come from server usage, never word counts.",
            "The streaming time/token estimate is (completion time − first content event)/(reported output tokens − 1).",
            "SSE chunks may contain several tokens. This is not a distribution of individual token latencies.",
            "Serial request rates are not server capacity or concurrent goodput.",
            "",
            "## Evidence and limitations",
            "",
            "- Baseline and candidate order alternates across pairs. Network and server load can still confound measurements.",
            "- Thresholds are evaluated against observed samples; no confidence interval or causal performance claim is computed.",
            "- Environment metadata is operator-supplied, not remotely attested.",
            "- Run verification checks required files, request coverage, identities and deterministic decision regeneration.",
            "- Hashes do not authenticate the author or prove an endpoint actually executed the requests.",
            "- A separately stored trusted manifest digest can detect later bundle replacement.",
            "- The local inputs file contains the operator's contract and prompts; do not publish private run directories.",
            "",
        ]
    )
    return "\n".join(lines)


def verify_project(project_dir: Path, expected_manifest: str | None = None) -> dict[str, Any]:
    from .runner import (
        POLICY,
        _jobs,
        _load,
        _run_dir,
        aggregate,
        decide,
        project_root,
        validate_results,
    )

    try:
        root = project_root(project_dir)
        project, current_contract, current_deployments = _load(root)
        run_id = project.get("latest_run_id")
        run_dir = _run_dir(root, run_id)
        manifest_path = safe_path(run_dir, "manifest.json")
        manifest_hash = sha256_file(manifest_path)
        if expected_manifest is not None and (
            not re.fullmatch(r"[0-9a-f]{64}", expected_manifest)
            or expected_manifest != manifest_hash
        ):
            raise EvidenceValidationError("trusted_manifest_mismatch")
        manifest = read_json(manifest_path)
        if (
            set(manifest) != {"schema_version", "run_id", "files"}
            or manifest.get("schema_version") != MANIFEST_SCHEMA
            or manifest.get("run_id") != run_id
        ):
            raise EvidenceValidationError("manifest_identity_invalid")
        files = manifest.get("files")
        if not isinstance(files, dict) or set(files) != REQUIRED_FILES:
            raise EvidenceValidationError("manifest_required_file_set_mismatch")
        if {path.name for path in run_dir.iterdir()} != REQUIRED_FILES | {"manifest.json"}:
            raise EvidenceValidationError("unexpected_or_missing_run_artifact")
        for relative, expected in files.items():
            path = safe_path(run_dir, relative)
            if (
                not isinstance(expected, str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected)
                or not path.is_file()
                or sha256_file(path) != expected
            ):
                raise EvidenceValidationError("artifact_hash_mismatch")
        inputs = read_json(safe_path(run_dir, "inputs.json"))
        contract, deployments, recorded_project = (
            inputs["contract"],
            inputs["deployments"],
            inputs["project"],
        )
        validate_project(recorded_project)
        validate_contract(contract)
        if not isinstance(deployments, dict) or set(deployments) != {"baseline", "candidate"}:
            raise EvidenceValidationError("deployment_set_invalid")
        for role in deployments:
            validate_deployment(deployments[role], role)
        if (
            project_hash(recorded_project) != project_hash(project)
            or contract != current_contract
            or deployments != current_deployments
        ):
            raise EvidenceValidationError("current_inputs_do_not_match_run")
        receipt = read_json(safe_path(run_dir, "receipt.json"))
        progress = read_json(safe_path(run_dir, "progress.json"))
        if (
            receipt.get("schema_version") != RECEIPT_SCHEMA
            or receipt.get("product_version") != PRODUCT_VERSION
            or receipt.get("run_id") != run_id
        ):
            raise EvidenceValidationError("receipt_identity_invalid")
        if (
            progress.get("run_id") != run_id
            or progress.get("status") != "complete"
            or progress.get("in_flight") is not None
        ):
            raise EvidenceValidationError("progress_not_complete")
        identity = receipt["identity"]
        repeats, timeout = identity["repeats"], identity["timeout_s"]
        if (
            type(repeats) is not int
            or not 1 <= repeats <= 100
            or not finite_number(timeout, 0.05, 600)
        ):
            raise EvidenceValidationError("run_parameters_invalid")
        if (
            identity != progress.get("identity")
            or identity.get("project_hash") != project_hash(project)
            or identity.get("contract_hash") != contract_hash(contract)
        ):
            raise EvidenceValidationError("receipt_input_identity_mismatch")
        if identity.get("deployment_hashes") != {
            r: d["deployment_hash"] for r, d in deployments.items()
        }:
            raise EvidenceValidationError("receipt_deployment_identity_mismatch")
        if (
            identity.get("runtime", {}).get("policy") != POLICY
            or identity.get("runtime", {}).get("product_version") != PRODUCT_VERSION
        ):
            raise EvidenceValidationError("unsupported_evaluation_policy")
        jobs = _jobs(contract, repeats)
        if progress.get("jobs") != jobs or progress.get("results") != receipt.get("results"):
            raise EvidenceValidationError("progress_receipt_mismatch")
        validate_results(receipt.get("results"), jobs, contract, deployments)
        aggregates = {r: aggregate(receipt["results"], r) for r in deployments}
        verdict, findings = decide(contract, deployments, aggregates)
        if (
            receipt.get("summary") != {"verdict": verdict, "aggregates": aggregates}
            or receipt.get("findings") != findings
        ):
            raise EvidenceValidationError("decision_recomputation_mismatch")
        expected_deployments = {
            r: {"model": d["model"], "endpoint_origin": public_endpoint(d["endpoint"])}
            for r, d in deployments.items()
        }
        if receipt.get("deployments") != expected_deployments:
            raise EvidenceValidationError("reported_deployment_mismatch")
        if (
            receipt.get("contract_name") != contract["name"]
            or receipt.get("scope", {}).get("cases") != [c["id"] for c in contract["cases"]]
            or receipt["scope"].get("repeats") != repeats
        ):
            raise EvidenceValidationError("scope_mismatch")
        if read_json(safe_path(run_dir, "report.json")) != receipt:
            raise EvidenceValidationError("json_report_mismatch")
        if read_bounded_bytes(safe_path(run_dir, "report.md")).decode("utf-8") != render_report(
            receipt
        ):
            raise EvidenceValidationError("markdown_report_mismatch")
        return {
            "valid": True,
            "run_id": run_id,
            "manifest_sha256": manifest_hash,
            "trust": "externally-anchored-integrity"
            if expected_manifest
            else "self-consistency-only",
            "errors": [],
        }
    except EvidenceValidationError as exc:
        return {"valid": False, "errors": [str(exc)]}
    except Exception:
        # Never echo raw JSON/remote payloads or unexpected exception messages.
        return {"valid": False, "errors": ["evidence_verification_failed"]}
