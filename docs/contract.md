# Contract reference

Start with `examples/contract.json`. Unknown top-level, case, acceptance, and message
fields are rejected rather than ignored. Values must be bounded JSON: duplicate keys,
NaN, Infinity, booleans masquerading as numbers, duplicate case IDs, and invalid limits
are rejected.

## Required fields

`schema_version` is `release-assurance-contract/v1`; `name` is non-empty. `cases`
contains 1–1000 cases. `repeats` defaults to 1 and is limited to 1–100. Total paired
requests must not exceed 10,000. Case IDs are unique portable identifiers.

Each case has `id` and non-empty text `messages` using system/user/assistant/tool
roles. Supported request fields are temperature, max_tokens, stream, tools,
tool_choice, response_format, top_p, and seed. The tool uses a single completion;
multimodal and legacy function-call requests are unsupported. For streaming it requests
`stream_options.include_usage`; endpoints must support that option.

## Compatibility assertions

- `expected_json_schema`: inline Draft 2020-12 structural schema. References and
  `format` assertions are explicitly unsupported. Remote schemas are never fetched.
  Use structural constraints rather than assuming an annotation is enforced.
- `exact_json`: response JSON must equal this value; works without a separate schema,
  including JSON null. Numeric representation is compared canonically.
- `expected_text_contains`: every listed non-empty string must occur in response text.
- `required_tool_calls`: each named function must appear with valid JSON-object
  arguments. Optional `arguments` specify a recursive subset of required object fields;
  arrays and scalar values match exactly. Returned tools are never executed.
- `safe_to_store`: false by default. True allows retention of synthetic response text
  and tool arguments up to 64 KiB; larger responses are explicitly omitted, not truncated
  without notice. Never enable this for secrets or customer data.

All requested compatibility checks must pass. Nonzero failure/malformed-rate allowances
are not supported in v0.1 and are rejected. Ordinary transport failures are not
compatibility proof: they make the run INCONCLUSIVE. A broken baseline is INCONCLUSIVE,
not evidence that a candidate regressed.

## Acceptance limits

`min_samples` is the minimum successful samples per deployment. Optional limits:

| Field | Observed quantity |
|---|---|
| max_latency_regression_pct | Candidate p95 total-response latency change vs baseline |
| max_ttft_regression_pct | Candidate p95 first-content-event latency change |
| max_tpot_regression_pct | Candidate p95 streamed time/token estimate change |
| max_p95_latency_ms | Candidate p95 total-response latency |
| max_p95_ttft_ms | Candidate p95 first-content-event latency |
| max_p95_tpot_ms | Candidate p95 streamed time/token estimate |
| min_requests_per_second | Candidate serial request rate, not concurrency capacity |

Limits are finite nonnegative numbers or null. Missing a required metric makes the run
INCONCLUSIVE. A zero/invalid baseline for a relative limit is INCONCLUSIVE. A threshold
exceeded by observed samples is FAIL, not a statistical significance claim.

`environment.candidate_must_match` names operator-supplied environment fields that must
be known and equal. Unknown or mismatching required fields are INCONCLUSIVE. Deployment
metadata can be registered with `--environment-json`; it is not remotely attested.

Changing a contract or registration during an incomplete run is rejected. Start a new
project when intentionally changing the experiment. Existing completed evidence remains
on disk, but latest verification fails if current inputs no longer match that run.
