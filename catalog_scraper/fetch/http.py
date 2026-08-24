"""The static fetcher: HTTP, retries, backoff and rate-limit awareness.

Three decisions in here are worth a client's attention.

**Only some failures are retried.** A 404 and a robots.txt refusal are not
transient; retrying them adds load to someone else's server and delays the
report by exactly ``max_attempts x backoff`` seconds for no gain. The decision
lives on :class:`~catalog_scraper.errors.FetchError.retryable`, next to the
failure taxonomy, rather than being spread through this loop.

**``Retry-After`` wins over the backoff curve.** When a server answers 429 and
says how long to wait, doubling our own number and ignoring theirs is how a
polite client gets blocked anyway.

**Backoff is not jittered.** Jitter exists to desynchronise many clients hitting
one server; this is a single sequential process whose requests are already
spaced by ``delay_seconds``. Adding randomness would buy nothing and would make
the delays in the run report unexplainable and the tests approximate. Documented
in docs/adr/ADR-003.
"""

from __future__ import annotations

import time

import httpx

from catalog_scraper.clock import Clock, Sleeper
from catalog_scraper.config import HttpSettings
from catalog_scraper.errors import FetchError
from catalog_scraper.fetch.base import FetchedPage, Throttle
from catalog_scraper.fetch.robots import RobotsPolicy
from catalog_scraper.logging_setup import RunLogger
from catalog_scraper.models import FailureKind


class HttpFetcher:
    """Fetches pages over HTTP with retry, backoff and politeness delays."""

    def __init__(
        self,
        settings: HttpSettings,
        *,
        clock: Clock,
        sleeper: Sleeper,
        log: RunLogger,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._sleeper = sleeper
        self._log = log
        self._client = httpx.Client(
            timeout=httpx.Timeout(settings.timeout_seconds),
            headers={"User-Agent": settings.user_agent},
            follow_redirects=True,
            transport=transport,
        )
        self._throttle = Throttle(settings.delay_seconds, clock=clock, sleeper=sleeper)
        self._robots = RobotsPolicy(
            self._client, settings.user_agent, enabled=settings.respect_robots
        )

    @property
    def throttle(self) -> Throttle:
        """Shared with the browser fetcher: politeness is per host, not per transport."""
        return self._throttle

    @property
    def robots(self) -> RobotsPolicy:
        """Shared with the browser fetcher: one identity, one set of rules obeyed."""
        return self._robots

    def fetch(self, url: str, *, wait_for: str | None = None) -> FetchedPage:
        """Retrieve ``url``, retrying transient failures.

        ``wait_for`` is accepted and ignored so that this class satisfies the
        same protocol as the browser fetcher. The configuration loader already
        rejects a static source that sets it, so it can never arrive here by
        accident.
        """
        del wait_for

        self._robots.check(url)

        last_error: FetchError | None = None
        for attempt in range(1, self._settings.max_attempts + 1):
            self._throttle.wait(url)
            started = time.monotonic()
            try:
                response = self._client.get(url)
            except httpx.TimeoutException as exc:
                last_error = FetchError(
                    FailureKind.TIMEOUT,
                    f"no response within {self._settings.timeout_seconds}s: {exc}",
                )
            except httpx.HTTPError as exc:
                last_error = FetchError(
                    FailureKind.CONNECTION_ERROR, f"{type(exc).__name__}: {exc}"
                )
            else:
                error = _classify(response)
                if error is None:
                    return FetchedPage(
                        url=str(response.url),
                        html=response.text,
                        status=response.status_code,
                        attempts=attempt,
                        fetched_with="static",
                        elapsed_seconds=time.monotonic() - started,
                    )
                last_error = error

            if not last_error.retryable or attempt == self._settings.max_attempts:
                last_error.attempts = attempt
                raise last_error

            delay = self._backoff_for(attempt, last_error)
            self._log.warning(
                "fetch.retry",
                url=url,
                attempt=attempt,
                of=self._settings.max_attempts,
                kind=last_error.kind.value,
                reason=str(last_error),
                sleeping_seconds=round(delay, 3),
            )
            self._sleeper.sleep(delay)

        raise AssertionError("unreachable: the loop either returns or raises")  # pragma: no cover

    def _backoff_for(self, attempt: int, error: FetchError) -> float:
        exponential = self._settings.backoff_base_seconds * (2 ** (attempt - 1))
        delay = min(exponential, self._settings.backoff_max_seconds)
        if error.retry_after_seconds is not None:
            # The server told us how long to wait. Honour it even if it is longer
            # than our own cap: the cap protects the run's duration, but ignoring
            # an explicit Retry-After is how a polite client gets banned.
            delay = max(delay, error.retry_after_seconds)
        return float(delay)

    def close(self) -> None:
        self._client.close()


def _classify(response: httpx.Response) -> FetchError | None:
    """Turn a response into a failure, or ``None`` if it is usable."""
    status = response.status_code
    if status < 400:
        return None
    if status == 429:
        return FetchError(
            FailureKind.RATE_LIMITED,
            f"HTTP 429 from {response.url}",
            http_status=status,
            retry_after_seconds=_retry_after(response),
        )
    if status >= 500:
        return FetchError(
            FailureKind.SERVER_ERROR,
            f"HTTP {status} from {response.url}",
            http_status=status,
            retry_after_seconds=_retry_after(response),
        )
    return FetchError(
        FailureKind.CLIENT_ERROR, f"HTTP {status} from {response.url}", http_status=status
    )


def _retry_after(response: httpx.Response) -> float | None:
    """Read ``Retry-After``, seconds form only.

    The HTTP-date form is legal but rare, and parsing it correctly requires
    trusting the server's clock against ours. Ignoring it falls back to the
    normal backoff curve, which is the safe direction to be wrong in.
    """
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None
