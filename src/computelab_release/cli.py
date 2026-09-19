"""Small, scriptable CLI with distinct gate exit codes."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from .models import VERSION, ReleaseAssuranceError, contract_hash, pretty_json
from .report import render_report, verify_project
from .runner import (
    define_contract,
    initialize_project,
    load_latest_receipt,
    qualify,
    register_deployment,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="computelab-release",
        description="Compare two LLM endpoints against an explicit compatibility contract.",
    )
    parser.add_argument("--version", action="version", version=VERSION)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="create a new project in an empty directory")
    init.add_argument("project", type=Path)
    init.add_argument("--name")
    register = commands.add_parser("register", help="record endpoint identity, never an API key")
    register.add_argument("role", choices=("baseline", "candidate"))
    register.add_argument("project", type=Path)
    register.add_argument("--url", required=True)
    register.add_argument("--model", required=True)
    register.add_argument("--api-key-env")
    register.add_argument("--environment-json", type=Path)
    contract = commands.add_parser("define-contract", help="validate and install contract JSON")
    contract.add_argument("project", type=Path)
    contract.add_argument("--input", type=Path)
    run = commands.add_parser(
        "qualify", help="evaluate baseline and candidate (0=PASS, 1=FAIL, 3=INCONCLUSIVE)"
    )
    run.add_argument("project", type=Path)
    run.add_argument("--repeats", type=int)
    run.add_argument("--timeout", type=float, default=30.0)
    run.add_argument("--resume", action="store_true")
    run.add_argument("--stop-after", type=int, help="checkpoint after a bounded number of requests")
    for verb in ("show", "report", "verify"):
        sub = commands.add_parser(verb)
        sub.add_argument("project", type=Path)
        if verb == "verify":
            sub.add_argument(
                "--expected-manifest",
                help="SHA-256 previously stored in a trusted external location",
            )
    demo = commands.add_parser(
        "demo", help="run CPU-only synthetic PASS/FAIL/INCONCLUSIVE fixtures"
    )
    demo.add_argument(
        "--output",
        type=Path,
        required=True,
        help="new or empty directory; never deleted automatically",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "init":
            result = initialize_project(args.project, args.name)
        elif args.command == "register":
            deployment = register_deployment(
                args.project,
                args.role,
                args.url,
                args.model,
                args.api_key_env,
                args.environment_json,
            )
            result = {"role": deployment["role"], "deployment_hash": deployment["deployment_hash"]}
        elif args.command == "define-contract":
            contract = define_contract(args.project, args.input)
            result = {"contract_sha256": contract_hash(contract)}
        elif args.command == "qualify":
            receipt = qualify(
                args.project,
                repeats=args.repeats,
                timeout_s=args.timeout,
                resume=args.resume,
                stop_after=args.stop_after,
            )
            verdict = receipt["summary"]["verdict"]
            print(
                pretty_json(
                    {
                        "verdict": verdict,
                        "run_id": receipt["run_id"],
                        "report": f"runs/{receipt['run_id']}/report.md",
                    }
                ),
                end="",
            )
            return {"PASS": 0, "FAIL": 1, "INCONCLUSIVE": 3}[verdict]
        elif args.command == "verify":
            result = verify_project(args.project, args.expected_manifest)
            print(pretty_json(result), end="")
            return 0 if result["valid"] else 4
        elif args.command in {"show", "report"}:
            receipt = load_latest_receipt(args.project)
            if args.command == "report":
                print(render_report(receipt), end="")
                return 0
            result = {
                "run_id": receipt["run_id"],
                "summary": receipt["summary"],
                "findings": receipt["findings"],
            }
        elif args.command == "demo":
            from .demo import run_demo

            result = run_demo(args.output)
        else:
            return 2
        print(pretty_json(result), end="")
        return 0
    except ReleaseAssuranceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError, KeyError):
        print(
            "error: input or filesystem operation failed; no passing verdict was produced",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
