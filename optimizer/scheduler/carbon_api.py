"""Carbon intensity data providers.

This module defines the ``CarbonDataProvider`` abstraction and ships a
``MockCarbonDataProvider`` for development and testing.  Real providers
(ElectricityMap, WattTime, etc.) can be plugged in by subclassing
``CarbonDataProvider`` and updating :func:`get_provider`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class CarbonDataProvider(ABC):
    """Abstract base class for carbon intensity data sources."""

    @abstractmethod
    def get_current_intensity(self) -> float:
        """Return the current grid carbon intensity in gCO₂/kWh."""

    @abstractmethod
    def get_forecast(self) -> list[dict[str, Any]]:
        """Return a short-term carbon intensity forecast.

        Each entry is a dict with at least ``hour`` (int, hours from now)
        and ``intensity`` (float, gCO₂/kWh).
        """


class MockCarbonDataProvider(CarbonDataProvider):
    """Hard-coded carbon data for development and offline testing."""

    def get_current_intensity(self) -> float:
        """Return a mock current carbon intensity."""

        return 320.0

    def get_forecast(self) -> list[dict[str, Any]]:
        """Return a mock three-hour forecast."""

        return [
            {"hour": 1, "intensity": 300.0},
            {"hour": 2, "intensity": 250.0},
            {"hour": 3, "intensity": 180.0},
        ]


def get_provider() -> CarbonDataProvider:
    """Return the active carbon data provider.

    Currently returns the mock provider.  Replace this factory when
    integrating a real API such as ElectricityMap or WattTime.
    """

    return MockCarbonDataProvider()
