# Architecture and limits

The CLI calls a small Python library. `models` validates JSON contracts and handles
bounded persistence. `transport` makes one HTTP(S) request and reconstructs bounded
SSE. `runner` alternates baseline/candidate requests, records checks, and decides the
observed contract. `report` independently regenerates aggregates, decisions, and
Markdown from the recorded rows. `demo` supplies only loopback fixtures.

A project contains `project.json`, `contract.json`, `deployments/`, and `runs/`.
Each run has six files: inputs, progress, receipt, JSON report, Markdown report, and
manifest. No SQLite, browser, Docker, agent, or GPU infrastructure is required.
There are no plugins or dynamically loaded code from contracts.

## What verification establishes

Verification requires every planned request exactly once in the declared order,
valid row types, request hashes matching input snapshots, consistent deployment and
project fingerprints, correct aggregates and decision, complete manifest coverage,
and exact report regeneration. It refuses unknown artifacts, symlinks, stale current
inputs, and malformed pointers. It is structural verification, not a proof that raw
responses were correct or that a benchmark took place. See SECURITY.md.

## Measurement model

Each case is run serially against both endpoints. Order alternates by case/repeat to
reduce a fixed first-arm bias. Warm-up, concurrency, GPU clocks, server queues, and
network load are not controlled by this client. Sample percentiles are nearest-rank
observations; small samples can be noisy. The tool intentionally computes no
confidence intervals and does not claim a regression is statistically significant.
Use dedicated benchmarking tools for load generation, reliable throughput limits,
independent replications, or an inference-performance publication.

TTFT is measured to the first non-empty text/tool content SSE event, not headers or
a role-only event. Non-streamed responses cannot supply TTFT. Time/token is estimated
only with streamed first-content time and server-reported output-token counts >1.
Missing required metrics yield INCONCLUSIVE. The serial request-rate metric includes
observed request time; it is not server capacity.

## Supported protocol

The endpoint base URL resolves to `/chat/completions`. Only one text completion and
function-style tools are supported. Streaming expects UTF-8 SSE with a terminal
finish reason followed by `[DONE]`; fragmented tool arguments are joined before
validation. Multimodal content, legacy function_call, multi-choice requests, proxy
configuration, and alternative completion APIs are not implemented. Unsupported or
malformed protocol behavior fails rather than silently succeeding.

## Recovery and resource bounds

A single-writer lock protects normal CLI writers. Checkpoints precede requests and
follow completed rows. Completed requests are not repeated on a normal resume.
A process death during an in-flight request is ambiguous and blocks automatic retry.
There is no exactly-once guarantee at the remote server. Reuse requires unchanged
project/contract/deployments, repeat count, timeout, Python/platform policy identity.

A run is limited to 10,000 requests and 16 MiB per JSON artifact. Progress snapshots
are rewritten atomically after requests; this favors simple recovery, not large-scale
load generation. For very large suites, use smaller separate projects. JSON schemas
are trusted operator input and can still be computationally expensive. No multi-user
filesystem or CPU isolation is claimed.
