from __future__ import annotations

from datetime import UTC, datetime

from catalog_scraper.config import DedupeSettings
from catalog_scraper.dedupe import Deduplicator
from catalog_scraper.models import DuplicateKind, Money, Product


def product(*, sku: str = "NC-1", price_minor: int = 1200, source: str = "primary") -> Product:
    return Product(
        source_id=source,
        sku=sku,
        title="Hex bolt",
        price=Money(currency="USD", minor=price_minor),
        url="https://example.test/products/nc-1",
        page_no=1,
        scraped_at=datetime(2026, 8, 24, tzinfo=UTC),
    )


def test_keys_are_case_insensitive_without_hiding_source_spelling_difference() -> None:
    dedupe = Deduplicator(DedupeSettings())
    first = product(sku=" NC-1 ")
    duplicate = product(sku="nc-1", source="mirror")

    assert dedupe.add(first) is None
    report = dedupe.add(duplicate)

    assert report is not None
    assert report.kind is DuplicateKind.CONFLICTING
    assert report.differing_fields == ["sku"]
    assert dedupe.products() == [first]


def test_conflict_reports_both_prices_and_keep_first_policy() -> None:
    dedupe = Deduplicator(DedupeSettings(on_conflict="keep_first"))
    first = product(price_minor=1200)
    conflicting = product(price_minor=999, source="mirror")
    dedupe.add(first)

    report = dedupe.add(conflicting)

    assert report is not None
    assert report.kind is DuplicateKind.CONFLICTING
    assert report.differing_fields == ["price"]
    assert report.kept_values == {"price": "12.00 USD"}
    assert report.dropped_values == {"price": "9.99 USD"}
    assert dedupe.products() == [first]


def test_keep_last_policy_replaces_the_survivor_without_reordering() -> None:
    dedupe = Deduplicator(DedupeSettings(on_conflict="keep_last"))
    first = product(price_minor=1200)
    replacement = product(price_minor=999, source="mirror")
    dedupe.add(first)

    report = dedupe.add(replacement)

    assert report is not None
    assert report.kept_source_id == "mirror"
    assert dedupe.products() == [replacement]
