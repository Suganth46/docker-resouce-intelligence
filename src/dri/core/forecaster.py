"""
Metrics forecaster: project CPU/memory usage N hours ahead per container.

Uses Holt's linear trend when statsmodels is available,
falls back to NumPy linear regression otherwise.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Literal, Union

import numpy as np

from ..utils.logger import get_logger
from .storage_base import StorageBackend

logger = get_logger("forecaster")

# Minimum samples required to produce a forecast
MIN_SAMPLES = 15

# Try to import statsmodels for trend smoothing
try:
    from statsmodels.tsa.holtwinters import Holt  # type: ignore
    _HAS_STATSMODELS = True
except ImportError:
    _HAS_STATSMODELS = False
    logger.info("statsmodels not installed — using linear regression fallback for forecasting")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class InsufficientForecastData:
    """
    Returned instead of None when there aren't enough samples to produce a
    forecast.  Carries structured context so callers can return informative
    API responses.
    """

    container_id: str
    metric: str
    status: str = "insufficient_data"
    min_samples_required: int = MIN_SAMPLES
    samples_available: int = 0

    def to_dict(self) -> dict:
        return {
            "container_id": self.container_id,
            "metric": self.metric,
            "status": self.status,
            "min_samples_required": self.min_samples_required,
            "samples_available": self.samples_available,
        }


@dataclass
class ForecastResult:
    """Single-metric forecast for a container."""

    container_id: str
    metric: str                   # 'cpu_percent' or 'memory_percent'
    horizon_hours: int
    current_value: float
    predicted_value: float
    confidence_low: float
    confidence_high: float
    predicted_at: datetime
    method_used: str              # 'holt_linear' or 'linear_regression'
    sample_count: int

    def to_dict(self) -> dict:
        return {
            "container_id": self.container_id,
            "metric": self.metric,
            "horizon_hours": self.horizon_hours,
            "current_value": round(self.current_value, 2),
            "predicted_value": round(self.predicted_value, 2),
            "confidence_low": round(self.confidence_low, 2),
            "confidence_high": round(self.confidence_high, 2),
            "predicted_at": self.predicted_at.isoformat(),
            "method_used": self.method_used,
            "sample_count": self.sample_count,
        }


# ---------------------------------------------------------------------------
# Forecaster
# ---------------------------------------------------------------------------


class MetricsForecaster:
    """
    Forecast container resource usage N hours into the future.

    Primary method: Holt's linear trend (requires statsmodels).
    Fallback: linear regression via NumPy polyfit.

    Both methods compute confidence intervals as ±1.5 × residual RMSE.
    """

    def __init__(self, storage: StorageBackend) -> None:
        self.storage = storage

    async def forecast(
        self,
        container_id: str,
        horizon_hours: int = 24,
        metric: str = "memory_percent",
        lookback_days: int = 7,
    ) -> Union[ForecastResult, InsufficientForecastData]:
        """
        Forecast metric value ``horizon_hours`` from now.

        Args:
            container_id: Docker container short-ID.
            horizon_hours: How many hours ahead to predict.
            metric: 'cpu_percent' or 'memory_percent'.
            lookback_days: Days of historical data to use.

        Returns:
            ForecastResult on success, InsufficientForecastData when there
            are fewer than MIN_SAMPLES data points.
        """
        minutes = lookback_days * 24 * 60
        samples = await self.storage.get_recent_metrics(container_id, minutes=minutes)

        if len(samples) < MIN_SAMPLES:
            logger.debug(
                f"Insufficient samples for forecast of {container_id}: "
                f"{len(samples)} < {MIN_SAMPLES}"
            )
            return InsufficientForecastData(
                container_id=container_id,
                metric=metric,
                min_samples_required=MIN_SAMPLES,
                samples_available=len(samples),
            )

        samples = [s for s in samples if getattr(s, metric, None) is not None]
        values = np.array(
            [getattr(s, metric) for s in samples],
            dtype=float,
        )

        if len(values) < MIN_SAMPLES:
            return InsufficientForecastData(
                container_id=container_id,
                metric=metric,
                min_samples_required=MIN_SAMPLES,
                samples_available=len(values),
            )

        current_value = float(values[-1])
        n = len(values)

        predicted_at = datetime.now(timezone.utc) + timedelta(hours=horizon_hours)
        last = samples[-1].timestamp
        span = (last - samples[0].timestamp).total_seconds()
        interval = span / (n - 1) if span > 0 else lookback_days * 86400 / n
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        steps_ahead = max(1, round((predicted_at - last).total_seconds() / interval))

        if _HAS_STATSMODELS:
            predicted, conf_low, conf_high, method = self._holt_forecast(
                values, steps_ahead
            )
        else:
            predicted, conf_low, conf_high, method = self._linear_forecast(
                values, steps_ahead
            )

        # Clamp predictions to physically meaningful range
        upper = 100.0 if metric == "memory_percent" else np.inf
        predicted, conf_low, conf_high = np.clip([predicted, conf_low, conf_high], 0.0, upper)

        return ForecastResult(
            container_id=container_id,
            metric=metric,
            horizon_hours=horizon_hours,
            current_value=current_value,
            predicted_value=predicted,
            confidence_low=conf_low,
            confidence_high=conf_high,
            predicted_at=predicted_at,
            method_used=method,
            sample_count=n,
        )

    def _holt_forecast(
        self,
        values: np.ndarray,
        steps_ahead: int,
    ) -> tuple[float, float, float, str]:
        """
        Holt's linear trend forecast.

        Returns (predicted, conf_low, conf_high, method_name).
        """
        try:
            model = Holt(values, initialization_method="estimated")
            fit = model.fit(optimized=True, use_brute=True)
            # Evaluate the additive trend directly at the target step.
            predicted = float(fit.level[-1] + steps_ahead * fit.trend[-1])

            # Confidence interval from in-sample residuals RMSE
            residuals = values - fit.fittedvalues
            rmse = float(np.sqrt(np.mean(residuals ** 2)))
            half_width = 1.5 * rmse

            return predicted, predicted - half_width, predicted + half_width, "holt_linear"
        except Exception as e:
            logger.warning(f"Holt fit failed ({e}), falling back to linear regression")
            return self._linear_forecast(values, steps_ahead)

    def _linear_forecast(
        self,
        values: np.ndarray,
        steps_ahead: int,
    ) -> tuple[float, float, float, str]:
        """
        Linear regression (OLS) forecast via numpy.polyfit.

        Returns (predicted, conf_low, conf_high, method_name).
        """
        n = len(values)
        x = np.arange(n, dtype=float)
        try:
            coeffs = np.polyfit(x, values, 1)
        except Exception:
            # Last resort: return current value with wide CI
            last = float(values[-1])
            std = float(np.std(values))
            return last, last - 2 * std, last + 2 * std, "linear_regression"

        # Predict at x = n + steps_ahead - 1
        x_pred = float(n + steps_ahead - 1)
        predicted = float(np.polyval(coeffs, x_pred))

        # Residuals for CI
        y_fit = np.polyval(coeffs, x)
        residuals = values - y_fit
        rmse = float(np.sqrt(np.mean(residuals ** 2)))
        half_width = 1.5 * rmse

        return predicted, predicted - half_width, predicted + half_width, "linear_regression"
