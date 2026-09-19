# Maintainer release procedure

Publish only this standalone project, never the private research repository or its
history. Do not copy runtime projects, prompts, endpoint credentials, prospect
records, generated evidence, virtual environments, or model artifacts into Git.

## Before publication

Confirm copyright ownership and the prepared MIT license. Create a new repository
from the audited source allowlist. Review every initial tracked file; do not use a
recursive `git add` from a parent directory. Record the release source-tree digest.

Run the commands in CONTRIBUTING.md in clean environments. The CPU workflow tests
Python 3.11 and 3.13 on Linux and 3.13 on Windows; configured jobs are not evidence
until they actually run. Complete formatting, lint, strict typing, dependency
vulnerability scanning and secret scanning before declaring a release ready.
Inspect the source distribution and wheel, then install the wheel outside the
checkout and run the synthetic demo without PYTHONPATH or editable-install access.
Never claim untested platforms or a clean vulnerability database result.

## Repository settings

Enable private vulnerability reporting and verify the Security tab provides a
private reporting route before linking it publicly. Enable secret scanning/push
protection and dependency alerts where available. Protect the default branch with
required checks and review; keep workflow token permissions read-only. Do not
attach self-hosted runners to untrusted public pull requests. Do not configure
live inference credentials in the public test workflow.

Confirm the selected repository/account's Actions usage terms and spending limits
before enabling CI. No paid runners or API/GPU tests are necessary for this suite.

## Distribution

Build with `python -m build`; run `python tools/check_release.py` and
`python tools/wheel_smoke.py`. Include source, wheel, changelog and SHA-256 checksums
only after their gates pass. A checksum supports integrity, not publisher identity.
Use a reviewed signed tag or artifact attestation when a real signing identity is
available; do not manufacture signatures or provenance. PyPI publishing is a
separate explicit maintainer action, not an automatic workflow in this project.

This project has no automatic deployment, package publication or release action.
