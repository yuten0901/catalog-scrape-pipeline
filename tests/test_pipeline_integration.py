from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from catalog_scraper.clock import FixedClock, RecordingSleeper
from catalog_scraper.config import load_config
from catalog_scraper.pipeline import Pipeline
from demo_site.server import DemoSite


@pytest.mark.browser
def test_shipped_config_runs_static_browser_retry_and_failure_paths(
    demo_site: DemoSite, tmp_path: Path
) -> None:
    config = load_config("config/demo-local.yaml", env={"DEMO_BASE_URL": demo_site.base_url})
    run = config.run.model_copy(
        update={
            "output_dir": str(tmp_path / "out"),
            "state_file": str(tmp_path / "state.json"),
        }
    )
    config = config.model_copy(update={"run": run})

    report = Pipeline(
        config,
        clock=FixedClock(datetime(2026, 8, 24, 9, 0, tzinfo=UTC)),
        sleeper=RecordingSleeper(),
    ).run()

    assert report.exit_code == 1
    assert report.exported == 16
    assert report.totals == {
        "pages_fetched": 9,
        "pages_failed": 1,
        "records_found": 26,
        "valid": 16,
        "rejected": 6,
        "duplicates_identical": 2,
        "duplicates_conflicting": 2,
    }
    assert demo_site.hits("/flaky/mirror.html") == 3
    assert {failure.kind.value for failure in report.failures} == {"no_records"}
    assert set(report.rejections_by_reason) == {
        "listed_on:unparsable",
        "price:missing",
        "price:no_currency",
        "price:unparsable",
        "rating:unparsable",
        "title:missing",
    }

    products = json.loads((tmp_path / "out/products.json").read_text(encoding="utf-8"))
    duplicates = json.loads((tmp_path / "out/duplicates.json").read_text(encoding="utf-8"))
    assert len(products) == 16
    conflict = next(item for item in duplicates if item["key"] == "nc-1003")
    assert conflict["kept_values"]["price"] == "1234.50 EUR"
    assert conflict["dropped_values"]["price"] == "1199.00 EUR"
