"""Feature extraction for carbon-aware scheduling decisions.

Two public interfaces are provided:

1. ``build_ml_feature_vector`` — builds the **41-feature vector** expected by
   the trained LightGBM green-window classifier.  Requires a rolling history
   buffer of recent carbon intensity readings (up to 168 hours).

2. ``extract_features`` — the **original 8-feature extractor** kept for
   backward-compatibility with the rule-based ``SchedulingDecisionEngine``.
   Used as a fallback when the ML predictor is unavailable.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any


# ---------------------------------------------------------------------------
# ML Feature Builder (41 features — must match best_model_params.json order)
# ---------------------------------------------------------------------------

def build_ml_feature_vector(
    ci_history: list[float],
    now: datetime | None = None,
) -> dict[str, float]:
    """Build the 41-feature vector expected by the LightGBM model.

    Parameters
    ----------
    ci_history:
        Ordered list of hourly carbon intensity readings (gCO₂eq/kWh),
        **oldest first**.  The last element is the most recent reading
        (i.e. current hour).  Should contain at least 1 value; ideally
        168+ for full lag coverage.
    now:
        The datetime of the **most recent** reading in ``ci_history``.
        Defaults to ``datetime.now()``.

    Returns
    -------
    dict
        Feature dictionary with exactly the keys listed in
        ``best_model_params.json → feature_columns``.  Missing lag values
        (insufficient history) are filled with the earliest available
        reading to avoid NaN propagation.
    """

    now = now or datetime.now()

    # Convenience: index from the end (history[-1] = current hour)
    def lag(n: int) -> float:
        """Return the CI value n hours ago, or the oldest available reading."""
        if n < len(ci_history):
            return float(ci_history[-(n + 1)])
        return float(ci_history[0]) if ci_history else 0.0

    def rolling_window(n: int) -> list[float]:
        """Return the last n readings (newest at the end)."""
        return [float(v) for v in ci_history[-n:]] if len(ci_history) >= 1 else [0.0]

    def safe_mean(values: list[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    def safe_std(values: list[float]) -> float:
        if len(values) < 2:
            return 0.0
        m = safe_mean(values)
        variance = sum((v - m) ** 2 for v in values) / len(values)
        return math.sqrt(variance)

    # ── Temporal features ────────────────────────────────────────────────
    hour = now.hour
    dow = now.weekday()          # 0 = Monday, 6 = Sunday
    is_weekend = int(dow >= 5)

    # ── Lag values ───────────────────────────────────────────────────────
    ci_lag_1h   = lag(1)
    ci_lag_2h   = lag(2)
    ci_lag_3h   = lag(3)
    ci_lag_6h   = lag(6)
    ci_lag_12h  = lag(12)
    ci_lag_24h  = lag(24)
    ci_lag_48h  = lag(48)
    ci_lag_168h = lag(168)

    # ── Rolling window statistics ─────────────────────────────────────────
    w3   = rolling_window(3)
    w6   = rolling_window(6)
    w12  = rolling_window(12)
    w24  = rolling_window(24)
    w48  = rolling_window(48)
    w168 = rolling_window(168)

    # ── Rate of change ────────────────────────────────────────────────────
    current = lag(0)
    ci_diff_1h       = current - ci_lag_1h
    ci_diff_24h      = current - ci_lag_24h
    pct_denom = ci_lag_1h if ci_lag_1h != 0.0 else 1.0
    ci_pct_change_1h = (ci_diff_1h / pct_denom) * 100.0

    return {
        # Temporal
        "hour":              float(hour),
        "day_of_week":       float(dow),
        "day_of_month":      float(now.day),
        "month":             float(now.month),
        "quarter":           float((now.month - 1) // 3 + 1),
        "week_of_year":      float(now.isocalendar().week),
        "is_weekend":        float(is_weekend),
        # Cyclical encodings
        "hour_sin":          math.sin(2 * math.pi * hour / 24),
        "hour_cos":          math.cos(2 * math.pi * hour / 24),
        "dow_sin":           math.sin(2 * math.pi * dow / 7),
        "dow_cos":           math.cos(2 * math.pi * dow / 7),
        "month_sin":         math.sin(2 * math.pi * now.month / 12),
        "month_cos":         math.cos(2 * math.pi * now.month / 12),
        # Lag features
        "ci_lag_1h":         ci_lag_1h,
        "ci_lag_2h":         ci_lag_2h,
        "ci_lag_3h":         ci_lag_3h,
        "ci_lag_6h":         ci_lag_6h,
        "ci_lag_12h":        ci_lag_12h,
        "ci_lag_24h":        ci_lag_24h,
        "ci_lag_48h":        ci_lag_48h,
        "ci_lag_168h":       ci_lag_168h,
        # Rolling mean
        "ci_rolling_mean_3h":   safe_mean(w3),
        "ci_rolling_std_3h":    safe_std(w3),
        "ci_rolling_mean_6h":   safe_mean(w6),
        "ci_rolling_std_6h":    safe_std(w6),
        "ci_rolling_mean_12h":  safe_mean(w12),
        "ci_rolling_std_12h":   safe_std(w12),
        "ci_rolling_mean_24h":  safe_mean(w24),
        "ci_rolling_std_24h":   safe_std(w24),
        "ci_rolling_mean_48h":  safe_mean(w48),
        "ci_rolling_std_48h":   safe_std(w48),
        "ci_rolling_mean_168h": safe_mean(w168),
        "ci_rolling_std_168h":  safe_std(w168),
        # Rolling min / max
        "ci_rolling_min_24h":   min(w24),
        "ci_rolling_max_24h":   max(w24),
        "ci_rolling_min_168h":  min(w168),
        "ci_rolling_max_168h":  max(w168),
        # Rate of change
        "ci_diff_1h":        ci_diff_1h,
        "ci_diff_24h":       ci_diff_24h,
        "ci_pct_change_1h":  ci_pct_change_1h,
        # Interaction
        "hour_x_weekend":    float(hour * is_weekend),
    }


# ---------------------------------------------------------------------------
# Rule-based Feature Extractor (backward-compatible — DO NOT REMOVE)
# ---------------------------------------------------------------------------

def extract_features(
    optimizer_output: dict[str, Any],
    carbon_data: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a feature vector from optimizer results and carbon data.

    This is the **original 8-feature extractor** used by the rule-based
    ``SchedulingDecisionEngine``.  It is retained so that the fallback path
    continues to work unchanged when the ML predictor is unavailable.

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
        Flat feature dictionary for the rule-based decision engine.
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
