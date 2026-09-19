# Changelog

## 0.1.0 — release candidate

First standalone public-source candidate of the Release Assurance CLI.

- Isolated the product from unrelated research, model, GPU, and commercial machinery.
- Added distinct PASS / FAIL / INCONCLUSIVE exit codes and a local synthetic demo.
- Bounded HTTP/SSE consumption; disabled redirects and plaintext credential transport.
- Removed fabricated token counts and non-streaming first-token timings.
- Tightened contracts, result coverage, resume identity, mandatory artifacts, and
  deterministic verdict/report verification.
- Added privacy-focused failure codes, optional externally trusted manifest hashes,
  security regressions, packaging checks, and CPU-only CI configuration.

The standalone import path is `computelab_release`. The console entrypoint remains
`computelab-release`. Private-tool evidence is not silently migrated; create a new
project so evaluation policy and measurements cannot be confused with old receipts.
