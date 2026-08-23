"""``robots.txt``, honoured before the first page of every host is touched.

A portfolio scraper that ignores robots.txt is a portfolio scraper nobody
serious will run. This module keeps that promise cheap: one request per origin,
cached for the run, and a refusal is a normal, reported outcome rather than a
crash.

The status-code policy follows RFC 9309 §2.3.1:

======================  ==========================================
2xx                     parse and apply the rules
4xx (including 404)     no rules exist; everything is allowed
5xx / network failure   *disallow everything* for that host
======================  ==========================================

The last row is the one people get wrong. "We could not read the rules" is not
the same as "there are no rules", and defaulting it to allow means an outage on
the target's side turns this pipeline into the thing robots.txt was invented to
stop.
"""

from __future__ import annotations

from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from catalog_scraper.errors import FetchError
from catalog_scraper.models import FailureKind


class RobotsPolicy:
    """Per-origin robots.txt rules, fetched once and cached for the run."""

    def __init__(self, client: httpx.Client, user_agent: str, *, enabled: bool = True) -> None:
        self._client = client
        self._user_agent = user_agent
        self._enabled = enabled
        # origin -> parser, or None meaning "everything allowed here".
        self._cache: dict[str, RobotFileParser | None] = {}
        self._unreachable: dict[str, str] = {}

    def check(self, url: str) -> None:
        """Raise :class:`FetchError` if ``url`` must not be fetched."""
        if not self._enabled:
            return

        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._cache:
            self._load(origin)

        if origin in self._unreachable:
            raise FetchError(
                FailureKind.ROBOTS_DISALLOWED,
                f"robots.txt for {origin} could not be read ({self._unreachable[origin]}); "
                f"treating the host as disallowed. Set http.respect_robots=false only for "
                f"hosts you own or have written permission to crawl.",
            )

        parser = self._cache[origin]
        if parser is not None and not parser.can_fetch(self._user_agent, url):
            raise FetchError(
                FailureKind.ROBOTS_DISALLOWED,
                f"{url} is disallowed by {origin}/robots.txt for user-agent "
                f"{self._user_agent!r}",
            )

    def _load(self, origin: str) -> None:
        robots_url = f"{origin}/robots.txt"
        try:
            response = self._client.get(robots_url)
        except httpx.HTTPError as exc:
            self._cache[origin] = None
            self._unreachable[origin] = f"{type(exc).__name__}: {exc}"
            return

        if 400 <= response.status_code < 500:
            self._cache[origin] = None
            return
        if response.status_code >= 500:
            self._cache[origin] = None
            self._unreachable[origin] = f"HTTP {response.status_code}"
            return

        parser = RobotFileParser()
        parser.parse(response.text.splitlines())
        self._cache[origin] = parser
