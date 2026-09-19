# Contributing

Keep this a small endpoint-contract checker. Changes should include a focused test
and explain a user-visible behavior or corrected failure mode. Do not add GPU/model
requirements, experimental research pipelines, prospect data, generated reports from
real endpoints, or credentials.

Install `.[dev]` in a virtual environment and run the commands in the README. Tests
must be runnable without external inference services. Use synthetic loopback fixtures;
do not mock away the transport when testing HTTP/SSE behavior.

A contract feature must be validated, enforced, documented, and tested for invalid
input. A missing measurement must not silently pass its limit. Verification changes
need negative tests for missing, duplicate, stale, and corrupted evidence.

Submit small pull requests. Include reproduction steps and test commands actually
run; mark skipped/unavailable checks honestly. AI-assisted contributions are welcome,
but authors remain responsible for source provenance, correctness, and review.

Be respectful and factual in issues and reviews. For private security issues follow
SECURITY.md, not the public issue tracker. Maintenance is best-effort; substantial new
features should be discussed before implementation.
