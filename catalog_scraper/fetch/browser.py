"""The browser fetcher: Playwright, used only where it is actually needed.

Running a browser costs roughly two orders of magnitude more CPU and memory than
an HTTP request, so a source is ``kind: browser`` only when the markup that
arrives over the wire genuinely does not contain the data. In this repository
that case is real and demonstrable: ``/js/catalog.html`` ships an empty ``<div>``
and builds its product list from JSON one frame after load, and
``tests/test_pipeline_integration.py`` asserts that the browser path renders
the records that are absent from the static response.

Two failure modes get explicit handling because both are common and both are
easy to hide.

**No browser binary.** ``playwright install chromium`` is a separate step from
``pip install playwright``, so "it works on my machine" is the default state of
this dependency. A missing binary produces a
:class:`~catalog_scraper.models.FailureKind.BROWSER_UNAVAILABLE` page failure —
a named line in the run report and a non-zero exit — rather than an empty CSV.

**The wait condition never arrives.** Every browser source must declare
``wait_for_selector`` (the configuration loader enforces it). Scraping "when the
page looks done" is a race that fails a few percent of the time, which is the
worst possible frequency: often enough to corrupt the data, rarely enough that
nobody reproduces it.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from catalog_scraper.config import BrowserSettings
from catalog_scraper.errors import FetchError
from catalog_scraper.fetch.base import FetchedPage, Throttle
from catalog_scraper.fetch.robots import RobotsPolicy
from catalog_scraper.logging_setup import RunLogger
from catalog_scraper.models import FailureKind

if TYPE_CHECKING:  # pragma: no cover - import cost is the point
    from playwright.sync_api import Browser, Playwright


class BrowserFetcher:
    """Renders pages in headless Chromium and returns the resulting DOM.

    The browser is started lazily on the first request and reused for every
    page; a fresh :class:`~playwright.sync_api.Page` is opened and **closed in a
    ``finally``** for each fetch. Reusing one page across a run leaks state
    (cookies, scroll position, half-finished timers) between pages, and leaking
    pages themselves is the standard way a long crawl runs a machine out of
    memory.
    """

    def __init__(
        self,
        settings: BrowserSettings,
        *,
        user_agent: str,
        throttle: Throttle,
        robots: RobotsPolicy,
        log: RunLogger,
    ) -> None:
        self._settings = settings
        # One identity for the whole run: a browser announcing itself differently
        # from the HTTP fetcher makes the target's logs unreadable, and makes the
        # robots.txt rules we obeyed different from the ones we were judged by.
        self._user_agent = user_agent
        self._throttle = throttle
        self._robots = robots
        self._log = log
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._pages_rendered = 0

    def fetch(self, url: str, *, wait_for: str | None = None) -> FetchedPage:
        if not self._settings.enabled:
            raise FetchError(
                FailureKind.BROWSER_UNAVAILABLE,
                "browser.enabled is false in the configuration, but this source is "
                "kind: browser. Enable it, or convert the source to kind: static.",
            )

        self._robots.check(url)
        self._throttle.wait(url)

        browser = self._ensure_browser()
        timeout_ms = self._settings.timeout_seconds * 1000
        started = time.monotonic()
        page = None
        try:
            page = browser.new_page(user_agent=self._user_agent)
            response = page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            status = response.status if response is not None else 0
            if status >= 400:
                raise FetchError(
                    FailureKind.SERVER_ERROR if status >= 500 else FailureKind.CLIENT_ERROR,
                    f"HTTP {status} from {url}",
                    http_status=status,
                )
            if wait_for:
                page.wait_for_selector(wait_for, timeout=timeout_ms, state="attached")
            html = page.content()
            final_url = page.url
        except FetchError:
            raise
        except Exception as exc:
            raise _as_fetch_error(exc, url, wait_for, self._settings.timeout_seconds) from exc
        finally:
            if page is not None:
                page.close()

        self._pages_rendered += 1
        return FetchedPage(
            url=final_url,
            html=html,
            status=status,
            attempts=1,
            fetched_with="browser",
            elapsed_seconds=time.monotonic() - started,
        )

    def _ensure_browser(self) -> Browser:
        if self._browser is not None:
            return self._browser
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - playwright is a hard dependency
            raise FetchError(
                FailureKind.BROWSER_UNAVAILABLE,
                f"playwright is not installed: {exc}. Run: pip install -e .",
            ) from exc

        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=self._settings.headless)
        except Exception as exc:
            self._shutdown()
            raise FetchError(
                FailureKind.BROWSER_UNAVAILABLE,
                f"could not launch Chromium: {exc}. The browser binary is installed "
                f"separately from the Python package: run `playwright install chromium`.",
            ) from exc

        self._log.info("browser.launched", headless=self._settings.headless)
        return self._browser

    def close(self) -> None:
        if self._browser is not None:
            self._log.info("browser.closed", pages_rendered=self._pages_rendered)
        self._shutdown()

    def _shutdown(self) -> None:
        """Release both handles, and do it even if the first release fails.

        A ``browser.close()`` that raises must not leak the Playwright driver
        process, which outlives the interpreter and is invisible in ``ps`` unless
        you know its name.
        """
        try:
            if self._browser is not None:
                self._browser.close()
        finally:
            self._browser = None
            if self._playwright is not None:
                self._playwright.stop()
            self._playwright = None


def _as_fetch_error(exc: Exception, url: str, wait_for: str | None, timeout: float) -> FetchError:
    """Map a Playwright exception onto the shared failure taxonomy."""
    name = type(exc).__name__
    if "Timeout" in name:
        if wait_for:
            return FetchError(
                FailureKind.TIMEOUT,
                f"{url} did not produce {wait_for!r} within {timeout}s. Either the page "
                f"is slower than the timeout, or the selector no longer matches.",
            )
        return FetchError(FailureKind.TIMEOUT, f"{url} did not load within {timeout}s")
    return FetchError(FailureKind.CONNECTION_ERROR, f"{name} while rendering {url}: {exc}")
