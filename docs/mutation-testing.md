# Mutation Testing Results

Executed locally on 2026-08-24 with Python 3.14.6 and Chromium 151. Mutations were applied one at
a time, the named test was run, and the production code was restored immediately afterward. No
mutation is present in the repository history.

| Invariant | Temporary mutation | Test that failed | Observed failure |
|---|---|---|---|
| Source decimal-separator configuration reaches normalization | Replaced `source.decimal_separator` with `AUTO` in `build_product` | `test_shipped_config_runs_static_browser_retry_and_failure_paths` | An invalid three-decimal GBP row passed; exported count changed from 16 to 17 |
| HTTP 404 is not transient | Added `CLIENT_ERROR` to `FetchError.retryable` | `test_404_is_not_retried` | Attempts changed from 1 to 3 |
| Server `Retry-After` overrides a shorter local backoff | Replaced `max` with `min` in the delay calculation | `test_retry_after_header_overrides_shorter_backoff` | Recorded delay changed from 1.0s to 0.1s |
| Empty first page is a failed selector, not successful exhaustion | Made the first-page branch unreachable | `test_shipped_config_runs_static_browser_retry_and_failure_paths` | Run exit code changed from 1 to 0 |
| Duplicate conflict values stay structured in JSON | Disabled the duplicate-specific JSON branch | `test_shipped_config_runs_static_browser_retry_and_failure_paths` | Structured dictionary access failed because the value became a CSV-style string |

These checks demonstrate that the tests observe the production workflow and exported artifacts,
not only mock return values.
