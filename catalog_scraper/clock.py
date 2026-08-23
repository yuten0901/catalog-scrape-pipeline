"""Time, as an injected dependency.

Two things in this pipeline depend on the clock: relative dates on pages
("3 days ago") and retry spacing. Both are behaviours a client cares about, and
neither is testable if the code calls :func:`time.sleep` and
:meth:`datetime.now` directly — the tests become either slow or flaky, and
"3 days ago" silently means something different every day.

So the pipeline takes a :class:`Clock` and a :class:`Sleeper`. In production
they are the real ones; in tests they are frozen and recording, which is why the
retry and timeout tests finish in milliseconds and assert exact delays instead
of ranges.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    """Source of the current time."""

    def now(self) -> datetime:
        """Current time, timezone-aware and in UTC."""
        ...


class Sleeper(Protocol):
    """Somewhere to spend a delay."""

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """The real clock. Always UTC — a scrape run is compared across days."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class SystemSleeper:
    """The real sleeper."""

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class FixedClock:
    """A clock pinned to one instant, advanceable by hand.

    Lives in the package rather than in ``tests/`` because ``scripts/demo.py``
    uses it too, to make the committed example output reproducible.
    """

    def __init__(self, instant: datetime) -> None:
        if instant.tzinfo is None:
            raise ValueError("FixedClock requires a timezone-aware datetime")
        self._instant = instant.astimezone(UTC)

    def now(self) -> datetime:
        return self._instant

    def advance(self, seconds: float) -> None:
        self._instant = datetime.fromtimestamp(self._instant.timestamp() + seconds, UTC)


class RecordingSleeper:
    """Records requested delays instead of taking them."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.delays.append(seconds)

    @property
    def total(self) -> float:
        return sum(self.delays)
