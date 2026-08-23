"""Duplicate detection across pages and across sources.

The same SKU turns up twice for boring reasons: the catalogue re-orders itself
between two page requests, or a mirror lists a product the primary site also
lists. Both are normal. What is not normal is the two copies disagreeing, and
that distinction is the whole design here:

``identical``    same key, same content hash. Dropped silently — there is
                 nothing for anyone to decide.
``conflicting``  same key, different content. Dropped *and reported*, naming the
                 fields that differ, because "which price is right" is a
                 business question and this program is not entitled to answer it
                 quietly.

A scraper that reports one number ("1,284 products") after collapsing 40
conflicting pairs has destroyed information the client needed.
"""

from __future__ import annotations

from catalog_scraper.config import DedupeSettings
from catalog_scraper.models import DuplicateKind, DuplicateRecord, Money, Product

# The fields compared when two records share a key. `page_no`, `source_id` and
# `scraped_at` are excluded for the same reason they are excluded from
# `Product.content_hash`: a product appearing on a different page of a different
# site has not changed.
_COMPARED_FIELDS = ("sku", "title", "price", "availability", "rating", "category", "listed_on", "url")


class Deduplicator:
    """Keeps the first (or last) record per key and reports every collision."""

    def __init__(self, settings: DedupeSettings) -> None:
        self._settings = settings
        self._kept: dict[str, Product] = {}
        self._order: list[str] = []

    def key_for(self, product: Product) -> str:
        """Build the duplicate key from the configured fields.

        Values are rendered as strings and joined with a delimiter that cannot
        occur inside them, so that ``("AB", "C")`` and ``("A", "BC")`` are not
        the same key. That collision sounds theoretical until a key includes a
        title.
        """
        return "\x1f".join(_render(getattr(product, name)) for name in self._settings.key_fields)

    def add(self, product: Product) -> DuplicateRecord | None:
        """Offer a product. Returns a record describing the collision, if any."""
        key = self.key_for(product)
        existing = self._kept.get(key)
        if existing is None:
            self._kept[key] = product
            self._order.append(key)
            return None

        differing = _differing_fields(existing, product)
        kind = DuplicateKind.IDENTICAL if not differing else DuplicateKind.CONFLICTING

        if self._settings.on_conflict == "keep_last":
            self._kept[key] = product
            kept, dropped = product, existing
        else:
            kept, dropped = existing, product

        return DuplicateRecord(
            source_id=product.source_id,
            key=key,
            kind=kind,
            kept_source_id=kept.source_id,
            kept_url=kept.url,
            dropped_url=dropped.url,
            differing_fields=differing,
        )

    def products(self) -> list[Product]:
        """The survivors, in the order their keys were first seen.

        Insertion order rather than sorted order: it is the site's own ordering,
        which is usually meaningful (bestsellers, newest first), and destroying
        it for the sake of a tidy CSV throws away information for free.
        """
        return [self._kept[key] for key in self._order]


def _render(value: object) -> str:
    """Stringify a key component so that equal values render equally."""
    if value is None:
        return ""
    if isinstance(value, Money):
        return f"{value.minor}{value.currency}"
    if isinstance(value, str):
        # Keys are matched case-insensitively after whitespace collapsing:
        # `NC-1001` and `nc-1001` are the same SKU on every site anyone has
        # ever built, and treating them as different silently doubles the export.
        return " ".join(value.split()).casefold()
    return str(value)


def _differing_fields(left: Product, right: Product) -> list[str]:
    """Which business fields two records with the same key disagree about."""
    if left.content_hash == right.content_hash:
        return []
    return [name for name in _COMPARED_FIELDS if getattr(left, name) != getattr(right, name)]
