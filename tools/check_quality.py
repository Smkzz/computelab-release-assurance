"""Enforce production coverage, maintainability, and complexity thresholds."""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path
from typing import Any

from radon.complexity import cc_visit
from radon.metrics import mi_visit

from computelab_release.models import VERSION

TOTAL_MIN = 95.0
MODULE_COVERAGE_MIN = 90.0
MODULE_MI_MIN = 20.0
MAX_COMPLEXITY = 10
PACKAGE_PREFIX = "src/computelab_release/"
PACKAGE_DIR = Path("src/computelab_release")


def _percent(summary: dict[str, Any]) -> float:
    value = summary.get("percent_covered")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise SystemExit("coverage JSON is missing a numeric percent_covered value")
    return float(value)


def _coverage_failures(path: str) -> list[str]:
    report = json.loads(Path(path).read_text(encoding="utf-8"))
    raw_files = report.get("files")
    if not isinstance(raw_files, dict):
        raise SystemExit("coverage JSON has no files object")
    files = {
        str(name).replace("\\", "/"): details
        for name, details in raw_files.items()
        if str(name).replace("\\", "/").startswith(PACKAGE_PREFIX) and str(name).endswith(".py")
    }
    if not files:
        raise SystemExit("coverage JSON contains no production Python modules")
    failures: list[str] = []
    for name in sorted(files):
        details = files[name]
        if not isinstance(details, dict) or not isinstance(details.get("summary"), dict):
            raise SystemExit(f"coverage JSON has no summary for {name}")
        percent = _percent(details["summary"])
        print(f"coverage {name}: {percent:.1f}%")
        if percent < MODULE_COVERAGE_MIN:
            failures.append(f"coverage {name} {percent:.1f}% < {MODULE_COVERAGE_MIN:.1f}%")
    totals = report.get("totals")
    if not isinstance(totals, dict):
        raise SystemExit("coverage JSON has no totals object")
    total = _percent(totals)
    print(f"coverage production total: {total:.1f}%")
    if total < TOTAL_MIN:
        failures.append(f"coverage production total {total:.1f}% < {TOTAL_MIN:.1f}%")
    return failures


def _maintainability_failures() -> list[str]:
    failures: list[str] = []
    paths = sorted(PACKAGE_DIR.glob("*.py"))
    if not paths:
        raise SystemExit("no production Python modules found for maintainability gate")
    for path in paths:
        source = path.read_text(encoding="utf-8")
        mi = float(mi_visit(source, multi=True))
        print(f"maintainability {path.as_posix()}: {mi:.2f}")
        if mi < MODULE_MI_MIN:
            failures.append(f"maintainability {path.as_posix()} {mi:.2f} < {MODULE_MI_MIN:.2f}")
        for block in cc_visit(source):
            complexity = int(block.complexity)
            if complexity > MAX_COMPLEXITY:
                failures.append(
                    f"complexity {path.as_posix()}:{block.lineno} {block.name} "
                    f"{complexity} > {MAX_COMPLEXITY}"
                )
    return failures


def _version_failures() -> list[str]:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]
    package_version = project.get("version")
    print(f"version source={VERSION} package={package_version}")
    if package_version != VERSION:
        return [f"version mismatch: source {VERSION!r} != package {package_version!r}"]
    return []


def main(path: str = "coverage.json") -> None:
    failures = _coverage_failures(path) + _maintainability_failures() + _version_failures()
    if failures:
        raise SystemExit("Quality gate failed: " + "; ".join(failures))
    print("Coverage and maintainability quality gate passed")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "coverage.json")
