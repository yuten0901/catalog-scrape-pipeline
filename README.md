# Catalog Scrape Pipeline

A configurable data-collection pipeline that turns messy catalogue pages into clean CSV and JSON,
while preserving the rejected records, duplicate conflicts, and failed pages needed to audit a run.

## Business scenario

A parts distributor receives listings from a legacy catalogue, a JavaScript storefront, and a
partner mirror. The feeds disagree, contain malformed values, and sometimes fail. The deliverable
is not merely a list of products: it is a usable dataset plus evidence explaining every loss or
conflict.

## What it demonstrates

- Static extraction with `httpx` and client-rendered extraction with Playwright
- `next`-link and page-parameter pagination with loop and page-count guards
- YAML-defined sources, selectors, timeouts, delays, retries, and output formats
- Locale-aware money and date normalization without floating-point prices
- Explicit valid, rejected, duplicate, and page-failure datasets
- Retry with bounded backoff, `Retry-After`, request throttling, and robots.txt checks
- Content hashing and optional incremental/change-only exports
- Deterministic tests against an intentionally hostile local website

## Data collected

Each accepted product contains SKU, title, amount and ISO currency, availability, optional rating,
category and listing date, canonical URL, source/page provenance, scrape time, change status, and a
content hash. Required or unparsable data rejects the record instead of silently producing a blank.

## Architecture and data flow

```text
YAML config
    -> validated source definitions
    -> HTTP or Playwright fetcher
    -> CSS extraction into raw strings
    -> normalization and validation
    -> deduplication and change detection
    -> products / rejected / duplicates / failures
    -> CSV + JSON + run-report.json
```

The HTTP and browser transports share one robots policy and throttle. The pipeline owns pagination
and failure accounting; normalization and deduplication remain small, testable decisions.

## Static versus browser scraping

`catalog-static` follows real next links in server-rendered HTML. `catalog-js` loads the same domain
in Chromium, waits for an explicit rendered-state selector, and stops when a later page renders an
empty grid. Fetching that JS page statically yields no products by design.

## Data quality rules

- Prices are integer minor units with an ISO currency; no binary floats are stored.
- Ambiguous values such as `£7.505` use a per-source decimal-separator declaration.
- Date order is declared per source rather than guessed from `07/08/2026`.
- Missing optional fields remain null; present but unparsable fields reject the row.
- Duplicate keys keep the configured winner and report both values for every conflicting field.
- Unknown stock wording is retained as `unknown` and surfaced as a warning.

## Reliability and failure handling

Transient timeouts, connection errors, 429 responses, and 5xx responses are retried. Client errors
and robots refusals are not. Every source has a hard page cap, pagination cycles are detected, and
Playwright is closed even after failure. Partial results are written, but any failed page makes the
command exit 1. Configuration errors exit 2 before fetching.

## Setup

Requires Python 3.12+ and Chromium for the browser source.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
python -m pip install -e ".[dev]"
python -m playwright install chromium
```

## Run the demo

Terminal 1:

```bash
catalog-scrape serve-demo --port 8800
```

Terminal 2:

```bash
catalog-scrape validate --config config/demo-local.yaml
catalog-scrape scrape --config config/demo-local.yaml
```

The headline configuration deliberately includes a redesigned legacy feed. The run therefore exits
1 after preserving all usable rows and reporting `no_records` for that source. To demonstrate a
clean exit, select the healthy sources:

```bash
catalog-scrape scrape --config config/demo-local.yaml \
  --source catalog-static --source catalog-js --source mirror-flaky
```

Useful overrides:

```bash
catalog-scrape scrape --config config/demo-local.yaml --output-dir out/run-2
catalog-scrape scrape --config config/demo-local.yaml --only-changed
```

## Example result

The bundled scenario currently fetches 9 pages and finds 26 raw records. It exports 16 products,
rejects 6 malformed records, reports 4 duplicates (2 conflicting), and records 1 failed page. The
exact summary is asserted by the end-to-end pipeline test rather than maintained as an unchecked
documentation claim.

Output files are `products`, `rejected`, `duplicates`, and `failures` in both CSV and JSON, plus
`run-report.json`. CSV is Excel-friendly UTF-8 with a BOM; JSON preserves nested price and conflict
structures. A two-record [JSON output sample](examples/products.sample.json) is committed for a
quick review without running the project.

## Testing

```bash
python -m pytest
python -m ruff check .
python -m mypy catalog_scraper demo_site tests
```

The suite uses the shipped configuration file and a real local HTTP server. The browser-marked test
launches real Chromium. See [the test strategy](docs/test-strategy.md) for test levels, failure
semantics, and mutation checks. The recorded negative results are in
[the mutation-testing report](docs/mutation-testing.md).

## Project structure

```text
catalog_scraper/   pipeline, fetchers, extraction, normalization, export
config/            runnable YAML source definition
demo_site/         reproducible static/JS target with injectable failures
tests/             unit, HTTP integration, and full browser pipeline tests
docs/              focused engineering documentation
```

## Engineering decisions

- A bundled target replaces fragile live-site test dependencies.
- Playwright is used only where JavaScript genuinely produces the records.
- Four outcome datasets prevent “not inspected” or “failed” from appearing as success.
- Configuration rejects unknown keys and invalid selectors before making requests.
- Simple sequential processing keeps rate limiting and failure order explainable.

This deliberately avoids a distributed crawler, proxy rotation, CAPTCHA bypass, stealth browsing,
and other technology unrelated to the assignment.

## Responsible scraping

Use this only where automated access is authorized. Review site terms, identify the client with an
appropriate user agent, honor robots.txt, minimize request frequency, and collect only necessary
data. The bundled target exists so the complete demonstration is reproducible without placing load
on a third-party service.
