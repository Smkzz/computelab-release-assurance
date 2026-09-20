"""Aggregate observations and apply qualification prerequisites and metric limits."""

from __future__ import annotations

from typing import Any

from .models import METRIC_LIMITS, percentile


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
        stats[metric] = _metric_summary(successful, metric)
    elapsed = sum(r["latency_ms"] for r in rows) / 1000
    stats["requests_per_second"] = len(successful) / elapsed if elapsed > 0 else None
    return stats


def _metric_summary(rows: list[dict[str, Any]], metric: str) -> dict[str, Any]:
    """Summarize only observations that expose the requested timing metric."""
    values = [float(r[metric]) for r in rows if r.get(metric) is not None]
    return {
        "count": len(values),
        "p50": percentile(values, 0.5),
        "p95": percentile(values, 0.95),
    }


def _environment_matches(contract: dict[str, Any], deployments: dict[str, dict[str, Any]]) -> bool:
    """Unknown environment identities cannot satisfy a requested match."""
    for field in contract.get("environment", {}).get("candidate_must_match", []):
        a, b = (deployments[role]["environment"].get(field) for role in ("baseline", "candidate"))
        if a in (None, "", "unknown") or b in (None, "", "unknown") or a != b:
            return False
    return True


def _qualification_decision(
    contract: dict[str, Any],
    deployments: dict[str, dict[str, Any]],
    aggregates: dict[str, dict[str, Any]],
) -> tuple[str, list[str]] | None:
    base, candidate = aggregates["baseline"], aggregates["candidate"]
    if not _environment_matches(contract, deployments):
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
    return None


def _metric_decision(
    limits: dict[str, Any], base: dict[str, Any], candidate: dict[str, Any]
) -> tuple[str, list[str]]:
    failures: list[str] = []
    for key, metric in METRIC_LIMITS.items():
        limit = limits.get(key)
        if limit is None:
            continue
        a, b = base[metric], candidate[metric]
        # Missing requested metrics cannot silently waive a contract requirement.
        if not _metric_available(base, candidate, metric):
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
    return ("FAIL" if failures else "PASS"), failures


def _metric_available(base: dict[str, Any], candidate: dict[str, Any], metric: str) -> bool:
    return bool(
        base[metric]["count"] == base["successful_samples"]
        and candidate[metric]["count"] == candidate["successful_samples"]
    )


def decide(
    contract: dict[str, Any],
    deployments: dict[str, dict[str, Any]],
    aggregates: dict[str, dict[str, Any]],
) -> tuple[str, list[str]]:
    prerequisite = _qualification_decision(contract, deployments, aggregates)
    if prerequisite is not None:
        return prerequisite
    limits = contract.get("acceptance", {})
    candidate = aggregates["candidate"]
    verdict, failures = _metric_decision(limits, aggregates["baseline"], candidate)
    if verdict == "INCONCLUSIVE":
        return verdict, failures
    if limits.get("min_requests_per_second") is not None:
        rps = candidate["requests_per_second"]
        if rps is None:
            return "INCONCLUSIVE", ["required_metric_unavailable"]
        if rps < limits["min_requests_per_second"]:
            failures.append("min_requests_per_second")
    return ("FAIL" if failures else "PASS"), failures
