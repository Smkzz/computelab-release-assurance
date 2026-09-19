# Computelab Release Assurance

**Check an LLM deployment change against the behaviors your application requires.**

Give the CLI a baseline endpoint, a candidate endpoint, and a JSON contract. It runs
both, checks structured responses and tool calls, records client-side timings,
and writes a reproducible **PASS**, **FAIL**, or **INCONCLUSIVE** report.

This is a small, experimental regression checker—not a model judge, load generator,
safety certification, or inference optimizer. You control the endpoints and tests.
Python 3.11+ is targeted; no GPU, model weights, API subscription, or agent framework
is required by the tool itself. The runtime dependency is `jsonschema`.

## Try the local demo

From this source directory:

```sh
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install .
computelab-release demo --output demo-output
```

Package installation may need PyPI access. The installed demo needs no external
network: it starts a temporary loopback HTTP server with synthetic completions.
It checks compatible output, a deliberately broken JSON response, and an
unavailable candidate. Expected results are **PASS**, **FAIL**, and **INCONCLUSIVE**.
The demo returns zero only when all expected decisions and evidence checks pass.
It never deletes an existing output directory.

Inspect the seeded regression:

```sh
computelab-release show demo-output/regression
computelab-release report demo-output/regression
computelab-release verify demo-output/regression
```

See a [sample report](examples/report.md).

## Compare your deployments

Use test endpoints you own or have permission to call. Requests can consume
inference capacity; the tool never starts or modifies a model server.

```sh
computelab-release init upgrade-check
computelab-release register baseline upgrade-check --url http://127.0.0.1:8000/v1 --model baseline-model
computelab-release register candidate upgrade-check --url http://127.0.0.1:8001/v1 --model candidate-model
computelab-release define-contract upgrade-check --input examples/contract.json
computelab-release qualify upgrade-check
computelab-release verify upgrade-check
```

For authenticated deployments, supply **HTTPS** endpoints and `--api-key-env` naming
an existing environment variable. Never put a key in a URL or contract. Plain HTTP
is supported for intentional local/private test deployments without credentials;
bearer credentials over HTTP are rejected, even on loopback. Redirects and implicit
proxy-environment use are disabled.

| Exit code | Meaning |
|---:|---|
| 0 | Successful command; `qualify` means PASS |
| 1 | Candidate failed the tested contract |
| 2 | Invalid input, interrupted run, or operational error |
| 3 | Qualification is INCONCLUSIVE |
| 4 | Evidence verification failed |

`report` prints Markdown. `show` and `verify` print JSON. A successful evidence
verification is not the same as a passing deployment verdict.

## What gets checked

Contracts can require inline JSON Schema structure, exact JSON values, required
text, required function calls and argument subsets. Streaming tool fragments are
reassembled before checks. Missing, truncated, oversized, or malformed responses
do not become successful samples. See [the contract reference](docs/contract.md).

Optional latency limits use **observed client-side samples**. TTFT is available
only from streamed content events. Time/token is an estimate requiring server
usage counts; words are never substituted for tokens. No confidence interval,
server-capacity measurement, or causal speedup claim is implied.

## Evidence and recovery

Each run writes input snapshots, result rows, a report, and a SHA-256 manifest.
Verification checks complete request coverage, input identities, required files,
and recomputes the decision and report from recorded rows. An optional
`verify --expected-manifest SHA256` checks against a digest you stored separately.
Unsigned self-contained hashes do **not** authenticate the experiment or its author.

Runs checkpoint between requests. Resume with the same parameters:

```sh
computelab-release qualify upgrade-check --resume
```

Changed inputs, runtime policy, or timeout reject stale resumes. A request that was
in flight when the process died has an unknown server outcome: it is **not silently
reissued**. Preserve that run and start a separate project for a new attempt.

## Privacy and limits

**Project directories are private data.** They contain your supplied contract,
prompts, endpoint registration, and input snapshots. Extra raw model responses are
not retained by default; errors use value-free codes. `safe_to_store` is an explicit
opt-in intended only for synthetic fixtures. Never publish a real run directory.
Hashes of low-entropy content are not anonymization.

The CLI trusts the local operator and contract author. It is not an untrusted-URL
service, hostile-local-user sandbox, or statistically powered benchmark framework.
See [SECURITY.md](SECURITY.md) and [architecture and limits](docs/architecture.md).

## Development

```sh
python -m pip install -e ".[dev]"
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m mypy
python -m build
```

Tests use loopback synthetic servers, never paid APIs or downloaded models. See
[CONTRIBUTING.md](CONTRIBUTING.md). This is a portfolio OSS project with bounded
maintenance, not a supported commercial service. There is no support SLA.

The CLI was extracted and hardened from Computelab's private experimental toolkit.
The public package intentionally excludes research engines, benchmark databases,
model assets, prospect records, and historical campaign machinery.

## License

MIT for this source and documentation. Dependencies retain their own licenses and
are installed separately; this repository does not redistribute model weights or
third-party matrix-algorithm datasets.
