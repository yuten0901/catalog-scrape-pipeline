"""Turning a page of HTML into records, and records into products or rejections.

Two steps, kept apart on purpose:

``extract_records``  HTML -> :class:`~catalog_scraper.models.RawRecord`. Pure
                     selector work. Everything is a string or ``None``.
``build_product``    RawRecord -> :class:`~catalog_scraper.models.Product` or
                     :class:`~catalog_scraper.models.RejectedRecord`.

The split is what makes a rejection useful. The raw strings survive the failure,
so ``rejected.csv`` shows what the page actually said next to the reason it was
unusable — which is the only thing that lets someone fix a selector without
re-running the crawl.

The rejection rule
------------------
A record is rejected when:

* a **required** field is absent or empty, or
* **any** mapped field was present but could not be parsed.

The second half is the interesting one. It would be gentler to null out an
unparsable optional field and keep the row, and that is what most scrapers do.
It is also how a scraper rots: "the site said something we do not understand" is
evidence that the page changed, and a column that quietly fills with blanks is
evidence nobody ever sees. An absent optional element is different — the site
said nothing, which is normal — and stays ``None`` without complaint.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from bs4 import BeautifulSoup, Tag

from catalog_scraper.config import SourceConfig
from catalog_scraper.errors import NormalizationError
from catalog_scraper.fields import FIELDS_BY_NAME, REQUIRED_FIELDS
from catalog_scraper.models import (
    Availability,
    Product,
    RawRecord,
    RejectedRecord,
    RejectionReason,
)
from catalog_scraper.normalize import NORMALIZERS, NormalizationContext

# lxml is used rather than html.parser because real catalogue pages contain
# unclosed tags and stray markup, and the two parsers build *different trees*
# from the same broken input. Pinning the parser here means a fixture that
# passes in the test suite parses identically in production.
_PARSER = "lxml"


def extract_records(
    html: str, *, source: SourceConfig, page_url: str, page_no: int
) -> list[RawRecord]:
    """Pull one :class:`RawRecord` per element matching ``record_selector``."""
    soup = BeautifulSoup(html, _PARSER)
    records: list[RawRecord] = []
    for element in soup.select(source.record_selector):
        fields = {
            name: _read_field(element, mapping.selector, mapping.attr)
            for name, mapping in source.fields.items()
        }
        records.append(
            RawRecord(source_id=source.id, page_url=page_url, page_no=page_no, fields=fields)
        )
    return records


def _read_field(element: Tag, selector: str, attr: str | None) -> str | None:
    """Read one field out of a record element.

    Returns ``None`` when the selector matched nothing (or the attribute was
    absent), and a possibly-empty string when it matched. The difference is
    preserved all the way to the rejection message: "your selector is wrong" and
    "the site left this blank" send whoever is debugging to different places.
    """
    found = element.select_one(selector)
    if found is None:
        return None
    if attr is None:
        return found.get_text(" ", strip=False)

    value: Any = found.get(attr)
    if value is None:
        return None
    # `class` and a few other attributes come back from BeautifulSoup as a list.
    # Joining rather than taking [0] is deliberate: ratings live in the *second*
    # class of `class="star-rating Three"`.
    if isinstance(value, list):
        return " ".join(str(item) for item in value)
    return str(value)


def build_product(
    raw: RawRecord, *, source: SourceConfig, now: datetime
) -> tuple[Product | RejectedRecord, list[str]]:
    """Normalize, validate, and return either a product or the reasons it failed.

    The second element is a list of warnings: things worth telling the operator
    that are not bad enough to throw the row away. Today that is exactly one
    case — stock wording the vocabulary does not cover — and it exists so that a
    site inventing a new phrase shows up in the run report instead of turning
    into a column of ``unknown``.
    """
    context = NormalizationContext(
        page_url=raw.page_url,
        now=now,
        date_order=source.date_order,
        default_currency=source.default_currency,
    )

    values: dict[str, Any] = {}
    reasons: list[RejectionReason] = []
    warnings: list[str] = []

    for name in source.fields:
        spec = FIELDS_BY_NAME[name]
        text = raw.fields.get(name)

        try:
            value = NORMALIZERS[spec.normalizer](text, context)
        except NormalizationError as exc:
            reasons.append(RejectionReason(field_name=name, code=exc.code, detail=str(exc)))
            continue

        if value is None:
            if name in REQUIRED_FIELDS:
                reasons.append(
                    RejectionReason(
                        field_name=name,
                        code="missing",
                        detail=(
                            f"selector {source.fields[name].selector!r} matched no element"
                            if text is None
                            else "element matched but contained no text"
                        ),
                    )
                )
            continue

        if value is Availability.UNKNOWN:
            warnings.append(
                f"{source.id}: unmapped availability wording {text!r} on {raw.page_url} "
                f"(recorded as 'unknown', not as missing)"
            )
        values[name] = value

    if reasons:
        return (
            RejectedRecord(
                source_id=raw.source_id,
                page_url=raw.page_url,
                page_no=raw.page_no,
                reasons=reasons,
                raw_fields=raw.fields,
            ),
            warnings,
        )

    try:
        product = Product(
            source_id=raw.source_id, page_no=raw.page_no, scraped_at=now, **values
        )
    except ValueError as exc:
        # The model's own constraints (length limits, absolute-URL check) firing
        # here means a value normalized cleanly but is still not usable. Same
        # outcome as any other rejection: reported, with the raw strings.
        return (
            RejectedRecord(
                source_id=raw.source_id,
                page_url=raw.page_url,
                page_no=raw.page_no,
                reasons=[
                    RejectionReason(field_name="<record>", code="invalid", detail=str(exc))
                ],
                raw_fields=raw.fields,
            ),
            warnings,
        )
    return product, warnings


def find_next_url(html: str, *, selector: str, page_url: str) -> str | None:
    """Resolve the ``next`` link on a page, or ``None`` if there is not one.

    Absence is the normal way pagination ends and is not an error. A link that
    exists but is not an http(s) URL (``href="#"``, ``javascript:void(0)``) is
    also treated as absence: following it would either re-fetch the same page
    forever or crash.
    """
    from urllib.parse import urljoin, urlsplit

    soup = BeautifulSoup(html, _PARSER)
    element = soup.select_one(selector)
    if element is None:
        return None
    href = element.get("href")
    if not isinstance(href, str) or not href.strip() or href.strip().startswith("#"):
        return None
    resolved = urljoin(page_url, href.strip())
    if urlsplit(resolved).scheme not in {"http", "https"}:
        return None
    return resolved
