# Changelog

## Unreleased

- Expanded the README with scan-friendly scope, audience, badges, workflow diagram, actual demo output shape, an inline minimal contract, a rendered report excerpt, and troubleshooting guidance.
- Added one-line README navigation, a replayable terminal demo capture (included in source distributions), a successful qualification output example, and first-mention links to the contract and architecture references.
- Added a top-level terminal image, scan-friendly requirements and verdict semantics, plus a runnable mock HTTP server demonstrating the real register/qualify/verify path.

## 0.1.1 — 2026-09-20

Quality-hardening release with no intended change to the v0.1 contract semantics.

- Refactored contract validation, evidence verification, qualification orchestration, CLI dispatch, and HTTP/SSE handling into focused helpers with enforced complexity limits.
- Expanded deterministic regression coverage for validation boundaries, resume/checkpoint safety, evidence forgery classes, decision logic, CLI failures, and transport protocol rejection paths.
- Added mandatory branch-coverage gates: at least 95% across production code and 90% for every production Python module.
- Added static security analysis to the development and hosted CI gates.
- Polished public documentation and made the release-quality procedure reproducible from a clean checkout.

## 0.1.0 — 2026-09-20

Initial public release of the Release Assurance CLI.

- Added explicit PASS / FAIL / INCONCLUSIVE exit codes and a local synthetic demo.
- Bounded HTTP/SSE consumption; disabled redirects and plaintext credential transport.
- Avoided fabricated token counts and non-streaming first-token timings.
- Bound contracts, result coverage, resume identity, mandatory artifacts, and deterministic verdict/report verification.
- Added privacy-focused failure codes, optional externally trusted manifest hashes, security regressions, packaging checks, and CPU-only CI.

The import path is `computelab_release`; the console entrypoint is `computelab-release`. Evidence from another evaluation policy is not silently migrated: create a new project when changing the experiment so measurements cannot be confused with earlier receipts.
