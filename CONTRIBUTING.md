# Contributing

Keep this project a focused endpoint-contract regression checker. Changes should have a clear user-visible or correctness rationale and include targeted tests. Do not add unrelated product machinery, real endpoint evidence, customer data, credentials, model weights, or mandatory GPU dependencies.

Install `.[dev,security]` in a virtual environment and run the quality commands in the README. Tests must work without external inference services. Use synthetic loopback fixtures for HTTP/SSE behavior rather than replacing the transport with mocks when the transport itself is under test.

A contract feature must be validated, enforced, documented, and tested for invalid input. Missing measurements must not silently satisfy an acceptance limit. Evidence-verification changes need negative tests for missing, duplicate, stale, corrupted, contradictory, and identity-mismatched artifacts.

Production branch coverage must remain at least 95% overall and 90% per Python module. Every production module must remain Radon A maintainability (MI ≥ 20), every code block must remain at complexity 10 or below, and the strict Ruff complexity gate must stay clean without suppressing genuine findings. Security, typing, lint, format, build, release-boundary, and isolated-wheel checks are release requirements rather than optional cleanup.

Submit focused pull requests. Include reproduction steps and the commands actually run; identify skipped or unavailable checks explicitly. AI-assisted contributions are welcome, but contributors remain responsible for provenance, correctness, tests, and review.

Be respectful and factual in issues and reviews. Report vulnerabilities through the private route described in SECURITY.md rather than posting exploit details publicly. Maintenance is best-effort; discuss substantial scope expansions before implementation.
