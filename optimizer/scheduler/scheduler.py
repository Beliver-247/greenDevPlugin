"""Scheduler orchestrator for carbon-aware build scheduling.

This is the single entry point for the scheduling subsystem.  It wires
together the carbon data provider, history store, ML predictor, and
rule-based fallback engine into a pipeline that returns a scheduling
recommendation.

Pipeline
--------
1. Load the local carbon intensity history store.
2. Fetch the current carbon intensity from the configured provider.
   If using the real Electricity Maps provider and the store is empty
   or stale, backfill the last 168 hours automatically.
3. Append the current reading to the history store and persist it.
4. Attempt ML prediction via ``GreenWindowPredictor``.
   - On success → return the ML recommendation (includes ``green_probability``).
   - On ``PredictorUnavailableError`` → print a ⚠ warning and fall back
     to the rule-based ``SchedulingDecisionEngine``.
5. Return the recommendation dict to the caller (``cli.py``).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from .carbon_api import CarbonDataProvider, get_provider
from .decision_engine import SchedulingDecisionEngine
from .features import extract_features
from .history_store import CarbonHistoryStore
from .predictor import GreenWindowPredictor, PredictorUnavailableError

# Default path for the local history cache file.
_DEFAULT_HISTORY_PATH: str = "~/.greendevops/carbon_history.json"

# Minimum hours of history needed before the ML model is considered reliable.
_DEFAULT_MIN_HISTORY_HOURS: int = 3

# How old (hours) the newest history entry can be before triggering a re-fetch.
_STALE_THRESHOLD_HOURS: int = 2


def run_scheduler(
    optimizer_output: dict[str, Any],
    carbon_aware_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the full carbon-aware scheduling pipeline.

    Parameters
    ----------
    optimizer_output:
        The payload dictionary produced by the optimizer CLI.  Must contain
        at least ``affected_modules``, ``directly_affected_modules``, and
        ``changed_files``.
    carbon_aware_config:
        Optional dict from the ``carbon_aware:`` YAML section.  Controls
        provider selection, history path, model path, and fallback thresholds.

    Returns
    -------
    dict
        A scheduling recommendation with at least:
        - ``action``: ``"execute_now"`` or ``"schedule"``
        - ``engine``: ``"lgbm"`` or ``"rule_based"``
        - ``green_probability``: float (only when engine is ``"lgbm"``)
        - ``scheduled_hour`` / ``target_intensity`` (only when action is ``"schedule"``)
    """

    cfg = carbon_aware_config or {}
    history_path = cfg.get("history_store_path", _DEFAULT_HISTORY_PATH)
    min_history = int(cfg.get("min_history_hours", _DEFAULT_MIN_HISTORY_HOURS))
    model_path_override = cfg.get("model_path") or None
    backfill_on_empty = bool(cfg.get("backfill_on_empty", True))

    now = datetime.now()

    # ── Step 1: Load history store ────────────────────────────────────────
    store = CarbonHistoryStore.load(history_path)

    # ── Step 2: Get carbon data provider ─────────────────────────────────
    provider: CarbonDataProvider = get_provider(cfg)

    # ── Step 2a: Backfill history if the store is empty or stale ─────────
    if backfill_on_empty and (len(store) == 0 or store.is_stale(_STALE_THRESHOLD_HOURS)):
        _backfill_history(store, provider)

    # ── Step 3: Fetch current intensity and append to store ───────────────
    try:
        current_intensity = provider.get_current_intensity()
    except Exception as exc:  # noqa: BLE001
        print(
            f"[GreenOptimizer] ⚠  Could not fetch current carbon intensity: {exc}. "
            "Using last known value from history store."
        )
        current_intensity = float(store.get_history(1)[-1]) if store.get_history(1) else 300.0

    store.append(intensity=current_intensity, timestamp=now)
    store.save(history_path)

    ci_history = store.get_history(168)

    # ── Step 4: Also fetch forecast (used by both ML and rule-based paths) ─
    try:
        forecast = provider.get_forecast()
    except Exception as exc:  # noqa: BLE001
        print(f"[GreenOptimizer] ⚠  Could not fetch forecast: {exc}. Using empty forecast.")
        forecast = []

    # ── Step 5: Attempt ML prediction ────────────────────────────────────
    try:
        if model_path_override:
            predictor = GreenWindowPredictor.from_config_path(model_path_override)
        else:
            predictor = GreenWindowPredictor.from_default_location()

        result = predictor.predict(ci_history=ci_history, now=now, forecast=forecast)
        result["current_intensity"] = current_intensity
        result["carbon_history"] = _serialize_history(ci_history, now)
        result["carbon_forecast"] = forecast
        return result

    except PredictorUnavailableError as exc:
        # Logged fallback — the ⚠ message appears in CI pipeline logs
        print(f"[GreenOptimizer] ⚠  ML predictor unavailable: {exc}")
        print("[GreenOptimizer]    Using rule-based scheduling engine instead.")

    # ── Step 6: Rule-based fallback ───────────────────────────────────────
    carbon_data: dict[str, Any] = {
        "current_intensity": current_intensity,
        "forecast": forecast,
    }
    features = extract_features(optimizer_output, carbon_data, now=now)

    # Attach best forecast hour so the decision engine can include it.
    if forecast:
        best = min(forecast, key=lambda e: e["intensity"])
        features["_best_forecast_hour"] = best["hour"]

    engine = SchedulingDecisionEngine()
    result = engine.decide(features)
    result["engine"] = "rule_based"
    result["current_intensity"] = current_intensity
    result["carbon_history"] = _serialize_history(ci_history, now)
    result["carbon_forecast"] = forecast
    return result


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _serialize_history(
    ci_history: list[float],
    now: datetime,
) -> list[dict]:
    """Convert a flat list of CI floats into timestamped dicts for the dashboard.

    Parameters
    ----------
    ci_history:
        Ordered float list from the history store, **oldest first**.
        The last element corresponds to *now*.
    now:
        The datetime of the most recent reading.

    Returns
    -------
    list[dict]
        Each dict has ``timestamp`` (ISO-8601 string) and ``intensity`` (float).
    """
    n = len(ci_history)
    return [
        {
            "timestamp": (now - timedelta(hours=(n - 1 - i))).isoformat(),
            "intensity": float(ci_history[i]),
        }
        for i in range(n)
    ]


def _backfill_history(store: CarbonHistoryStore, provider: CarbonDataProvider) -> None:
    """Attempt to backfill the store with the last 168 hours from the provider.

    Only providers that override ``get_history()`` (i.e. ``ElectricityMapsProvider``)
    will actually return data.  ``MockCarbonDataProvider`` returns synthetic readings
    which are still useful for development and testing.

    Parameters
    ----------
    store:
        The history store to populate.
    provider:
        The active carbon data provider.
    """

    try:
        n_missing = max(0, 168 - len(store))
        if n_missing == 0:
            return

        print(
            f"[GreenOptimizer] 📡  Backfilling carbon intensity history "
            f"({n_missing} hours) — this may take a moment..."
        )
        readings = provider.get_history(hours=n_missing)
        if readings:
            store.bulk_append(readings)
            print(
                f"[GreenOptimizer] ✅  Backfill complete: {len(readings)} readings added "
                f"(store now has {len(store)} entries)."
            )
        else:
            print("[GreenOptimizer] ℹ  Backfill returned no data.")
    except Exception as exc:  # noqa: BLE001
        print(
            f"[GreenOptimizer] ⚠  History backfill failed: {exc}. "
            "Proceeding with partial history."
        )
