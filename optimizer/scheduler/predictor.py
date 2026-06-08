"""ML-based green window prediction (stub).

This module will eventually host an XGBoost model that predicts the
optimal build window based on historical carbon data, build telemetry,
and time-series features.  For now it raises ``NotImplementedError`` so
that calling code can detect the absence of a trained model and fall
back to the rule-based decision engine.

Future integration points
-------------------------
* Train an XGBoost classifier on historical carbon intensity data
  paired with build duration / outcome labels.
* Load a serialised model (e.g. ``model.json``) at construction time.
* The ``predict`` method should return a dict matching the
  ``SchedulingDecisionEngine.decide`` output contract.
"""

from __future__ import annotations

from typing import Any


class GreenWindowPredictor:
    """Placeholder for an ML-based scheduling predictor.

    Replace the body of :meth:`predict` with model inference once a
    trained XGBoost (or equivalent) model is available.
    """

    def predict(self, features: dict[str, Any]) -> dict[str, Any]:
        """Predict the optimal green build window.

        Parameters
        ----------
        features:
            Feature dictionary produced by
            :func:`~optimizer.scheduler.features.extract_features`.

        Raises
        ------
        NotImplementedError
            Always — the ML model is not yet implemented.
        """

        raise NotImplementedError(
            "ML prediction is not yet implemented. "
            "This will be replaced with an XGBoost model."
        )
