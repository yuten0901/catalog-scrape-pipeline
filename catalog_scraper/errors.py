"""Exception types.

Kept in one small module because the interesting property of this system is
*which* failures are recoverable, and that is easiest to see when the whole
taxonomy fits on one screen:

* :class:`ConfigError` — the run cannot start. Raised at load time, never
  mid-run, so a typo in a selector name is reported before a single request is
  made.
* :class:`FetchError` — one page could not be retrieved. Carries a
  :class:`~catalog_scraper.models.FailureKind` so the retry policy can decide
  whether trying again could plausibly help.
* :class:`NormalizationError` — one *field* could not be interpreted. Never
  fatal: it turns into a rejection reason attached to that record.

Nothing else raises. A page failure must not be able to end the run, because
partial results are still worth delivering.
"""

from __future__ import annotations

from catalog_scraper.models import FailureKind


class CatalogScraperError(Exception):
    """Base class, so a caller can catch everything this package raises."""


class ConfigError(CatalogScraperError):
    """The configuration file is unusable.

    Raised during loading only. Includes the offending path (e.g.
    ``sources[1].fields.price``) so the message points at a line rather than
    describing a symptom.
    """


class FetchError(CatalogScraperError):
    """A page could not be fetched.

    ``kind`` drives the retry decision and also appears verbatim in the run
    report, so the same word a client reads in ``run-report.json`` is the one
    the code branched on.
    """

    def __init__(
        self,
        kind: FailureKind,
        message: str,
        *,
        http_status: int | None = None,
        retry_after_seconds: float | None = None,
        attempts: int = 1,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.http_status = http_status
        self.retry_after_seconds = retry_after_seconds
        self.attempts = attempts
        """How many requests were made before giving up.

        Carried on the error rather than recomputed by the caller, because the
        caller does not know how many of the attempts happened -- and a failure
        report that says "1 attempt" after three retries is a lie that makes the
        retry policy look broken."""

    @property
    def retryable(self) -> bool:
        """Whether repeating the request could plausibly succeed.

        A 404 or a robots.txt refusal is not a transient condition; retrying it
        only delays the report and adds load to someone else's server.
        """
        return self.kind in {
            FailureKind.TIMEOUT,
            FailureKind.CONNECTION_ERROR,
            FailureKind.RATE_LIMITED,
            FailureKind.SERVER_ERROR,
        }


class NormalizationError(CatalogScraperError):
    """A raw string could not be converted to the field's type.

    Carries a short machine-readable ``code`` (``unparsable``, ``out_of_range``)
    rather than only prose, because rejection reasons are aggregated in the run
    report and prose does not aggregate.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
