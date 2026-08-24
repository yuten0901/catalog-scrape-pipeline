from __future__ import annotations

from datetime import UTC, datetime

import pytest

from catalog_scraper.errors import NormalizationError
from catalog_scraper.normalize import (
    DecimalSeparator,
    NormalizationContext,
    normalize_date,
    normalize_money,
)


def context(separator: DecimalSeparator = DecimalSeparator.AUTO) -> NormalizationContext:
    return NormalizationContext(
        page_url="https://example.test/catalog",
        now=datetime(2026, 8, 24, 9, 0, tzinfo=UTC),
        decimal_separator=separator,
    )


@pytest.mark.parametrize(
    ("raw", "separator", "amount", "currency"),
    [
        ("$1,234.50", DecimalSeparator.AUTO, "1234.50", "USD"),
        ("1.234,50 EUR", DecimalSeparator.AUTO, "1234.50", "EUR"),
        ("£7.505", DecimalSeparator.AUTO, "7505.00", "GBP"),
    ],
)
def test_money_normalization(
    raw: str, separator: DecimalSeparator, amount: str, currency: str
) -> None:
    money = normalize_money(raw, context(separator))

    assert money is not None
    assert str(money.decimal) == amount
    assert money.currency == currency


def test_declared_decimal_separator_prevents_ambiguous_price_corruption() -> None:
    with pytest.raises(NormalizationError, match="decimal places"):
        normalize_money("£7.505", context(DecimalSeparator.DOT))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("2026-08-01", "2026-08-01"), ("3 days ago", "2026-08-21")],
)
def test_date_normalization(raw: str, expected: str) -> None:
    assert str(normalize_date(raw, context())) == expected
