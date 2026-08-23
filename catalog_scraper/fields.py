"""The bridge between the configuration file and the data model.

This tiny table is the reason this project is a *pipeline* rather than a
scraping framework. A source's configuration says **where** each field is
(selector, attribute); it does not get to say **what** the field is. The type,
the normalizer and whether the field is required are properties of
:class:`~catalog_scraper.models.Product` and live here, in code.

Two consequences, both deliberate:

* A configuration file cannot declare ``price`` to be text and quietly ship a
  CSV column of ``"£51.77"`` strings.
* A configuration file that forgets to map a required field fails at load time
  with the field name in the message, not at export time with an empty column.

``tests/unit/test_fields.py`` asserts this table and ``Product`` stay in step —
adding a field to the model without describing it here (or the reverse) fails
the suite. That check exists because the two drift silently otherwise, and the
symptom is a column that is always blank.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FieldSpec:
    """How one product field is obtained and interpreted."""

    name: str
    normalizer: str
    """A key of :data:`catalog_scraper.normalize.NORMALIZERS`."""
    required: bool
    description: str


PRODUCT_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("sku", "text", True, "Supplier's product code; the default duplicate key."),
    FieldSpec("title", "text", True, "Product name as displayed."),
    FieldSpec("price", "money", True, "Price, parsed to ISO currency + minor units."),
    FieldSpec("url", "url", True, "Absolute link to the product page."),
    FieldSpec("availability", "availability", False, "Stock state, mapped to a closed enum."),
    FieldSpec("rating", "rating", False, "Customer rating rescaled to 0-5."),
    FieldSpec("category", "text", False, "Catalogue section the product was listed under."),
    FieldSpec("listed_on", "date", False, "Date the listing was published or last updated."),
)

FIELDS_BY_NAME: dict[str, FieldSpec] = {spec.name: spec for spec in PRODUCT_FIELDS}
REQUIRED_FIELDS: frozenset[str] = frozenset(spec.name for spec in PRODUCT_FIELDS if spec.required)
