"""Change detection between runs.

The state file is a map from duplicate key to content hash — nothing else, no
copies of the data. That keeps it small enough to commit alongside a scheduled
job, and it means the interesting question ("what moved since yesterday?") is
answered without a database.

The one rule this module exists to enforce: **no state file means ``unknown``,
never ``new``.** The first run of a nightly job would otherwise report every
product as new, and so would a run where somebody deleted the file, and so would
a run where the path was misspelled. Three very different situations printing the
same word is how a change report becomes noise nobody reads.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from catalog_scraper.errors import ConfigError
from catalog_scraper.models import ChangeStatus

STATE_VERSION = 1


@dataclass
class ChangeTracker:
    """Compares this run's products against the previous run's hashes."""

    previous: dict[str, str]
    enabled: bool

    @classmethod
    def disabled(cls) -> ChangeTracker:
        return cls(previous={}, enabled=False)

    @classmethod
    def load(cls, path: str | Path) -> ChangeTracker:
        """Read a state file. A missing file is a first run, not an error.

        A *corrupt* file is an error, though. Silently starting from scratch
        would mean every product is reported as new, and the run would look
        successful while the change report was meaningless.
        """
        state_path = Path(path)
        if not state_path.exists():
            return cls(previous={}, enabled=True)
        try:
            document = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(
                f"state file {state_path} exists but could not be read ({exc}). Delete it to "
                f"start change tracking again, or fix the path in run.state_file."
            ) from exc
        version = document.get("version")
        if version != STATE_VERSION:
            raise ConfigError(
                f"state file {state_path} is version {version!r}; this build writes "
                f"version {STATE_VERSION}. Delete it to start again."
            )
        return cls(previous=dict(document.get("records", {})), enabled=True)

    def classify(self, key: str, content_hash: str) -> ChangeStatus:
        if not self.enabled:
            return ChangeStatus.UNKNOWN
        seen = self.previous.get(key)
        if seen is None:
            return ChangeStatus.NEW
        return ChangeStatus.UNCHANGED if seen == content_hash else ChangeStatus.CHANGED

    def save(self, path: str | Path, records: dict[str, str], *, now: datetime) -> None:
        """Write the new state.

        Only called when the run produced records. Overwriting a good state file
        with an empty one after a run in which every page failed would silently
        turn the *next* run's report into "everything is new".
        """
        state_path = Path(path)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": STATE_VERSION,
            "updated_at": now.isoformat(),
            "records": records,
        }
        state_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
