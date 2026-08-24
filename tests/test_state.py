from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from catalog_scraper.errors import ConfigError
from catalog_scraper.models import ChangeStatus
from catalog_scraper.state import ChangeTracker


def test_disabled_tracking_reports_unknown_not_new() -> None:
    tracker = ChangeTracker.disabled()

    assert tracker.classify("sku", "hash") is ChangeStatus.UNKNOWN


def test_state_round_trip_distinguishes_new_changed_and_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    ChangeTracker(previous={}, enabled=True).save(
        path, {"same": "hash-1", "changed": "hash-old"}, now=datetime(2026, 8, 24, tzinfo=UTC)
    )

    tracker = ChangeTracker.load(path)

    assert tracker.classify("new", "hash") is ChangeStatus.NEW
    assert tracker.classify("same", "hash-1") is ChangeStatus.UNCHANGED
    assert tracker.classify("changed", "hash-new") is ChangeStatus.CHANGED


def test_corrupt_state_is_not_silently_treated_as_first_run(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("not json", encoding="utf-8")

    with pytest.raises(ConfigError, match="could not be read"):
        ChangeTracker.load(path)
