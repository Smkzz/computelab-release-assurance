# Computelab Release Assurance

**Check an LLM deployment change against the behaviors your application actually requires.**

Computelab Release Assurance compares a baseline OpenAI-compatible endpoint with a candidate against an explicit JSON contract. It records bounded client-side evidence and returns a reproducible **PASS**, **FAIL**, or **INCONCLUSIVE** verdict.

The project is intentionally narrow: it is a regression checker, not a model judge, load generator, safety certification, capacity benchmark, or inference optimizer. You choose the endpoints and the acceptance contract. Python 3.11+ is supported; the runtime has one dependency, `jsonschema`, and requires no GPU, model weights, agent framework, or paid API by itself.

## Quick start

From a source checkout:

```sh
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install .
computelab-release demo --output demo-output
```

The installed demo runs entirely on loopback with synthetic completions. It exercises a compatible candidate, a deliberate structured-output regression, and an unavailable candidate; the expected verdicts are **PASS**, **FAIL**, and **INCONCLUSIVE**. Existing output directories are never deleted automatically.

Inspect the synthetic regression:

```sh
computelab-release show demo-output/regression
computelab-release report demo-output/regression
computelab-release verify demo-output/regression
```

See the [sample report](examples/report.md).

## Compare deployments

Use endpoints you own or are authorized to test. Requests can consume inference capacity; this tool never starts or modifies a model server.

```sh
computelab-release init upgrade-check
computelab-release register baseline upgrade-check --url http://127.0.0.1:8000/v1 --model baseline-model
computelab-release register candidate upgrade-check --url http://127.0.0.1:8001/v1 --model candidate-model
computelab-release define-contract upgrade-check --input examples/contract.json
computelab-release qualify upgrade-check
computelab-release verify upgrade-check
```

For authenticated deployments, use **HTTPS** and `--api-key-env` to name an existing environment variable. Never place a key in a URL or contract. Plain HTTP is supported only for intentional unauthenticated local/private test deployments; bearer credentials over HTTP are rejected. Redirects and implicit proxy-environment use are disabled.

| Exit code | Meaning |
|---:|---|
| 0 | Successful command; `qualify` means PASS |
| 1 | Candidate failed the tested contract |
| 2 | Invalid input, interrupted run, or operational error |
| 3 | Qualification is INCONCLUSIVE |
| 4 | Evidence verification failed |

`report` prints Markdown. `show` and `verify` print JSON. Evidence verification and deployment qualification are deliberately separate: a self-consistent evidence bundle does not imply a passing candidate.

## Contract checks

Contracts can require inline JSON Schema structure, exact JSON values, required text, and required function calls with argument subsets. Streaming tool fragments are reassembled before validation. Missing, truncated, oversized, malformed, or incomplete responses cannot silently become successful samples. See the [contract reference](docs/contract.md).

Optional performance limits operate on **observed client-side samples**. TTFT exists only for streamed content events. Time/token is an estimate that requires server-reported token counts; words are never substituted for tokens. The tool makes no confidence-interval, server-capacity, or causal speedup claim.

## Evidence and recovery

Each run records input snapshots, result rows, a deterministic report, and a SHA-256 manifest. Verification checks request coverage, input identities, required artifacts, hashes, and recomputes the decision and Markdown report from recorded rows.

For stronger tamper detection, store a manifest digest separately and verify it later:

```sh
computelab-release verify upgrade-check --expected-manifest <sha256>
```

The manifest establishes consistency, not publisher identity or truth of remote execution.

Runs checkpoint between requests. Resume only with unchanged inputs and policy:

```sh
computelab-release qualify upgrade-check --resume
```

An in-flight request at process death has an unknown remote outcome and is never silently replayed. Preserve that interrupted run and start a separate project for a fresh attempt.

## Privacy and security

**Treat project directories as private data.** They contain your contract, prompts, endpoint registration, and input snapshots. Raw response text is not retained by default; `safe_to_store` is an explicit opt-in intended for synthetic fixtures only. Hashes of low-entropy content are not anonymization.

The CLI trusts the local operator and contract author. It is not a hostile-local-user sandbox or an SSRF defense for a service that accepts untrusted endpoint URLs. See [SECURITY.md](SECURITY.md) and [architecture and limits](docs/architecture.md) for the exact trust boundaries.

## Development and quality gates

Install development dependencies and run:

```sh
python -m pip install -e ".[dev,security]"
python -m coverage run -m pytest -q
python -m coverage report -m
python -m coverage json -o coverage.json
python tools/check_quality.py coverage.json
python -m ruff check .
python -m ruff check src --select C901,PLR0911,PLR0912,PLR0915
python -m ruff format --check .
python -m mypy
python -m bandit -r src -q
python -m vulture src --min-confidence 80
python -m build
python tools/check_release.py
python tools/wheel_smoke.py
```

The quality gate requires at least **95% production branch coverage overall**, **90% for every production Python module**, **Radon A maintainability (MI ≥ 20) for every production module**, and **cyclomatic complexity ≤ 10 for every block**. Tests use synthetic loopback servers; hosted CI requires no inference credentials, paid APIs, GPUs, or self-hosted runners. See [CONTRIBUTING.md](CONTRIBUTING.md).

This is an experimental open-source project with best-effort maintenance and no support SLA.

## License

MIT for this source and documentation. Dependencies retain their own licenses and are installed separately. The repository does not redistribute model weights or third-party datasets.
