"""Scheduler orchestrator for carbon-aware build scheduling.

This is the single entry point for the scheduling subsystem.  It wires
together the carbon data provider, feature extraction, and decision
engine into a simple pipeline that returns a scheduling recommendation.
"""

from __future__ import annotations

from typing import Any

from .carbon_api import get_provider
from .decision_engine import SchedulingDecisionEngine
from .features import extract_features


def run_scheduler(optimizer_output: dict[str, Any]) -> dict[str, Any]:
    """Run the full carbon-aware scheduling pipeline.

    Parameters
    ----------
    optimizer_output:
        The payload dictionary produced by the optimizer CLI.  Must
        contain at least ``affected_modules``,
        ``directly_affected_modules``, and ``changed_files``.

    Returns
    -------
    dict
        A scheduling recommendation — either
        ``{"action": "execute_now"}`` or
        ``{"action": "schedule", "scheduled_hour": int,
        "target_intensity": float}``.
    """

    provider = get_provider()

    carbon_data: dict[str, Any] = {
        "current_intensity": provider.get_current_intensity(),
        "forecast": provider.get_forecast(),
    }

    features = extract_features(optimizer_output, carbon_data)

    # Attach the best forecast hour so the decision engine can report
    # which hour was selected without re-scanning the forecast.
    forecast = carbon_data.get("forecast", [])
    if forecast:
        best = min(forecast, key=lambda entry: entry["intensity"])
        features["_best_forecast_hour"] = best["hour"]

    engine = SchedulingDecisionEngine()
    return engine.decide(features)
