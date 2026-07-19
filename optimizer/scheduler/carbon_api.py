"""Carbon intensity data providers.

This module defines the ``CarbonDataProvider`` abstraction and ships two
concrete implementations:

``MockCarbonDataProvider``
    Hard-coded values for development and offline testing.  Used by default
    when no API key is configured.

``ElectricityMapsProvider``
    Fetches live and historical carbon intensity data from the
    `Electricity Maps API v4 <https://api.electricitymaps.com/>`_.
    Requires an ``auth-token`` (set via ``ELECTRICITY_MAPS_API_KEY`` env var
    or the ``carbon_aware.electricity_maps_api_key`` config key).

Switching providers
-------------------
Call :func:`get_provider` with an optional config dict.  The factory
automatically selects the real provider when an API key is available::

    provider = get_provider({"provider": "electricity_maps", "zone": "LK"})
    current = provider.get_current_intensity()
"""

from __future__ import annotations

import math
import os
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Any


class CarbonDataProvider(ABC):
    """Abstract base class for carbon intensity data sources."""

    @abstractmethod
    def get_current_intensity(self) -> float:
        """Return the current grid carbon intensity in gCO₂eq/kWh (direct)."""

    @abstractmethod
    def get_forecast(self) -> list[dict[str, Any]]:
        """Return a short-term carbon intensity forecast.

        Each entry is a dict with at least ``hour`` (int, hours from now)
        and ``intensity`` (float, gCO₂/kWh).
        """

    def get_history(self, hours: int = 168) -> list[tuple[datetime, float]]:
        """Return the last *hours* hourly carbon intensity readings.

        Returns a list of ``(datetime, float)`` tuples, **oldest first**.
        The default implementation returns an empty list; override in
        providers that support historical queries.

        Parameters
        ----------
        hours:
            Number of past hourly readings to retrieve.
        """

        return []


# ---------------------------------------------------------------------------
# Mock provider (default — no API key required)
# ---------------------------------------------------------------------------

class MockCarbonDataProvider(CarbonDataProvider):
    """Synthetic carbon data for development and offline testing.

    Generates a realistic sinusoidal day/night pattern:
    - Peak intensity (~420 gCO₂/kWh) during midday (12:00)
    - Trough intensity (~160 gCO₂/kWh) at night (00:00)
    This simulates a grid with higher fossil-fuel usage during peak demand
    and more renewables overnight, giving the ML model and charts
    meaningful variation to work with.
    """

    _BASE: float = 290.0      # midpoint gCO₂/kWh
    _AMPLITUDE: float = 130.0  # peak deviation

    def _intensity_at(self, dt: datetime) -> float:
        """Return synthetic intensity for a given datetime."""
        # Hour-of-day sinusoid: peak at 12:00, trough at 00:00
        hour_rad = (dt.hour + dt.minute / 60.0) / 24.0 * 2 * math.pi
        # Day-of-week variation: slightly greener on weekends
        dow_factor = 0.9 if dt.weekday() >= 5 else 1.0
        # Small random-looking variation based on day-of-year
        noise = 20.0 * math.sin(dt.timetuple().tm_yday / 7.0 * math.pi)
        raw = self._BASE + self._AMPLITUDE * math.sin(hour_rad - math.pi / 2)
        return max(80.0, raw * dow_factor + noise)

    def get_current_intensity(self) -> float:
        """Return a synthetic current carbon intensity."""
        return round(self._intensity_at(datetime.now()), 1)

    def get_forecast(self) -> list[dict[str, Any]]:
        """Return a 6-hour forecast showing the upcoming sinusoidal trend."""
        now = datetime.now()
        return [
            {
                "hour": h,
                "intensity": round(self._intensity_at(now + timedelta(hours=h)), 1),
            }
            for h in range(1, 7)
        ]

    def get_history(self, hours: int = 168) -> list[tuple[datetime, float]]:
        """Return a synthetic sinusoidal history for the last *hours* hours."""
        now = datetime.now().replace(minute=0, second=0, microsecond=0)
        return [
            (now - timedelta(hours=h), round(self._intensity_at(now - timedelta(hours=h)), 1))
            for h in range(hours, 0, -1)   # oldest first
        ]


# ---------------------------------------------------------------------------
# Electricity Maps provider (real API)
# ---------------------------------------------------------------------------

class ElectricityMapsProvider(CarbonDataProvider):
    """Fetches live carbon intensity data from the Electricity Maps API v4.

    Parameters
    ----------
    api_key:
        Your Electricity Maps ``auth-token``.
    zone:
        Grid zone identifier (e.g. ``"LK"`` for Sri Lanka).
    request_delay_s:
        Seconds to sleep between successive per-timestamp history requests
        to avoid rate-limiting.  Default: 0.2 s.
    """

    BASE_URL = "https://api.electricitymaps.com/v4"

    def __init__(
        self,
        api_key: str,
        zone: str = "LK",
        request_delay_s: float = 0.2,
    ) -> None:
        self._api_key = api_key
        self._zone = zone
        self._delay = request_delay_s

    # ------------------------------------------------------------------
    # CarbonDataProvider interface
    # ------------------------------------------------------------------

    def get_current_intensity(self) -> float:
        """Fetch the latest carbon intensity from ``/v4/carbon-intensity/latest``.

        Returns
        -------
        float
            Current carbon intensity in gCO₂eq/kWh (direct emissions factor).

        Raises
        ------
        RuntimeError
            If the API call fails or the response cannot be parsed.
        """

        url = f"{self.BASE_URL}/carbon-intensity/latest"
        data = self._get(url, params={"zone": self._zone})
        return float(data["carbonIntensity"])

    def get_forecast(self) -> list[dict[str, Any]]:
        """Fetch a short-term forecast from ``/v4/carbon-intensity/forecast``.

        Returns
        -------
        list[dict]
            Each entry: ``{"hour": int, "intensity": float}``.
        """

        url = f"{self.BASE_URL}/carbon-intensity/forecast"
        data = self._get(url, params={"zone": self._zone})

        forecast_entries = data.get("forecast", [])
        now = datetime.now(timezone.utc)
        result: list[dict[str, Any]] = []

        for i, entry in enumerate(forecast_entries):
            dt_str = entry.get("datetime") or entry.get("datetime_utc")
            intensity = float(entry.get("carbonIntensity", 0.0))
            if dt_str:
                try:
                    dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                    hours_from_now = max(0, round((dt - now).total_seconds() / 3600))
                except ValueError:
                    hours_from_now = i + 1
            else:
                hours_from_now = i + 1
            result.append({"hour": hours_from_now, "intensity": intensity})

        return result

    def get_history(self, hours: int = 168) -> list[tuple[datetime, float]]:
        """Fetch the last *hours* hourly readings via ``/v4/carbon-intensity/past``.

        The Electricity Maps ``/past`` endpoint is per-timestamp, so this
        method iterates hourly timestamps and issues one request per hour.
        A short delay (``request_delay_s``) is applied between requests to
        avoid rate-limiting.

        Parameters
        ----------
        hours:
            Number of past hourly readings to retrieve (max 168).

        Returns
        -------
        list[tuple[datetime, float]]
            ``(timestamp, intensity)`` tuples, **oldest first**.
        """

        url = f"{self.BASE_URL}/carbon-intensity/past"
        results: list[tuple[datetime, float]] = []
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

        for h in range(hours, 0, -1):   # oldest → newest
            target = now - timedelta(hours=h)
            try:
                data = self._get(
                    url,
                    params={
                        "zone": self._zone,
                        "datetime": target.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    },
                )
                intensity = float(data["carbonIntensity"])
                results.append((target, intensity))
            except Exception as exc:  # noqa: BLE001
                # Skip individual failed hours rather than aborting the whole backfill
                print(
                    f"[GreenOptimizer]   ⚠  Could not fetch history for "
                    f"{target.isoformat()}: {exc}"
                )
            if self._delay > 0:
                time.sleep(self._delay)

        return results

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get(self, url: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        """Perform a GET request and return the parsed JSON response.

        Parameters
        ----------
        url:
            Absolute URL to fetch.
        params:
            Query-string parameters.

        Raises
        ------
        RuntimeError
            On HTTP errors or network failures.
        """

        try:
            import urllib.request
            import urllib.parse

            if params:
                url = f"{url}?{urllib.parse.urlencode(params)}"

            req = urllib.request.Request(
                url,
                headers={"auth-token": self._api_key},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                import json
                body = resp.read().decode("utf-8")
                return json.loads(body)

        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"Electricity Maps API request to {url} failed: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# Provider factory
# ---------------------------------------------------------------------------

def get_provider(config: dict[str, Any] | None = None) -> CarbonDataProvider:
    """Return the active carbon data provider.

    Provider selection logic (in priority order):

    1. ``ELECTRICITY_MAPS_API_KEY`` environment variable is set  →  real provider
    2. ``config["electricity_maps_api_key"]`` is non-null          →  real provider
    3. ``config["provider"] == "electricity_maps"`` but no key     →  warning + mock
    4. Anything else                                               →  mock

    Parameters
    ----------
    config:
        Optional dict from the ``carbon_aware:`` YAML section.
    """

    cfg = config or {}
    api_key = (
        os.environ.get("ELECTRICITY_MAPS_API_KEY")
        or cfg.get("electricity_maps_api_key")
    )
    zone = cfg.get("electricity_maps_zone", "LK")
    provider_name = cfg.get("provider", "mock")

    if api_key:
        return ElectricityMapsProvider(api_key=str(api_key), zone=str(zone))

    if provider_name == "electricity_maps":
        print(
            "[GreenOptimizer] ⚠  'electricity_maps' provider requested but no API key found.\n"
            "                    Set ELECTRICITY_MAPS_API_KEY or carbon_aware.electricity_maps_api_key.\n"
            "                    Falling back to MockCarbonDataProvider."
        )

    return MockCarbonDataProvider()
