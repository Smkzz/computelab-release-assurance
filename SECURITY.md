# Security

## Reporting

Do not include credentials, private prompts, real run directories, or exploit details
in a public issue. Use this repository's **Security → Report a vulnerability** route
when private reporting is enabled. If that route is unavailable, open a minimal
issue requesting a private contact channel without disclosing the vulnerability.
No response time or remediation SLA is promised. This experimental 0.1 line is the
only maintained version; unreleased builds are not independently certified.

## Trust boundaries

The operator controls endpoints, contracts, local project files, and credentials.
Endpoints and returned output are not trusted. The tool does not execute model
output, tools returned by the model, shell commands supplied by responses, pickle,
or remote JSON Schema references.

Intentional localhost and private-network endpoints are supported. Do not expose
arbitrary endpoint selection to untrusted users. Network egress policy must be
provided by the deployment operator. The URL validator is not an SSRF sandbox.
HTTPS uses standard certificate verification; redirects, implicit proxies, and
plaintext bearer credentials are rejected. HTTP without auth provides no transport
confidentiality, including for prompts.

Responses are bounded to 4 MiB. SSE lines/events are bounded to 64 KiB, and event
count is bounded. A request deadline closes the connected socket; OS DNS resolution
can still obey the resolver's own timeout. There is no retry hiding failed samples.
Run JSON artifacts are bounded to 16 MiB; retained synthetic responses to 64 KiB each.
Local contracts can still contain expensive schema expressions. Only trusted contract
authors should use this single-user CLI; no hard CPU sandbox is claimed for schemas.

Project files reject traversal and symlinks and use atomic replacements and a
single-writer lock. These checks do not defeat a hostile same-user process racing
filesystem operations. Windows directory ACLs and filesystem permissions remain
the operator's responsibility. A stale lock must not be removed until the operator
has confirmed that no writer is alive.

## Evidence is not attestation

The manifest verifies consistency with recorded files, not truth of execution.
Someone who can replace the entire evidence set can generate matching hashes.
Preserve a manifest digest in a separately trusted location to detect later changes.
Even an externally anchored hash does not prove the endpoint answered truthfully.
Compatibility is recomputed from recorded check results, not replayed remote output.

## Data handling

Contracts and input snapshots contain the prompts you supply. API keys are read
from named environment variables, never intentionally saved. Response bodies are
not retained unless `safe_to_store` is explicitly enabled for synthetic cases.
Error codes avoid validator messages that echo model values. This cannot identify
secrets an operator puts into arbitrary metadata, contract names, or fixture text.
Do not store keys in those fields. Treat hashes and timing metadata as potentially
sensitive too. There is no telemetry, central upload, or analytics service.

## Maintainer release controls

Before publishing, enable private vulnerability reporting, secret scanning/push
protection where available, and protected-branch review. Never attach a public PR
workflow to a personal self-hosted runner. Review dependency updates; CI has read-only
repository permissions and no production endpoints, model credentials, or publishing
secrets. A local test pass is not proof that a hosted CI run or vulnerability scan passed.
