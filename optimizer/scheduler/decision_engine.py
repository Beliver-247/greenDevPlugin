"""Rule-based scheduling decision engine.

Decides whether a build should execute immediately or be delayed to a
lower-carbon-intensity time window based on extracted features.
"""

from __future__ import annotations

from typing import Any

# A forecast window must be at least this many gCO₂/kWh lower than the
# current intensity to justify delaying the build.
SIGNIFICANT_REDUCTION_THRESHOLD: float = 50.0

# Builds execute immediately when the grid intensity is below this value
# regardless of the forecast.
LOW_INTENSITY_CEILING: float = 200.0


class SchedulingDecisionEngine:
    """Determine whether to execute a build now or schedule it later."""

    def decide(self, features: dict[str, Any]) -> dict[str, Any]:
        """Return a scheduling recommendation.

        Parameters
        ----------
        features:
            Feature dictionary produced by
            :func:`~optimizer.scheduler.features.extract_features`.

        Returns
        -------
        dict
            ``{"action": "execute_now"}`` when the build should run
            immediately, or ``{"action": "schedule", "scheduled_hour": int,
            "target_intensity": float}`` when delaying is beneficial.
        """

        current = features.get("current_carbon_intensity", 0.0)

        if current < LOW_INTENSITY_CEILING:
            return {"action": "execute_now"}

        forecast_min = features.get("forecast_min_intensity", current)
        reduction = current - forecast_min

        if reduction >= SIGNIFICANT_REDUCTION_THRESHOLD:
            # Walk the forecast to find the hour with the lowest intensity.
            scheduled_hour = features.get("_best_forecast_hour", 0)
            return {
                "action": "schedule",
                "scheduled_hour": scheduled_hour,
                "target_intensity": forecast_min,
            }

        return {"action": "execute_now"}
