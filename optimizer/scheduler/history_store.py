"""Rolling carbon intensity history buffer.

Maintains a local JSON file that accumulates hourly carbon intensity
readings.  The store is used to build the lag and rolling-window features
required by the LightGBM green-window classifier.

Typical usage
-------------
::

    store = CarbonHistoryStore.load(path)
    store.append(intensity=312.5, timestamp=datetime.now())
    store.save(path)

    # Retrieve the last 168 readings (oldest-first) for feature engineering
    history = store.get_history(n_hours=168)
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

# Maximum number of hourly entries to retain in the store.
# 168 h = 1 week (longest lag feature used by the model).
MAX_HISTORY_HOURS: int = 168


class _Entry(NamedTuple):
    """A single timestamped carbon intensity reading."""

    timestamp: datetime   # UTC-aware or naive; stored as ISO-8601 string
    intensity: float      # gCO₂eq/kWh (direct)


class CarbonHistoryStore:
    """A persistent, self-trimming rolling buffer of carbon intensity values.

    Parameters
    ----------
    entries:
        Pre-loaded entries, ordered oldest-first.  Pass an empty list to
        create a fresh store.
    """

    def __init__(self, entries: list[_Entry] | None = None) -> None:
        self._entries: list[_Entry] = list(entries or [])

    # ------------------------------------------------------------------
    # Factory / persistence
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> "CarbonHistoryStore":
        """Load the store from *path*.

        If the file does not exist or cannot be parsed, an empty store is
        returned and a warning is printed to stdout.

        Parameters
        ----------
        path:
            Filesystem path to the JSON history file.
        """

        p = Path(path).expanduser()
        if not p.is_file():
            return cls()

        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            entries = [
                _Entry(
                    timestamp=datetime.fromisoformat(item["timestamp"]),
                    intensity=float(item["intensity"]),
                )
                for item in raw.get("entries", [])
            ]
            store = cls(entries)
            store._trim()
            return store
        except Exception as exc:  # noqa: BLE001
            print(
                f"[GreenOptimizer] ⚠  Could not read history store at {p}: {exc}. "
                "Starting with an empty history."
            )
            return cls()

    def save(self, path: str | Path) -> None:
        """Persist the store to *path* (creates parent directories as needed).

        Parameters
        ----------
        path:
            Filesystem path for the JSON history file.
        """

        p = Path(path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "entries": [
                {
                    "timestamp": entry.timestamp.isoformat(),
                    "intensity": entry.intensity,
                }
                for entry in self._entries
            ]
        }
        p.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def append(self, intensity: float, timestamp: datetime | None = None) -> None:
        """Add a new reading to the store and trim to ``MAX_HISTORY_HOURS``.

        Parameters
        ----------
        intensity:
            Carbon intensity value in gCO₂eq/kWh.
        timestamp:
            UTC timestamp of the reading.  Defaults to ``datetime.now()``.
        """

        ts = timestamp or datetime.now()
        self._entries.append(_Entry(timestamp=ts, intensity=intensity))
        self._trim()

    def bulk_append(self, readings: list[tuple[datetime, float]]) -> None:
        """Add multiple (timestamp, intensity) pairs at once, oldest-first.

        Used during the initial 168-hour backfill from the Electricity Maps
        API.  Duplicate timestamps are deduplicated; the store is trimmed
        after insertion.

        Parameters
        ----------
        readings:
            List of ``(datetime, float)`` tuples, oldest-first.
        """

        existing_ts = {e.timestamp.isoformat() for e in self._entries}
        for ts, intensity in readings:
            key = ts.isoformat()
            if key not in existing_ts:
                self._entries.append(_Entry(timestamp=ts, intensity=intensity))
                existing_ts.add(key)

        # Sort oldest-first after bulk insert
        self._entries.sort(key=lambda e: e.timestamp)
        self._trim()

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_history(self, n_hours: int = MAX_HISTORY_HOURS) -> list[float]:
        """Return the last *n_hours* intensity readings, **oldest first**.

        Returns a plain ``list[float]`` so it can be passed directly to
        :func:`~optimizer.scheduler.features.build_ml_feature_vector`.

        Parameters
        ----------
        n_hours:
            Number of recent hours to include.  At most ``MAX_HISTORY_HOURS``.
        """

        n = min(n_hours, MAX_HISTORY_HOURS)
        return [e.intensity for e in self._entries[-n:]]

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def oldest_timestamp(self) -> datetime | None:
        """The timestamp of the oldest entry, or ``None`` if the store is empty."""
        return self._entries[0].timestamp if self._entries else None

    @property
    def newest_timestamp(self) -> datetime | None:
        """The timestamp of the newest entry, or ``None`` if the store is empty."""
        return self._entries[-1].timestamp if self._entries else None

    def is_stale(self, max_age_hours: int = 2) -> bool:
        """Return ``True`` if the newest entry is older than *max_age_hours*.

        Used by the scheduler to decide whether to re-fetch from the API
        even when a history file already exists.

        Parameters
        ----------
        max_age_hours:
            Staleness threshold in hours.
        """

        if not self._entries:
            return True
        age = datetime.now() - self._entries[-1].timestamp.replace(tzinfo=None)
        return age > timedelta(hours=max_age_hours)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _trim(self) -> None:
        """Remove entries older than MAX_HISTORY_HOURS from the tail."""
        if len(self._entries) > MAX_HISTORY_HOURS:
            self._entries = self._entries[-MAX_HISTORY_HOURS:]
