from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from catalog_scraper.config import load_config
from catalog_scraper.extract import build_product, extract_records, find_next_url
from catalog_scraper.models import Product, RejectedRecord


def test_saved_malformed_fixture_extracts_raw_records_before_validation() -> None:
    config = load_config("config/demo-local.yaml", env={})
    source = config.source("catalog-static")
    html = Path("demo_site/pages/catalog-page-2.html").read_text(encoding="utf-8")

    records = extract_records(
        html, source=source, page_url="https://example.test/catalog/page-2.html", page_no=2
    )
    outcomes = [
        build_product(record, source=source, now=datetime(2026, 8, 24, tzinfo=UTC))[0]
        for record in records
    ]

    assert len(records) == 6
    assert all(isinstance(outcome, RejectedRecord) for outcome in outcomes)
    rejected = [outcome for outcome in outcomes if isinstance(outcome, RejectedRecord)]
    assert {code for outcome in rejected for code in outcome.reason_codes} == {
        "listed_on:unparsable",
        "price:missing",
        "price:no_currency",
        "price:unparsable",
        "rating:unparsable",
        "title:missing",
    }


def test_missing_optional_selector_is_null_but_required_selector_rejects() -> None:
    config = load_config("config/demo-local.yaml", env={})
    source = config.source("catalog-static")
    html = """
    <article class="product">
      <span class="sku">A-1</span><h3 class="title"><a href="/a-1"></a></h3>
      <span class="price">$12.00</span>
    </article>
    """

    raw = extract_records(html, source=source, page_url="https://example.test/list", page_no=1)[0]
    outcome, _ = build_product(raw, source=source, now=datetime(2026, 8, 24, tzinfo=UTC))

    assert raw.fields["category"] is None
    assert isinstance(outcome, RejectedRecord)
    assert outcome.reason_codes == ["title:missing"]


def test_valid_record_resolves_relative_product_url() -> None:
    config = load_config("config/demo-local.yaml", env={})
    source = config.source("catalog-static")
    html = Path("demo_site/pages/catalog-page-1.html").read_text(encoding="utf-8")

    raw = extract_records(
        html, source=source, page_url="https://example.test/catalog/page-1.html", page_no=1
    )[0]
    outcome, _ = build_product(raw, source=source, now=datetime(2026, 8, 24, tzinfo=UTC))

    assert isinstance(outcome, Product)
    assert outcome.url == "https://example.test/product/nc-1001.html"


def test_next_link_is_resolved_and_non_http_link_is_ignored() -> None:
    assert (
        find_next_url(
            '<a class="next" href="page-2.html">Next</a>',
            selector="a.next",
            page_url="https://example.test/catalog/page-1.html",
        )
        == "https://example.test/catalog/page-2.html"
    )
    assert (
        find_next_url(
            '<a class="next" href="javascript:void(0)">Next</a>',
            selector="a.next",
            page_url="https://example.test/catalog/page-1.html",
        )
        is None
    )
