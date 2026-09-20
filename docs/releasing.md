# Maintainer release procedure

Release only the reviewed standalone source tree. Do not include runtime projects, prompts, endpoint credentials, real evaluation evidence, customer data, virtual environments, caches, build directories, or model artifacts.

## Before publication

Confirm the intended version and license, review every tracked file, and verify that the working tree contains no unexpected untracked material. Do not use a recursive add from a parent workspace. Record the exact Git commit and release-artifact digest.

Run the complete quality gate from the README in a clean environment. Hosted CI currently covers Python 3.11 and 3.13 on Linux and Python 3.13 on Windows. Configured jobs are not evidence until the actual commit has passed them.

Release qualification requires:

- full test suite with branch coverage
- at least 95% production branch coverage overall and 90% per production Python module
- normal Ruff lint and format checks
- Radon A maintainability (MI ≥ 20) for every production module and cyclomatic complexity ≤ 10 per block
- strict Ruff complexity checks (`C901`, `PLR0911`, `PLR0912`, `PLR0915`)
- Vulture dead-code scan
- strict mypy
- Bandit static security analysis
- dependency vulnerability audit
- source/distribution boundary inspection
- wheel installation and demo/verification outside the checkout
- repository secret scanning and push protection

Never claim an unexecuted platform or vulnerability scan as passing.

## Repository settings

Keep private vulnerability reporting, secret scanning/push protection, dependency alerts, and protected-branch review enabled where the hosting account supports them. Required checks must protect the default branch. CI workflow permissions stay read-only and public pull requests must never receive production endpoints, model credentials, publishing credentials, or personal self-hosted runners.

Confirm the repository/account's Actions usage terms and spending limits before changing CI. No paid runners, inference APIs, or GPUs are required by this suite.

## Distribution

Build with `python -m build`, then run `python tools/check_release.py` and `python tools/wheel_smoke.py`. Inspect the source distribution and wheel before publication. Attach only reviewed release artifacts and publish SHA-256 checksums from the sealed files. A checksum provides integrity, not publisher identity.

Use a reviewed signed tag or artifact attestation when a real signing identity is available; never manufacture provenance. PyPI publishing is a separate explicit maintainer action and is not automatic in this repository.

After publication, download the hosted asset again, recompute its digest, and verify that the release tag resolves to the intended commit.
