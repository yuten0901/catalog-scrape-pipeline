# Test Strategy

## Purpose

The suite checks the pipeline's decisions, not merely its helpers. The highest-value test loads
`config/demo-local.yaml`, starts the bundled site on an ephemeral port, drives both `httpx` and a
real Chromium browser, and inspects the exported files. This catches drift between configuration,
orchestration, transports, and serialization.

## Levels

- Unit tests cover ambiguous currency parsing, date normalization, configuration validation, and
  environment expansion.
- HTTP integration tests use a real local server to prove retry boundaries, `Retry-After`, and
  non-retryable 404 behavior.
- The browser-marked pipeline test runs static pagination, JavaScript rendering, retry recovery,
  rejection, deduplication, partial failure, CSV export, and JSON export together.

No test contacts a public website. The target, pages, browser behavior, and injected failures are
controlled by this repository.

## Failure semantics

A skipped or uncollected test is not a pass. Pytest uses strict markers and strict xfail behavior.
The full suite requires Chromium; CI installs it explicitly. A run with a failed page must return
exit code 1 while preserving partial output. Invalid configuration returns exit code 2 before any
request.

## Mutation checks

Before release, temporarily reverse each invariant and confirm the named test fails:

1. Remove `decimal_separator` from `build_product`: the ambiguous-price test and pipeline test fail.
2. Retry HTTP 404 responses: `test_404_is_not_retried` fails on request count.
3. Stop honoring `Retry-After`: `test_retry_after_header_overrides_shorter_backoff` fails.
4. Convert an empty first page into successful pagination exhaustion: the pipeline test fails on
   exit code and failure taxonomy.
5. Flatten duplicate values in JSON: the pipeline test fails on structured conflict values.

Mutation results must be reported separately from the ordinary green suite; the mutations are
never committed.
