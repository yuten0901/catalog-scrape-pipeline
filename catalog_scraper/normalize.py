"""Turning what the page said into something a spreadsheet can add up.

This module is where most of the value of the project sits. Extraction is
selectors; *normalization* is where real catalogue data fights back:

* ``£51.77``, ``$1,234.50``, ``1.234,50 €`` and ``USD 19.99`` are the same kind
  of thing written four ways, and two of them disagree about what ``,`` means.
* ``2026-08-01``, ``01/08/2026``, ``Aug 1, 2026`` and ``3 days ago`` are four
  dates, one of which is only meaningful relative to a clock.
* ``star-rating Three`` is a rating hidden in a CSS class.
* ``In stock (22 available)``, ``Unavailable`` and ``No longer available`` all
  contain the substring "available".

Every function here is pure — text in, typed value or
:class:`~catalog_scraper.errors.NormalizationError` out — which is why they are
tested from a table of roughly 90 cases rather than through the pipeline.

Two rules the whole module obeys:

1. **Never guess silently.** Anything unparsable raises with a short code that
   ends up in the run report. A price that quietly became ``0.00`` is worse than
   a rejected row.
2. **Never lose precision.** Money is integer minor units from the first moment
   it is a number; it is never a float.
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from catalog_scraper.errors import NormalizationError
from catalog_scraper.models import Availability, Money


class DateOrder(StrEnum):
    """How to read ``03/04/2026``.

    There is no way to infer this from the data — a site that only ever lists
    days 1-12 is genuinely ambiguous — so it is declared per source in the
    configuration and applied strictly. If a value contradicts the declaration
    (``13/04`` under ``mdy``) the record is rejected rather than "fixed", because
    silently swapping the fields would produce a plausible wrong date.
    """

    DMY = "dmy"
    MDY = "mdy"


@dataclass(frozen=True)
class NormalizationContext:
    """Everything a normalizer needs that is not the string itself."""

    page_url: str
    now: datetime
    date_order: DateOrder = DateOrder.DMY
    default_currency: str | None = None
    """Used only when the text carries no symbol and no ISO code."""


# --------------------------------------------------------------------------
# text
# --------------------------------------------------------------------------

_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍﻿­"), None)


def normalize_text(raw: str | None, _context: NormalizationContext | None = None) -> str | None:
    """Collapse real-world whitespace noise into a single clean line.

    Handles the four things that actually appear in scraped markup: HTML
    entities that survived extraction, non-breaking and narrow spaces (which are
    *not* matched by ``\\s`` in every context and break equality comparisons),
    zero-width characters pasted in from word processors, and newline/tab runs
    from pretty-printed HTML.

    Returns ``None`` for anything that is empty once cleaned, so "the element
    existed but was blank" and "the element was missing" converge — for a
    consumer they are the same absence.
    """
    if raw is None:
        return None

    text = html.unescape(raw)
    text = text.translate(_ZERO_WIDTH)
    # NFKC folds non-breaking/narrow spaces and full-width punctuation into their
    # ordinary forms, which is what makes the collapse below sufficient.
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


# --------------------------------------------------------------------------
# money
# --------------------------------------------------------------------------

# `$` is assumed to be USD. It could be CAD, AUD, SGD... but a source that mixes
# them is a source whose configuration should set `default_currency`, and an
# explicit ISO code in the text always wins over this table. Documented in
# docs/data-quality.md as an assumption rather than left implicit in a dict.
CURRENCY_SYMBOLS: dict[str, str] = {
    "£": "GBP",
    "$": "USD",
    "€": "EUR",
    "¥": "JPY",
    "₹": "INR",
    "₩": "KRW",
    "R$": "BRL",
    "CHF": "CHF",
}

_ISO_CODE = re.compile(r"\b([A-Z]{3})\b")
_NUMERIC = re.compile(r"\d[\d.,'\s]*\d|\d")
_TWO_PLACES = Decimal("0.01")


def normalize_money(raw: str | None, context: NormalizationContext) -> Money | None:
    """Parse a price string into an ISO currency and integer minor units.

    The hard part is the separator. ``1,234.50`` and ``1.234,50`` are the same
    amount under opposite conventions, and ``19,99`` and ``1,999`` differ only in
    how many digits follow the comma. The rules applied, in order:

    * If both ``.`` and ``,`` appear, the **last** one is the decimal separator
      and the other is grouping. This is true for every locale in practice.
    * If only one appears more than once, it is grouping (``1.234.567``).
    * If only one appears once, it is grouping when followed by exactly three
      digits (``1,234``) and a decimal separator otherwise (``19,99``).

    More than two decimal places is rejected rather than rounded: rounding a
    scraped price is data corruption that no downstream check can detect.
    """
    text = normalize_text(raw)
    if text is None:
        return None

    currency = _detect_currency(text) or context.default_currency
    if currency is None:
        raise NormalizationError(
            "no_currency",
            f"no currency symbol or ISO code in {text!r} and the source declares no "
            f"default_currency",
        )

    match = _NUMERIC.search(text)
    if match is None:
        raise NormalizationError("unparsable", f"no numeric amount in {text!r}")

    amount_text = re.sub(r"[\s']", "", match.group(0))
    canonical = _canonical_decimal_string(amount_text, original=text)

    try:
        amount = Decimal(canonical)
    except InvalidOperation as exc:  # pragma: no cover - guarded by the regex above
        raise NormalizationError("unparsable", f"cannot parse amount from {text!r}") from exc

    if amount < 0:
        raise NormalizationError("out_of_range", f"negative price in {text!r}")
    if amount != amount.quantize(_TWO_PLACES):
        raise NormalizationError(
            "unparsable",
            f"{text!r} has more than two decimal places; refusing to round a price",
        )

    return Money(currency=currency, minor=int(amount.scaleb(2)))


def _detect_currency(text: str) -> str | None:
    """An explicit ISO code beats a symbol; ``R$`` beats ``$``."""
    iso = _ISO_CODE.search(text)
    if iso and iso.group(1) in _KNOWN_ISO_CODES:
        return iso.group(1)
    for symbol in sorted(CURRENCY_SYMBOLS, key=len, reverse=True):
        if symbol in text:
            return CURRENCY_SYMBOLS[symbol]
    return None


_KNOWN_ISO_CODES = frozenset(CURRENCY_SYMBOLS.values()) | {
    "AUD",
    "CAD",
    "CNY",
    "DKK",
    "NOK",
    "NZD",
    "PLN",
    "SEK",
    "SGD",
}


def _canonical_decimal_string(amount: str, *, original: str) -> str:
    """Rewrite a grouped/localised number as a plain ``123.45`` string."""
    has_dot = "." in amount
    has_comma = "," in amount

    if has_dot and has_comma:
        decimal_sep = "." if amount.rfind(".") > amount.rfind(",") else ","
        grouping_sep = "," if decimal_sep == "." else "."
        return amount.replace(grouping_sep, "").replace(decimal_sep, ".")

    separator = "." if has_dot else ("," if has_comma else None)
    if separator is None:
        return amount

    if amount.count(separator) > 1:
        return amount.replace(separator, "")

    _, _, tail = amount.partition(separator)
    if len(tail) == 3:
        # `1,234` / `1.234`. Ambiguous in theory, grouping in practice: a price
        # with exactly three decimal places is far rarer than a thousands
        # separator, and treating it as decimals would divide the price by 1000.
        return amount.replace(separator, "")
    if len(tail) == 0:
        raise NormalizationError("unparsable", f"trailing separator in {original!r}")
    return amount.replace(separator, ".")


# --------------------------------------------------------------------------
# dates
# --------------------------------------------------------------------------

_MONTHS = {
    name.lower(): number
    for number, names in enumerate(
        [
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ],
        start=1,
    )
    for name in names
}

_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ].*)?$")
_SLASHED = re.compile(r"^(\d{1,4})[/.-](\d{1,2})[/.-](\d{2,4})$")
_MONTH_FIRST = re.compile(r"^([A-Za-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})$")
_DAY_FIRST = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\.?,?\s+(\d{4})$")
_RELATIVE = re.compile(r"^(\d+)\s+(day|days|week|weeks|hour|hours|minute|minutes)\s+ago$")


def normalize_date(raw: str | None, context: NormalizationContext) -> date | None:
    """Parse the date formats that actually turn up on catalogue pages.

    Supported: ISO dates and datetimes, ``dd/mm/yyyy`` or ``mm/dd/yyyy``
    according to the source's declared order, ``Aug 1, 2026``, ``1 August 2026``,
    and relative expressions (``today``, ``yesterday``, ``3 days ago``).

    Relative dates are resolved against the injected clock, not
    :func:`datetime.now` — otherwise the same fixture would parse to a different
    date tomorrow and the test suite would rot on its own.
    """
    text = normalize_text(raw)
    if text is None:
        return None

    today = context.now.astimezone(UTC).date()
    lowered = text.lower()

    if lowered == "today":
        return today
    if lowered == "yesterday":
        return date.fromordinal(today.toordinal() - 1)

    relative = _RELATIVE.match(lowered)
    if relative:
        quantity, unit = int(relative.group(1)), relative.group(2)
        days = quantity * 7 if unit.startswith("week") else quantity if unit.startswith("day") else 0
        return date.fromordinal(today.toordinal() - days)

    iso = _ISO.match(text)
    if iso:
        return _build_date(*(int(part) for part in iso.groups()), original=text)

    month_first = _MONTH_FIRST.match(text)
    if month_first:
        month = _MONTHS.get(month_first.group(1).lower())
        if month is None:
            raise NormalizationError("unparsable", f"unknown month name in {text!r}")
        return _build_date(int(month_first.group(3)), month, int(month_first.group(2)), text)

    day_first = _DAY_FIRST.match(text)
    if day_first:
        month = _MONTHS.get(day_first.group(2).lower())
        if month is None:
            raise NormalizationError("unparsable", f"unknown month name in {text!r}")
        return _build_date(int(day_first.group(3)), month, int(day_first.group(1)), text)

    slashed = _SLASHED.match(text)
    if slashed:
        first, second, third = (int(part) for part in slashed.groups())
        if len(slashed.group(1)) == 4:  # 2026/08/01
            return _build_date(first, second, third, text)
        year = third + 2000 if third < 100 else third
        if context.date_order is DateOrder.DMY:
            return _build_date(year, second, first, text)
        return _build_date(year, first, second, text)

    raise NormalizationError("unparsable", f"unrecognised date format {text!r}")


def _build_date(year: int, month: int, day: int, original: str) -> date:
    try:
        return date(year, month, day)
    except ValueError as exc:
        # This is where a `date_order` mismatch surfaces: `13/04/2026` read as
        # mm/dd gives month 13. Reporting it as unparsable is deliberate — the
        # alternative, swapping the fields, would turn a configuration mistake
        # into a plausible wrong date that nobody would ever notice.
        raise NormalizationError("unparsable", f"{original!r} is not a valid date: {exc}") from exc


# --------------------------------------------------------------------------
# ratings
# --------------------------------------------------------------------------

_WORD_RATINGS = {"zero": 0.0, "one": 1.0, "two": 2.0, "three": 3.0, "four": 4.0, "five": 5.0}
_NUMERIC_RATING = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:/|out\s+of|of)?\s*(\d+)?")


def normalize_rating(raw: str | None, _context: NormalizationContext | None = None) -> float | None:
    """Parse ``star-rating Three``, ``4.5 out of 5``, ``4/5``, ``7/10`` or ``3``.

    Word ratings hidden in a CSS class are the common case on real storefronts,
    which is why the configuration can point a field at an attribute rather than
    text. Values expressed out of 10 are rescaled to 5; any other denominator is
    rejected, because guessing the scale would silently halve or double a rating.
    """
    text = normalize_text(raw)
    if text is None:
        return None

    lowered = text.lower()
    for word, value in _WORD_RATINGS.items():
        # Matched as a whole word so that a class list like
        # `star-rating Three` works but `threefold` does not.
        if re.search(rf"\b{word}\b", lowered):
            return value

    match = _NUMERIC_RATING.search(lowered)
    if match is None:
        raise NormalizationError("unparsable", f"no rating in {text!r}")

    value = float(match.group(1).replace(",", "."))
    scale = int(match.group(2)) if match.group(2) else 5
    if scale == 10:
        value /= 2
    elif scale != 5:
        raise NormalizationError("unparsable", f"unsupported rating scale /{scale} in {text!r}")

    if not 0.0 <= value <= 5.0:
        raise NormalizationError("out_of_range", f"rating {value} outside 0-5 in {text!r}")
    return round(value, 2)


# --------------------------------------------------------------------------
# availability
# --------------------------------------------------------------------------

# Order matters and is the whole trick: "unavailable" and "no longer available"
# both contain "available", so the specific phrases must be tested first.
# tests/unit/test_normalize.py locks this ordering in place.
_AVAILABILITY_RULES: tuple[tuple[Availability, tuple[str, ...]], ...] = (
    (Availability.DISCONTINUED, ("discontinued", "no longer available", "withdrawn")),
    (
        Availability.OUT_OF_STOCK,
        ("out of stock", "out-of-stock", "sold out", "unavailable", "not in stock"),
    ),
    (Availability.PREORDER, ("pre-order", "preorder", "pre order", "coming soon")),
    (Availability.IN_STOCK, ("in stock", "in-stock", "available", "ships", "add to basket")),
)


def normalize_availability(
    raw: str | None, _context: NormalizationContext | None = None
) -> Availability | None:
    """Map free-text stock wording onto the :class:`Availability` enum.

    Returns ``None`` when the field was absent, and
    :attr:`Availability.UNKNOWN` when text was present but matched nothing. The
    caller records the second case as a warning naming the unmapped phrase, so a
    site that invents new wording shows up in the run report instead of quietly
    becoming a column of blanks.
    """
    text = normalize_text(raw)
    if text is None:
        return None

    lowered = text.lower()
    for availability, phrases in _AVAILABILITY_RULES:
        if any(phrase in lowered for phrase in phrases):
            return availability
    return Availability.UNKNOWN


# --------------------------------------------------------------------------
# urls
# --------------------------------------------------------------------------

_STRIP_FRAGMENT = re.compile(r"#.*$")


def normalize_url(raw: str | None, context: NormalizationContext) -> str | None:
    """Resolve a possibly relative href against the page it was found on.

    Fragments are dropped: ``/p/9#reviews`` and ``/p/9`` are the same product,
    and leaving the fragment in would make two rows out of one on any site that
    links to an anchor. ``javascript:`` and ``mailto:`` hrefs are rejected rather
    than resolved — they mean the selector matched a control, not a product link.
    """
    from urllib.parse import urljoin, urlsplit

    text = normalize_text(raw)
    if text is None:
        return None

    scheme = urlsplit(text).scheme.lower()
    if scheme and scheme not in {"http", "https"}:
        raise NormalizationError("unparsable", f"{text!r} is not an http(s) link")

    resolved = urljoin(context.page_url, text)
    resolved = _STRIP_FRAGMENT.sub("", resolved)
    if not resolved.startswith(("http://", "https://")):
        raise NormalizationError("unparsable", f"{text!r} did not resolve to an absolute URL")
    return resolved


NORMALIZERS: dict[str, Any] = {
    "text": normalize_text,
    "money": normalize_money,
    "date": normalize_date,
    "rating": normalize_rating,
    "availability": normalize_availability,
    "url": normalize_url,
}
"""The complete normalizer vocabulary.

Closed on purpose. Each product field is bound to exactly one of these in
:mod:`catalog_scraper.fields`, and the configuration cannot override the
binding — see docs/adr/ADR-002.
"""
