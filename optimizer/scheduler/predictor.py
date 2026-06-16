"""ML-based green window prediction using the trained LightGBM classifier.

This module replaces the earlier stub that raised ``NotImplementedError``.
It loads the serialised LightGBM model produced by Phase 2 of the
``modelComparison`` research pipeline and uses it to classify whether the
current hour is a "green window" (carbon intensity ≤ 25th-percentile
threshold).

Decision contract
-----------------
``GreenWindowPredictor.predict()`` returns a dict of the same shape as
``SchedulingDecisionEngine.decide()`` so that both engines are drop-in
replacements for each other inside ``run_scheduler()``:

::

    {
        "action":            "execute_now" | "schedule",
        "green_probability": float,          # model confidence [0, 1]
        "engine":            "lgbm",
        # only present when action == "schedule":
        "scheduled_hour":    int,
        "target_intensity":  float,
    }

Logged fallback
---------------
If the model file is missing, a dependency is unavailable, or the history
buffer is too short, ``predict()`` raises ``PredictorUnavailableError``
with a human-readable message.  The caller (``run_scheduler``) catches this,
prints the message to stdout, and falls back to the rule-based engine.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

# Default location of the bundled model artefacts (relative to this file).
_MODEL_DIR: Path = Path(__file__).parent / "model"
_DEFAULT_MODEL_PATH: Path = _MODEL_DIR / "best_model.joblib"
_DEFAULT_PARAMS_PATH: Path = _MODEL_DIR / "best_model_params.json"

# Minimum number of history readings required before the ML path is used.
# Below this the feature vector is too sparse to be meaningful.
MIN_HISTORY_FOR_ML: int = 3


class PredictorUnavailableError(RuntimeError):
    """Raised when the ML predictor cannot produce a prediction.

    The scheduler catches this and falls back to the rule-based engine,
    printing the message so it appears in CI pipeline logs.
    """


class GreenWindowPredictor:
    """LightGBM-based green window classifier.

    Parameters
    ----------
    model_path:
        Path to the serialised model (``.joblib``).
    params_path:
        Path to the model metadata JSON (feature list, threshold, etc.).
    """

    def __init__(self, model_path: Path, params_path: Path) -> None:
        self._model = self._load_model(model_path)
        self._params = self._load_params(params_path)
        self._feature_cols: list[str] = self._params["feature_columns"]
        self._threshold: float = float(self._params.get("recommended_threshold", 0.50))

    # ------------------------------------------------------------------
    # Convenience constructors
    # ------------------------------------------------------------------

    @classmethod
    def from_default_location(cls) -> "GreenWindowPredictor":
        """Load the bundled model shipped inside the plugin package.

        Raises
        ------
        PredictorUnavailableError
            If the model files are not present at the expected paths.
        """

        return cls(model_path=_DEFAULT_MODEL_PATH, params_path=_DEFAULT_PARAMS_PATH)

    @classmethod
    def from_config_path(cls, model_path: str | Path) -> "GreenWindowPredictor":
        """Load a model from a user-specified path.

        Parameters
        ----------
        model_path:
            Path to the ``.joblib`` model file.  A sibling
            ``best_model_params.json`` must exist in the same directory.
        """

        p = Path(model_path).expanduser().resolve()
        params = p.parent / "best_model_params.json"
        return cls(model_path=p, params_path=params)

    # ------------------------------------------------------------------
    # Prediction
    # ------------------------------------------------------------------

    def predict(
        self,
        ci_history: list[float],
        now: datetime | None = None,
        forecast: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Predict whether the current hour is a green build window.

        Parameters
        ----------
        ci_history:
            Ordered list of hourly carbon intensity readings (oldest first).
            The last element is the current hour's reading.
        now:
            Current datetime.  Defaults to ``datetime.now()``.
        forecast:
            Optional short-term CI forecast from the carbon API, used to
            select the best scheduled hour when the model recommends delaying.
            Each entry: ``{"hour": int, "intensity": float}``.

        Returns
        -------
        dict
            Scheduling recommendation compatible with the rule-based engine
            output schema.

        Raises
        ------
        PredictorUnavailableError
            If there is insufficient history or the model cannot run.
        """

        if len(ci_history) < MIN_HISTORY_FOR_ML:
            raise PredictorUnavailableError(
                f"insufficient history: {len(ci_history)}/{MIN_HISTORY_FOR_ML} hours. "
                "Falling back to rule-based scheduling engine."
            )

        now = now or datetime.now()

        # Build and order the feature vector exactly as the model expects.
        try:
            from .features import build_ml_feature_vector
            raw_features = build_ml_feature_vector(ci_history, now)
            feature_row = [raw_features[col] for col in self._feature_cols]
        except KeyError as exc:
            raise PredictorUnavailableError(
                f"feature engineering failed (missing key: {exc}). "
                "Falling back to rule-based scheduling engine."
            ) from exc

        # Run inference.
        # Prefer pandas DataFrame so LightGBM receives named features (avoids
        # sklearn UserWarning). Falls back to a plain numpy array if pandas is
        # not installed in the runtime environment.
        try:
            try:
                import pandas as pd
                X = pd.DataFrame([feature_row], columns=self._feature_cols)
            except ImportError:
                import warnings
                import numpy as np
                X = np.array([feature_row], dtype=float)
                warnings.filterwarnings(
                    "ignore",
                    message="X does not have valid feature names",
                    category=UserWarning,
                )
            proba = float(self._model.predict_proba(X)[0, 1])
        except Exception as exc:  # noqa: BLE001
            raise PredictorUnavailableError(
                f"model inference failed: {exc}. "
                "Falling back to rule-based scheduling engine."
            ) from exc

        is_green = proba >= self._threshold
        action = "execute_now" if is_green else "schedule"

        result: dict[str, Any] = {
            "action": action,
            "green_probability": round(proba, 4),
            "engine": "lgbm",
        }

        # When scheduling, pick the best upcoming hour from the forecast.
        if action == "schedule" and forecast:
            best = min(forecast, key=lambda e: e["intensity"])
            result["scheduled_hour"] = best["hour"]
            result["target_intensity"] = best["intensity"]

        return result

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _load_model(path: Path) -> Any:
        """Load a joblib-serialised model from *path*."""

        if not path.is_file():
            raise PredictorUnavailableError(
                f"model file not found at {path}. "
                "Falling back to rule-based scheduling engine."
            )
        try:
            import joblib
            return joblib.load(path)
        except ImportError:
            raise PredictorUnavailableError(
                "joblib is not installed. "
                "Run 'pip install joblib lightgbm' to enable ML scheduling. "
                "Falling back to rule-based scheduling engine."
            )
        except Exception as exc:  # noqa: BLE001
            raise PredictorUnavailableError(
                f"could not load model from {path}: {exc}. "
                "Falling back to rule-based scheduling engine."
            ) from exc

    @staticmethod
    def _load_params(path: Path) -> dict[str, Any]:
        """Load model metadata from a JSON *path*."""

        if not path.is_file():
            raise PredictorUnavailableError(
                f"model params file not found at {path}. "
                "Falling back to rule-based scheduling engine."
            )
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise PredictorUnavailableError(
                f"could not read model params from {path}: {exc}. "
                "Falling back to rule-based scheduling engine."
            ) from exc
