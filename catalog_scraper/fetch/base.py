"""What every fetcher returns, and the politeness both of them obey.

There are two ways to get a page in this project — an HTTP request and a real
browser — and the pipeline is written against this one interface so that
switching a source from ``static`` to ``browser`` is a configuration change and
nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol
from urllib.parse import urlsplit

from catalog_scraper.clock import Clock, Sleeper


@dataclass(frozen=True)
class FetchedPage:
    """One successfully retrieved page."""

    url: str
    """The URL the content actually came from, *after* redirects.

    Relative links are resolved against this rather than against the requested
    URL. Getting that backwards silently produces wrong product URLs on any site
    that redirects ``/catalog`` to ``/catalog/``.
    """

    html: str
    status: int
    attempts: int
    """How many requests it took. 1 means it worked first time; anything higher
    is reported, because a source that needs three attempts per page is a source
    someone should look at."""

    fetched_with: Literal["static", "browser"]
    elapsed_seconds: float


class Fetcher(Protocol):
    """Retrieves a page, or raises :class:`~catalog_scraper.errors.FetchError`."""

    def fetch(self, url: str, *, wait_for: str | None = None) -> FetchedPage: ...

    def close(self) -> None: ...


class Throttle:
    """Keeps a minimum spacing between requests to the same host.

    Per host, not global: waiting half a second before touching a *different*
    server is pointless politeness that only makes runs slower. The spacing is
    measured from the end of one request to the start of the next, which is the
    conservative reading — a slow response does not earn the right to skip the
    delay.
    """

    def __init__(self, delay_seconds: float, *, clock: Clock, sleeper: Sleeper) -> None:
        self._delay = delay_seconds
        self._clock = clock
        self._sleeper = sleeper
        self._last_seen: dict[str, float] = {}

    def wait(self, url: str) -> float:
        """Sleep as long as politeness requires. Returns the seconds waited."""
        if self._delay <= 0:
            return 0.0
        host = urlsplit(url).netloc
        now = self._clock.now().timestamp()
        previous = self._last_seen.get(host)
        waited = 0.0
        if previous is not None:
            remaining = self._delay - (now - previous)
            if remaining > 0:
                self._sleeper.sleep(remaining)
                waited = remaining
        self._last_seen[host] = self._clock.now().timestamp()
        return waited
