"""Writing the four datasets a run produces, plus the report that explains them.

Four files, not one, because a client asking "how many products are there?"
and a developer asking "why is this one missing?" need different tables:

``products``    the deliverable
``rejected``    found, not trustworthy, with the raw strings and the reason
``duplicates``  dropped because another record had the same key
``failures``    pages that never yielded records at all

They are written even when empty. An absent ``rejected.csv`` is ambiguous — no
rejections, or the export step never ran? — and a zero-row file with a header is
not. This is the same principle as ``ChangeStatus.UNKNOWN``: the artefact must
distinguish "nothing to report" from "nobody looked".

Money is written as a decimal string next to its ISO currency, never as a float.
``51.77`` parsed as a float and written back out is not always ``51.77``, and
this file ends up in a spreadsheet that sums a column.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from catalog_scraper.models import (
    ChangeStatus,
    DuplicateRecord,
    PageFailure,
    Product,
    RejectedRecord,
    RunReport,
)

PRODUCT_COLUMNS: tuple[str, ...] = (
    "sku",
    "title",
    "price_amount",
    "price_currency",
    "availability",
    "rating",
    "category",
    "listed_on",
    "url",
    "source_id",
    "page_no",
    "scraped_at",
    "change",
    "content_hash",
)


@dataclass(frozen=True)
class ProductRow:
    """A product plus the two things only the pipeline knows about it."""

    product: Product
    key: str
    change: ChangeStatus


def write_outputs(
    *,
    output_dir: Path,
    formats: Sequence[str],
    rows: Sequence[ProductRow],
    rejected: Sequence[RejectedRecord],
    duplicates: Sequence[DuplicateRecord],
    failures: Sequence[PageFailure],
    csv_encoding: str = "utf-8-sig",
) -> dict[str, str]:
    """Write every dataset in every configured format. Returns name -> path."""
    output_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}

    datasets: tuple[tuple[str, tuple[str, ...], list[dict[str, Any]]], ...] = (
        ("products", PRODUCT_COLUMNS, [_product_dict(row) for row in rows]),
        ("rejected", _REJECTED_COLUMNS, [_rejected_dict(item) for item in rejected]),
        ("duplicates", _DUPLICATE_COLUMNS, [_duplicate_dict(item) for item in duplicates]),
        ("failures", _FAILURE_COLUMNS, [_failure_dict(item) for item in failures]),
    )

    for name, columns, records in datasets:
        if "csv" in formats:
            path = output_dir / f"{name}.csv"
            _write_csv(path, columns, records, encoding=csv_encoding)
            written[f"{name}.csv"] = str(path)
        if "json" in formats:
            path = output_dir / f"{name}.json"
            _write_json(path, name, records, rows)
            written[f"{name}.json"] = str(path)

    return written


def write_report(output_dir: Path, report: RunReport) -> str:
    path = output_dir / "run-report.json"
    path.write_text(report.to_json(), encoding="utf-8")
    return str(path)


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def _product_dict(row: ProductRow) -> dict[str, Any]:
    product = row.product
    return {
        "sku": product.sku,
        "title": product.title,
        "price_amount": f"{product.price.decimal:.{product.price.digits}f}",
        "price_currency": product.price.currency,
        "availability": product.availability.value,
        "rating": product.rating,
        "category": product.category,
        "listed_on": product.listed_on.isoformat() if product.listed_on else None,
        "url": product.url,
        "source_id": product.source_id,
        "page_no": product.page_no,
        "scraped_at": product.scraped_at.isoformat(),
        "change": row.change.value,
        "content_hash": product.content_hash,
    }


_REJECTED_COLUMNS = ("source_id", "page_no", "page_url", "reasons", "detail", "raw")


def _rejected_dict(item: RejectedRecord) -> dict[str, Any]:
    return {
        "source_id": item.source_id,
        "page_no": item.page_no,
        "page_url": item.page_url,
        # Machine-readable codes first ("price:unparsable"), so a client can
        # count them, and the prose after, so a developer can act on them.
        "reasons": "; ".join(item.reason_codes),
        "detail": " | ".join(reason.detail for reason in item.reasons),
        "raw": json.dumps(item.raw_fields, ensure_ascii=False, sort_keys=True),
    }


_DUPLICATE_COLUMNS = (
    "key",
    "kind",
    "differing_fields",
    "kept_values",
    "dropped_values",
    "kept_source_id",
    "kept_url",
    "dropped_source_id",
    "dropped_url",
)


def _duplicate_dict(item: DuplicateRecord) -> dict[str, Any]:
    return {
        # \x1f is the key's internal delimiter; it must not reach a CSV cell.
        "key": item.key.replace("\x1f", " | "),
        "kind": item.kind.value,
        "differing_fields": ", ".join(item.differing_fields),
        "kept_values": _render_values(item.kept_values),
        "dropped_values": _render_values(item.dropped_values),
        "kept_source_id": item.kept_source_id,
        "kept_url": item.kept_url,
        "dropped_source_id": item.source_id,
        "dropped_url": item.dropped_url,
    }


def _render_values(values: dict[str, str]) -> str:
    return "; ".join(f"{name}={value}" for name, value in values.items())


_FAILURE_COLUMNS = ("source_id", "page_no", "url", "kind", "attempts", "message")


def _failure_dict(item: PageFailure) -> dict[str, Any]:
    return {
        "source_id": item.source_id,
        "page_no": item.page_no,
        "url": item.url,
        "kind": item.kind.value,
        "attempts": item.attempts,
        "message": item.message,
    }


def _write_csv(
    path: Path, columns: tuple[str, ...], records: Iterable[dict[str, Any]], *, encoding: str
) -> None:
    """Write a CSV that Excel opens correctly.

    ``utf-8-sig`` writes a byte-order mark. Without it Excel on Windows decodes
    UTF-8 CSV as cp1252, and this dataset is full of ``£`` and ``€`` — the
    corruption lands squarely on the price column. Every standard CSV reader
    (Python's own included) strips the mark, so nothing downstream notices; set
    ``run.csv_encoding: utf-8`` if a consumer of yours is the exception.

    ``lineterminator="\\n"`` overrides csv's ``\\r\\n`` default so the files
    diff cleanly and look the same on every platform.
    """
    with path.open("w", encoding=encoding, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for record in records:
            writer.writerow({column: _csv_cell(record.get(column)) for column in columns})


def _csv_cell(value: Any) -> str:
    """Render one cell. ``None`` becomes empty, never the string ``"None"``."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _write_json(
    path: Path, name: str, records: list[dict[str, Any]], rows: Sequence[ProductRow]
) -> None:
    """Write a JSON array.

    An array rather than a wrapper object, so ``jq '.[] | select(...)'`` works
    without anyone reading the schema first. Run metadata lives in
    ``run-report.json``; mixing it in here would force every consumer to unwrap.
    """
    if name == "products":
        payload: list[dict[str, Any]] = [
            record | {"price": _price_object(row)}
            for record, row in zip(records, rows, strict=True)
        ]
    else:
        payload = records
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )


def _price_object(row: ProductRow) -> dict[str, Any]:
    """Price as a nested object in JSON, where structure is free.

    The flat ``price_amount``/``price_currency`` columns stay in the CSV output
    (and in the JSON, for symmetry) because a spreadsheet cannot address a
    nested field.
    """
    price = row.product.price
    return {
        "currency": price.currency,
        "amount": f"{price.decimal:.{price.digits}f}",
        "minor_units": price.minor,
        "minor_unit_digits": price.digits,
    }
