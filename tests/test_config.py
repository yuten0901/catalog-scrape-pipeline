from __future__ import annotations

from pathlib import Path

import pytest

from catalog_scraper.config import load_config
from catalog_scraper.errors import ConfigError
from catalog_scraper.normalize import DecimalSeparator


def test_shipped_config_is_loaded_from_disk_and_expands_base_url() -> None:
    config = load_config(
        Path("config/demo-local.yaml"), env={"DEMO_BASE_URL": "http://127.0.0.1:9123"}
    )

    assert config.source("catalog-static").start_url.startswith("http://127.0.0.1:9123/")
    assert config.source("catalog-static").decimal_separator is DecimalSeparator.DOT
    assert config.http.max_attempts == 3
    assert config.run.formats == ["csv", "json"]


def test_unknown_config_key_is_a_loud_error(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(
        """version: 1
run:
  output_dir: out
  typo_format: json
sources: []
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="typo_format"):
        load_config(path, env={})


def test_missing_environment_variable_is_not_replaced_with_empty_text(tmp_path: Path) -> None:
    path = tmp_path / "missing-env.yaml"
    path.write_text("url: '${MISSING}/catalog'\n", encoding="utf-8")

    with pytest.raises(ConfigError, match=r"MISSING.*not set"):
        load_config(path, env={})
