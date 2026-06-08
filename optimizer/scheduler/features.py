"""Feature extraction for carbon-aware scheduling decisions.

Combines optimizer build output with carbon telemetry into a flat
feature dictionary suitable for rule-based or ML-based scheduling.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


def extract_features(
    optimizer_output: dict[str, Any],
    carbon_data: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a feature vector from optimizer results and carbon data.

    Parameters
    ----------
    optimizer_output:
        The payload dict produced by the optimizer CLI (must contain
        ``affected_modules``, ``directly_affected_modules``, and
        ``changed_files``).
    carbon_data:
        Dict with ``current_intensity`` (float) and ``forecast``
        (list of ``{"hour": int, "intensity": float}``).
    now:
        Optional override for the current time (useful in tests).

    Returns
    -------
    dict
        Flat feature dictionary with the keys documented in the
        project specification.
    """

    now = now or datetime.now()
    forecast = carbon_data.get("forecast", [])
    intensities = [entry["intensity"] for entry in forecast] if forecast else [0.0]

    return {
        "affected_modules_count": len(optimizer_output.get("affected_modules", [])),
        "directly_affected_modules_count": len(
            optimizer_output.get("directly_affected_modules", [])
        ),
        "changed_files_count": len(optimizer_output.get("changed_files", [])),
        "hour_of_day": now.hour,
        "day_of_week": now.weekday(),
        "current_carbon_intensity": float(carbon_data.get("current_intensity", 0.0)),
        "forecast_min_intensity": float(min(intensities)),
        "forecast_avg_intensity": float(sum(intensities) / len(intensities)),
        "forecast_max_intensity": float(max(intensities)),
    }
