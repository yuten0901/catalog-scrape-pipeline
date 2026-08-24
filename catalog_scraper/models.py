"""The data model: what a run produces, and what it admits it failed to produce.

Four record types leave the pipeline, and keeping them distinct is the point:

============  =====================================================
``Product``   extracted, normalized, validated — safe to hand to a client
``Rejected``  found on the page but not trustworthy, with the reason why
``Duplicate`` a real record already represented by another one
``Failure``   a *page* that never yielded records at all
============  =====================================================

The fourth is the one that gets lost in hand-rolled scrapers. A page that
returned 503 three times and a page that legitimately contained nothing both
produce "no rows", and if they are not separated the output silently shrinks.
Every count in :class:`RunReport` exists so that "we did not look" can never be
displayed as "there was nothing there".
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Availability(StrEnum):
    """Normalized stock state.

    ``UNKNOWN`` is a real value, not a null: it means the site said something we
    do not have a mapping for. That is different from the site saying nothing,
    which leaves the field ``None``. Conflating the two would let an unmapped
    phrase like "Backorder - 3 weeks" quietly read as "no information".
    """

    IN_STOCK = "in_stock"
    OUT_OF_STOCK = "out_of_stock"
    PREORDER = "preorder"
    DISCONTINUED = "discontinued"
    UNKNOWN = "unknown"


class FailureKind(StrEnum):
    """Why a page produced nothing."""

    TIMEOUT = "timeout"
    CONNECTION_ERROR = "connection_error"
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    CLIENT_ERROR = "client_error"
    ROBOTS_DISALLOWED = "robots_disallowed"
    PARSE_ERROR = "parse_error"
    NO_RECORDS = "no_records"
    BROWSER_UNAVAILABLE = "browser_unavailable"


class ChangeStatus(StrEnum):
    """How a product compares with the previous run's state file."""

    NEW = "new"
    CHANGED = "changed"
    UNCHANGED = "unchanged"
    UNKNOWN = "unknown"
    """No state file was in use, so no comparison was made.

    Deliberately not folded into ``NEW``: "we have never seen this product" and
    "we did not check" must not print the same word.
    """


class DuplicateKind(StrEnum):
    """What kind of duplicate was found."""

    IDENTICAL = "identical"
    """Same key, byte-identical business fields. Dropped without comment."""

    CONFLICTING = "conflicting"
    """Same key, different values — e.g. two pages disagree on the price.

    Always reported with the fields that differ, because which copy was kept is
    a business decision the client may want to overrule.
    """


# ISO 4217 minor-unit digits for the currencies this project can parse. Only the
# exceptions are listed; everything else has two.
#
# This table is not decoration. An earlier version of this file assumed every
# currency had two decimal places, which silently turns "¥4,980" into ¥49.80 —
# a hundredfold error that looks entirely plausible in a spreadsheet and that no
# downstream check would ever catch. Zero-decimal currencies are common enough
# (JPY, KRW, plus most of the ones this project does not claim to support) that
# the assumption is not safe.
MINOR_UNIT_DIGITS: dict[str, int] = {"JPY": 0, "KRW": 0}
DEFAULT_MINOR_UNIT_DIGITS = 2


def minor_unit_digits(currency: str) -> int:
    """How many minor-unit digits ``currency`` has. Two unless listed otherwise."""
    return MINOR_UNIT_DIGITS.get(currency, DEFAULT_MINOR_UNIT_DIGITS)


class Money(BaseModel):
    """A price, stored as an integer number of minor units.

    Floats are the classic source of silent corruption in price scraping
    (``19.99`` does not round-trip), and this data ends up in spreadsheets that
    get summed. ``currency`` is ISO 4217 so that ``$`` from two different sites
    is not merged by accident, and the minor-unit scale is looked up per
    currency rather than assumed — see :data:`MINOR_UNIT_DIGITS`.
    """

    model_config = ConfigDict(frozen=True)

    currency: Annotated[str, Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")]
    minor: Annotated[int, Field(ge=0)]

    @property
    def digits(self) -> int:
        """Minor-unit digits for this currency: 2 for GBP, 0 for JPY."""
        return minor_unit_digits(self.currency)

    @property
    def decimal(self) -> Decimal:
        """The amount as a decimal, at this currency's own scale."""
        return Decimal(self.minor).scaleb(-self.digits)

    def __str__(self) -> str:
        return f"{self.decimal:.{self.digits}f} {self.currency}"


class RawRecord(BaseModel):
    """One record as it came off the page: strings and nulls, nothing else.

    Normalization is deliberately a later step. Keeping the raw strings intact
    means a rejected record can be exported with exactly what the page said,
    which is what someone debugging a selector actually needs.
    """

    source_id: str
    page_url: str
    page_no: int
    fields: dict[str, str | None]


class RejectionReason(BaseModel):
    """Why one field of one record was unusable."""

    model_config = ConfigDict(frozen=True)

    field_name: str
    code: str
    """Short, aggregatable: ``missing``, ``unparsable``, ``out_of_range``."""
    detail: str

    def __str__(self) -> str:
        return f"{self.field_name}:{self.code}"


class Product(BaseModel):
    """A validated catalogue record — the deliverable.

    Construction is validation: if a ``Product`` exists, every rule below held.
    There is no "valid" flag to forget to check.
    """

    model_config = ConfigDict(frozen=True)

    source_id: str
    sku: Annotated[str, Field(min_length=1, max_length=64)]
    title: Annotated[str, Field(min_length=1, max_length=300)]
    price: Money
    availability: Availability = Availability.UNKNOWN
    rating: Annotated[float, Field(ge=0.0, le=5.0)] | None = None
    category: Annotated[str, Field(max_length=120)] | None = None
    listed_on: date | None = None
    url: str
    page_no: int
    scraped_at: datetime

    @field_validator("url")
    @classmethod
    def _url_must_be_absolute_http(cls, value: str) -> str:
        """Reject anything that is not an absolute http(s) URL.

        Relative hrefs are resolved during normalization; one arriving here means
        the resolution step was skipped, and a CSV full of ``/catalog/3.html``
        is useless to the client. ``javascript:`` and ``data:`` hrefs are common
        in real markup and are not product links.
        """
        if not value.startswith(("http://", "https://")):
            raise ValueError(f"url must be an absolute http(s) URL, got {value!r}")
        return value

    @property
    def content_hash(self) -> str:
        """Stable hash of the *business* fields only.

        ``source_id``, ``page_no`` and ``scraped_at`` are excluded on purpose: a
        product that moved from page 2 to page 3 has not changed, and if the
        timestamp were included every product would be "changed" on every run,
        making change detection worthless.
        """
        payload = {
            "sku": self.sku,
            "title": self.title,
            "currency": self.price.currency,
            "minor": self.price.minor,
            "availability": self.availability.value,
            "rating": self.rating,
            "category": self.category,
            "listed_on": self.listed_on.isoformat() if self.listed_on else None,
            "url": self.url,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class RejectedRecord(BaseModel):
    """A record that was found but could not be trusted.

    Exported alongside the good rows rather than dropped. "We scraped 1,000
    products" means nothing without "and threw away 40, here they are".
    """

    source_id: str
    page_url: str
    page_no: int
    reasons: list[RejectionReason]
    raw_fields: dict[str, str | None]

    @property
    def reason_codes(self) -> list[str]:
        return [str(reason) for reason in self.reasons]


class DuplicateRecord(BaseModel):
    """A record dropped because another record already claimed its key."""

    source_id: str
    key: str
    kind: DuplicateKind
    kept_source_id: str
    kept_url: str
    dropped_url: str
    differing_fields: list[str] = Field(default_factory=list)
    """Empty for :attr:`DuplicateKind.IDENTICAL`; populated for conflicts."""

    kept_values: dict[str, str] = Field(default_factory=dict)
    dropped_values: dict[str, str] = Field(default_factory=dict)
    """The differing fields' actual values, on both sides.

    Naming the field ("they disagree about `price`") is not enough to act on:
    the question a client has is *which number is right*, and answering it from
    the field name alone means re-fetching both pages. Only the differing fields
    are carried, so a conflict on one field does not copy the whole record
    twice into the report."""


class PageFailure(BaseModel):
    """A page that never produced records."""

    source_id: str
    url: str
    page_no: int
    kind: FailureKind
    attempts: int
    message: str


class SourceReport(BaseModel):
    """Per-source counters. These must reconcile; see :meth:`RunReport.check`."""

    source_id: str
    kind: str
    pages_fetched: int = 0
    pages_failed: int = 0
    records_found: int = 0
    valid: int = 0
    rejected: int = 0
    duplicates_identical: int = 0
    duplicates_conflicting: int = 0
    stopped_because: str = "not_started"
    """Why pagination ended: ``no_next_link``, ``max_pages``, ``page_failed``…"""


class RunReport(BaseModel):
    """The machine-readable answer to "what happened during the run?".

    Written as ``run-report.json`` next to the data. The console summary is a
    rendering of this object, so the two can never disagree.
    """

    run_id: str
    started_at: datetime
    finished_at: datetime
    duration_seconds: float
    config_path: str
    config_sha256: str
    """Hash of the configuration file as loaded from disk.

    A run report that cannot be tied back to the exact configuration that
    produced it is not evidence of anything.
    """
    state_file: str | None
    sources: list[SourceReport] = Field(default_factory=list)
    failures: list[PageFailure] = Field(default_factory=list)
    duplicates: list[DuplicateRecord] = Field(default_factory=list)
    rejections_by_reason: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    exported: int = 0
    changes: dict[str, int] = Field(default_factory=dict)
    outputs: dict[str, str] = Field(default_factory=dict)
    exit_code: int = 0

    @property
    def totals(self) -> dict[str, int]:
        keys = (
            "pages_fetched",
            "pages_failed",
            "records_found",
            "valid",
            "rejected",
            "duplicates_identical",
            "duplicates_conflicting",
        )
        return {key: sum(getattr(source, key) for source in self.sources) for key in keys}

    def check(self) -> None:
        """Assert the counters reconcile. Called at the end of every run.

        Every record found on a page must be accounted for exactly once, as
        valid, rejected or duplicate. This is a self-audit rather than a test
        helper: a counting bug in the pipeline would otherwise show up as a
        plausible-looking report, which is worse than a crash.
        """
        for source in self.sources:
            accounted = (
                source.valid
                + source.rejected
                + source.duplicates_identical
                + source.duplicates_conflicting
            )
            if accounted != source.records_found:
                raise AssertionError(
                    f"source {source.source_id!r}: {source.records_found} records found but "
                    f"{accounted} accounted for (valid={source.valid} "
                    f"rejected={source.rejected} dup_identical={source.duplicates_identical} "
                    f"dup_conflicting={source.duplicates_conflicting})"
                )
        if self.exported != self.totals["valid"] and not self.changes.get("filtered_out"):
            raise AssertionError(
                f"{self.totals['valid']} valid records but {self.exported} exported"
            )

    def to_json(self) -> str:
        return json.dumps(self.model_dump(mode="json") | {"totals": self.totals}, indent=2)


def parse_report(payload: str | bytes) -> dict[str, Any]:
    """Load a report back from disk. Used by tests and by ``scripts/demo.py``."""
    return dict(json.loads(payload))
