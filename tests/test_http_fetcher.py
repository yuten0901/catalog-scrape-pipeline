from __future__ import annotations

from datetime import UTC, datetime

import pytest

from catalog_scraper.clock import FixedClock, RecordingSleeper
from catalog_scraper.config import HttpSettings
from catalog_scraper.errors import FetchError
from catalog_scraper.fetch.http import HttpFetcher
from catalog_scraper.logging_setup import null_logger
from catalog_scraper.models import FailureKind
from demo_site.server import DemoSite


def make_fetcher(site: DemoSite, sleeper: RecordingSleeper, attempts: int = 3) -> HttpFetcher:
    return HttpFetcher(
        HttpSettings(
            max_attempts=attempts,
            backoff_base_seconds=0.1,
            backoff_max_seconds=1,
            delay_seconds=0,
            respect_robots=True,
        ),
        clock=FixedClock(datetime(2026, 8, 24, tzinfo=UTC)),
        sleeper=sleeper,
        log=null_logger(),
    )


def test_transient_503_is_retried_until_real_server_recovers(demo_site: DemoSite) -> None:
    sleeper = RecordingSleeper()
    fetcher = make_fetcher(demo_site, sleeper)
    try:
        page = fetcher.fetch(f"{demo_site.base_url}/flaky/mirror.html")
    finally:
        fetcher.close()

    assert page.status == 200
    assert page.attempts == 3
    assert demo_site.hits("/flaky/mirror.html") == 3
    assert sleeper.delays == [0.1, 0.2]


def test_404_is_not_retried(demo_site: DemoSite) -> None:
    sleeper = RecordingSleeper()
    fetcher = make_fetcher(demo_site, sleeper)
    with pytest.raises(FetchError) as caught:
        try:
            fetcher.fetch(f"{demo_site.base_url}/missing")
        finally:
            fetcher.close()

    assert caught.value.kind is FailureKind.CLIENT_ERROR
    assert caught.value.attempts == 1
    assert demo_site.hits("/missing") == 1
    assert sleeper.delays == []


def test_retry_after_header_overrides_shorter_backoff(demo_site: DemoSite) -> None:
    sleeper = RecordingSleeper()
    fetcher = make_fetcher(demo_site, sleeper)
    try:
        page = fetcher.fetch(f"{demo_site.base_url}/limited/mirror.html")
    finally:
        fetcher.close()

    assert page.attempts == 2
    assert sleeper.delays == [1.0]
