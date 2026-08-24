from __future__ import annotations

from typing import Any, cast

import pytest

from catalog_scraper.config import BrowserSettings
from catalog_scraper.errors import FetchError
from catalog_scraper.fetch.browser import BrowserFetcher
from catalog_scraper.fetch.robots import RobotsPolicy
from catalog_scraper.logging_setup import null_logger
from catalog_scraper.models import FailureKind


class AllowAllRobots:
    def check(self, url: str) -> None:
        del url


class NoWaitThrottle:
    def wait(self, url: str) -> float:
        del url
        return 0.0


class FailingBrowser:
    def __init__(self) -> None:
        self.closed = False

    def new_page(self, **kwargs: object) -> None:
        del kwargs
        raise RuntimeError("browser context creation failed")

    def close(self) -> None:
        self.closed = True


def test_new_page_failure_is_reported_as_page_failure_not_pipeline_crash() -> None:
    browser = FailingBrowser()
    fetcher = BrowserFetcher(
        BrowserSettings(),
        user_agent="catalog-scrape-pipeline/test",
        throttle=cast(Any, NoWaitThrottle()),
        robots=cast(RobotsPolicy, AllowAllRobots()),
        log=null_logger(),
    )
    fetcher._browser = cast(Any, browser)

    with pytest.raises(FetchError) as caught:
        fetcher.fetch("https://example.test/catalog", wait_for="#grid")
    fetcher.close()

    assert caught.value.kind is FailureKind.CONNECTION_ERROR
    assert "browser context creation failed" in str(caught.value)
    assert browser.closed is True
