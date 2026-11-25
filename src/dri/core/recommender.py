"""
Resource recommender: p95-based CPU/memory limit recommendations,
memory leak detection (restart-aware), and statistical anomaly detection helpers.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Union


def _to_utc_naive(ts: datetime) -> datetime:
    """Normalize a naive or tz-aware datetime to naive UTC."""
    if ts.tzinfo is not None:
        return ts.astimezone(timezone.utc).replace(tzinfo=None)
    return ts

try:
    import numpy as np
except ImportError:
    np = None


def _mean(vals: list[float]) -> float:
    if np is not None:
        return float(np.mean(vals))
    import statistics
    return statistics.mean(vals) if vals else 0.0


def _percentile(vals: list[float], p: float) -> float:
    if np is not None:
        return float(np.percentile(vals, p))
    if not vals:
        return 0.0
    sorted_vals = sorted(vals)
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f = int(k)
    c = f + 1
    if c >= len(sorted_vals):
        return sorted_vals[-1]
    d0 = sorted_vals[f] * (c - k)
    d1 = sorted_vals[c] * (k - f)
    return d0 + d1

from ..utils.logger import get_logger
from .storage_base import StorageBackend

logger = get_logger("recommender")

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class InsufficientDataResult:
    """
    Returned instead of None when there isn't enough history to compute a
    recommendation.  Clients can inspect ``status``, ``min_samples_required``
    and ``samples_available`` to show a meaningful message.
    """

    container_id: str
    container_name: str
    status: str = "insufficient_data"
    min_samples_required: int = 0
    samples_available: int = 0

    def to_dict(self) -> dict:
        return {
            "container_id": self.container_id,
            "container_name": self.container_name,
            "status": self.status,
            "min_samples_required": self.min_samples_required,
            "samples_available": self.samples_available,
        }


@dataclass
class TrendEvidence:
    """
    Evidence bundle for the long-term memory trend assessment.

    ``trend_label`` values:
        - ``"Suspected memory leak"``    — all confidence thresholds met
        - ``"Memory rising · early observation"``  — slope positive but not enough history
        - ``"No sustained leak trend"``  — noisy, repeatedly falling, or step-then-flat
        - ``"Memory stable"``            — slope below threshold without repeated drops
        - ``"Trend evidence unavailable"`` — legacy cache without trend fields
        - ``"Insufficient data"``        — fewer samples/minutes than required
    """

    trend_label: str = "Trend evidence unavailable"
    # Slope in %/hr (matches LEAK_SLOPE_THRESHOLD and memory_leak_slope_pct_per_hour)
    slope_pct_per_hour: float = 0.0
    # Slope in %/min (matches ContainerDetail / LogEvidence growth unit)
    slope_pct_per_min: float = 0.0
    # Distinct timestamps (deduplicated) in the analysis window
    distinct_samples: int = 0
    # Actual elapsed minutes between first and last distinct timestamp
    observation_minutes: float = 0.0
    # Number of populated 10-minute sub-windows that show growth
    populated_growing_windows: int = 0
    # Total populated 10-minute sub-windows evaluated
    populated_windows_total: int = 0
    # Longest streak of consecutive 10-minute windows with sustained growth
    consecutive_growing_windows: int = 0
    # Human-readable explanation of why the label was chosen
    evidence_note: str = ""

    def to_dict(self) -> dict:
        return {
            "trend_label": self.trend_label,
            "slope_pct_per_hour": round(self.slope_pct_per_hour, 4),
            "slope_pct_per_min": round(self.slope_pct_per_min, 6),
            "distinct_samples": self.distinct_samples,
            "observation_minutes": round(self.observation_minutes, 2),
            "populated_growing_windows": self.populated_growing_windows,
            "populated_windows_total": self.populated_windows_total,
            "consecutive_growing_windows": self.consecutive_growing_windows,
            "evidence_note": self.evidence_note,
        }


@dataclass
class Recommendation:
    """Resource limit recommendation for a single container."""

    container_id: str
    container_name: str

    # Recommended limits (add safety margin on top of p95)
    recommended_cpu_pct: float       # percent (0-100+)
    recommended_memory_bytes: int    # bytes

    # Raw p95 values (no safety margin)
    p95_cpu: float
    p95_memory_bytes: int
    p95_memory_pct: float

    # Context
    sample_count: int
    lookback_days: int
    lookback_actual_days: float      # actual span of available data
    safety_margin: float             # e.g. 0.20 = +20%

    # Data quality
    data_completeness: str           # e.g. "3.2 of 7.0 days available" (kept for legacy clients)
    confidence_tier: str             # "low" | "medium" | "high"

    # --- Legacy spike/leak fields (kept for existing clients) ---
    is_memory_leak: bool = False
    memory_leak_slope_pct_per_hour: float = 0.0

    # --- Phase 7E: actual observation span & trend confidence evidence ---
    observation_minutes: Optional[float] = None
    trend_evidence: Optional[TrendEvidence] = None

    # Idle / scale-to-zero signal (merged from ScaleAction)
    is_idle: bool = False
    idle_recommendation: Optional[str] = None

    def to_dict(self) -> dict:
        d = {
            "container_id": self.container_id,
            "container_name": self.container_name,
            "recommended_cpu_pct": round(self.recommended_cpu_pct, 2),
            "recommended_memory_bytes": self.recommended_memory_bytes,
            "p95_cpu": round(self.p95_cpu, 2),
            "p95_memory_bytes": self.p95_memory_bytes,
            "p95_memory_pct": round(self.p95_memory_pct, 2),
            "sample_count": self.sample_count,
            "lookback_days": self.lookback_days,
            "lookback_actual_days": round(self.lookback_actual_days, 2),
            "safety_margin": self.safety_margin,
            "data_completeness": self.data_completeness,
            "confidence_tier": self.confidence_tier,
            "is_memory_leak": self.is_memory_leak,
            "memory_leak_slope_pct_per_hour": round(self.memory_leak_slope_pct_per_hour, 4),
            "is_idle": self.is_idle,
            "idle_recommendation": self.idle_recommendation,
        }
        if self.observation_minutes is not None:
            d["observation_minutes"] = round(self.observation_minutes, 2)
        if self.trend_evidence is not None:
            d["trend_evidence"] = self.trend_evidence.to_dict()
        return d


# ---------------------------------------------------------------------------
# Trend confidence thresholds (configurable module-level constants)
# ---------------------------------------------------------------------------

# Minimum elapsed minutes of distinct timestamps required to call a "leak"
TREND_MIN_ELAPSED_MINUTES: float = 30.0

# Minimum number of distinct (deduplicated) sample timestamps required
TREND_MIN_DISTINCT_SAMPLES: int = 20

# Number of consecutive populated 10-minute sub-windows that must all show growth
TREND_REQUIRED_GROWING_WINDOWS: int = 3

# Width of each sub-window in minutes
TREND_WINDOW_WIDTH_MINUTES: int = 10


# ---------------------------------------------------------------------------
# Recommender
# ---------------------------------------------------------------------------


class ResourceRecommender:
    """
    Compute per-container resource limit recommendations from historical data.

    Uses a configurable lookback window and safety margin on top of p95 usage.
    Also detects sustained memory growth (leak signature) via linear regression
    on rolling 60-minute averages, with restart-boundary awareness.

    Phase 7E adds a richer ``trend_evidence`` field that distinguishes a
    short-lived spike from a long-term sustained leak.  The legacy
    ``is_memory_leak`` / ``memory_leak_slope_pct_per_hour`` fields remain
    unchanged so existing clients continue to work.
    """

    # Minimum samples required to make any recommendation
    MIN_SAMPLES = 10

    # Slope threshold (% memory per hour) above which we consider growth significant
    LEAK_SLOPE_THRESHOLD = 0.5

    # Sharp drop in memory_percent (single step) that signals a container restart
    RESTART_DROP_THRESHOLD = 15.0

    # Sample-count thresholds for confidence tiers
    CONFIDENCE_LOW_THRESHOLD = 50
    CONFIDENCE_HIGH_THRESHOLD = 300

    # Idle detection thresholds (60-minute average)
    IDLE_CPU_THRESHOLD = 1.0        # percent
    IDLE_MEMORY_THRESHOLD = 1.0     # percent

    def __init__(
        self,
        storage: StorageBackend,
        *,
        leak_slope_threshold: float = LEAK_SLOPE_THRESHOLD,
        min_elapsed_minutes: float = TREND_MIN_ELAPSED_MINUTES,
        min_distinct_samples: int = TREND_MIN_DISTINCT_SAMPLES,
        required_growing_windows: int = TREND_REQUIRED_GROWING_WINDOWS,
        window_width_minutes: int = TREND_WINDOW_WIDTH_MINUTES,
    ) -> None:
        self.storage = storage
        self.leak_slope_threshold = leak_slope_threshold
        self.min_elapsed_minutes = min_elapsed_minutes
        self.min_distinct_samples = min_distinct_samples
        self.required_growing_windows = required_growing_windows
        self.window_width_minutes = window_width_minutes

    async def compute_recommendation(
        self,
        container_id: str,
        container_name: str = "",
        lookback_days: int = 7,
        safety_margin: float = 0.20,
    ) -> Union[Recommendation, InsufficientDataResult]:
        """
        Compute recommended CPU and memory limits for a container.

        Algorithm:
          1. Load samples from the last ``lookback_days`` days.
          2. Compute p95 of cpu_percent and memory_usage.
          3. Recommended limit = p95 * (1 + safety_margin).
          4. Compute data_completeness from actual span of loaded samples.
          5. Run restart-aware memory leak detection on the same samples.
          6. Run Phase 7E trend-confidence assessment.
          7. Emit confidence_tier based on sample_count.
          8. Detect idle containers (scale-to-zero signal).

        Args:
            container_id: Docker container short-ID.
            container_name: Human-readable name (used in the result).
            lookback_days: How many days of history to use.
            safety_margin: Fractional overhead added to p95 (default +20 %).

        Returns:
            Recommendation dataclass on success, or InsufficientDataResult
            when there are fewer than MIN_SAMPLES data points.
        """
        minutes = lookback_days * 24 * 60

        samples = await self.storage.get_recent_metrics(container_id, minutes=minutes)

        if len(samples) < self.MIN_SAMPLES:
            logger.debug(
                f"Insufficient samples for {container_id}: "
                f"{len(samples)} < {self.MIN_SAMPLES}"
            )
            return InsufficientDataResult(
                container_id=container_id,
                container_name=container_name,
                min_samples_required=self.MIN_SAMPLES,
                samples_available=len(samples),
            )

        cpu_vals = [s.cpu_percent for s in samples if s.cpu_percent is not None]
        mem_usage_vals = [
            s.memory_usage for s in samples if s.memory_usage is not None
        ]
        mem_pct_vals = [
            s.memory_percent for s in samples if s.memory_percent is not None
        ]

        if len(cpu_vals) < self.MIN_SAMPLES or len(mem_usage_vals) < self.MIN_SAMPLES:
            return InsufficientDataResult(
                container_id=container_id,
                container_name=container_name,
                min_samples_required=self.MIN_SAMPLES,
                samples_available=min(len(cpu_vals), len(mem_usage_vals)),
            )

        p95_cpu = _percentile(cpu_vals, 95)
        p95_memory_bytes = int(_percentile(mem_usage_vals, 95))
        p95_memory_pct = _percentile(mem_pct_vals, 95)

        recommended_cpu_pct = p95_cpu * (1 + safety_margin)
        recommended_memory_bytes = int(p95_memory_bytes * (1 + safety_margin))

        # ----------------------------------------------------------------
        # Data completeness: actual span vs. requested lookback
        # Keep existing data_completeness format for legacy clients while
        # exposing exact observation_minutes for modern UI formatting.
        # ----------------------------------------------------------------
        timestamps = [_to_utc_naive(s.timestamp) for s in samples]
        actual_span_seconds = (max(timestamps) - min(timestamps)).total_seconds()
        actual_days = actual_span_seconds / 86400.0
        actual_minutes = actual_span_seconds / 60.0
        data_completeness = (
            f"{round(actual_days, 1)} of {float(lookback_days)} days available"
        )

        # ----------------------------------------------------------------
        # Confidence tier
        # ----------------------------------------------------------------
        n = len(samples)
        if n < self.CONFIDENCE_LOW_THRESHOLD:
            confidence_tier = "low"
        elif n < self.CONFIDENCE_HIGH_THRESHOLD:
            confidence_tier = "medium"
        else:
            confidence_tier = "high"

        # ----------------------------------------------------------------
        # Phase 7E: trend confidence assessment (authoritative long-term trend)
        # ----------------------------------------------------------------
        trend_ev = self._assess_trend_confidence(samples, lookback_days)
        final_is_leak = trend_ev.trend_label == "Suspected memory leak"
        slope = trend_ev.slope_pct_per_hour

        # ----------------------------------------------------------------
        # Idle / scale-to-zero detection (merged from ScaleAction)
        # ----------------------------------------------------------------
        is_idle, idle_recommendation = self._detect_idle(samples)

        return Recommendation(
            container_id=container_id,
            container_name=container_name,
            recommended_cpu_pct=recommended_cpu_pct,
            recommended_memory_bytes=recommended_memory_bytes,
            p95_cpu=p95_cpu,
            p95_memory_bytes=p95_memory_bytes,
            p95_memory_pct=p95_memory_pct,
            sample_count=n,
            lookback_days=lookback_days,
            lookback_actual_days=actual_days,
            safety_margin=safety_margin,
            data_completeness=data_completeness,
            confidence_tier=confidence_tier,
            is_memory_leak=final_is_leak,
            memory_leak_slope_pct_per_hour=slope,
            observation_minutes=actual_minutes,
            trend_evidence=trend_ev,
            is_idle=is_idle,
            idle_recommendation=idle_recommendation,
        )

    @staticmethod
    def _regress_slope_per_hour(x_vals: list[float], y_vals: list[float]) -> float:
        """Compute OLS slope (% per hour) for memory_percent vs hours."""
        if len(x_vals) < 2:
            return 0.0
        try:
            if np is not None:
                coeffs = np.polyfit(np.array(x_vals), np.array(y_vals), 1)
                return float(coeffs[0])
            xn = len(x_vals)
            mx = sum(x_vals) / xn
            my = sum(y_vals) / xn
            denom = sum((xi - mx) ** 2 for xi in x_vals)
            if denom < 1e-6:
                return 0.0
            return sum((xi - mx) * (yi - my) for xi, yi in zip(x_vals, y_vals)) / denom
        except Exception:
            return 0.0

    # ------------------------------------------------------------------
    # Phase 7E: trend confidence assessment
    # ------------------------------------------------------------------

    def _assess_trend_confidence(
        self,
        samples: list,
        lookback_days: int = 7,
        window_hours: float = 6.0,
    ) -> TrendEvidence:
        """
        Distinguish a spike from a sustained long-term memory trend.

        Rules (thresholds are configurable via RecommenderConfig / __init__):
          1. Filter to last ``window_hours`` relative to the latest sample,
             then discard pre-restart data at any step drop >= RESTART_DROP_THRESHOLD.
          2. Deduplicate by timestamp — duplicate timestamps cannot inflate confidence.
          3. Require >= min_distinct_samples distinct timestamps.
          4. Require >= min_elapsed_minutes of elapsed time (from timestamps).
          5. Require >= required_growing_windows consecutive populated
             window_width_minutes buckets showing sustained growth (missing
             windows break the streak; sawtooth/repeatedly falling series and
             jump-then-plateau cannot qualify).
          6. Reserve "Suspected memory leak" exclusively for histories meeting
             all criteria with slope > leak_slope_threshold.
        """
        mem_samples = [s for s in samples if getattr(s, "memory_percent", None) is not None]
        if len(mem_samples) < self.MIN_SAMPLES:
            return TrendEvidence(
                trend_label="Insufficient data",
                evidence_note=f"Need {self.MIN_SAMPLES} samples; have {len(mem_samples)}.",
            )

        ordered = sorted(mem_samples, key=lambda s: _to_utc_naive(s.timestamp))
        latest_ts = _to_utc_naive(ordered[-1].timestamp)
        cutoff = latest_ts - timedelta(hours=window_hours)
        recent = [s for s in ordered if _to_utc_naive(s.timestamp) >= cutoff]

        if len(recent) < self.MIN_SAMPLES:
            return TrendEvidence(
                trend_label="Insufficient data",
                evidence_note=f"Need {self.MIN_SAMPLES} samples in {window_hours:g}h window; have {len(recent)}.",
            )

        last_restart_idx = -1
        for i in range(1, len(recent)):
            drop = recent[i - 1].memory_percent - recent[i].memory_percent
            if drop >= self.RESTART_DROP_THRESHOLD:
                last_restart_idx = i
        if last_restart_idx >= 0:
            post = recent[last_restart_idx:]
            logger.debug(
                f"Restart boundary detected: discarding {last_restart_idx} pre-restart "
                f"samples; assessing {len(post)} post-restart samples."
            )
            if len(post) < self.MIN_SAMPLES:
                return TrendEvidence(
                    trend_label="Insufficient data",
                    evidence_note=f"Only {len(post)} post-restart samples available.",
                )
            recent = post

        # --- Deduplicate by exact timestamp ---
        unique_by_ts: dict[datetime, object] = {}
        for s in recent:
            unique_by_ts[_to_utc_naive(s.timestamp)] = s
        unique = [unique_by_ts[ts] for ts in sorted(unique_by_ts)]

        distinct_count = len(unique)
        if distinct_count < 2:
            return TrendEvidence(
                trend_label="Insufficient data",
                distinct_samples=distinct_count,
                evidence_note="Duplicate timestamps only — cannot measure elapsed time.",
            )

        t_first = _to_utc_naive(unique[0].timestamp)
        t_last = _to_utc_naive(unique[-1].timestamp)
        total_span_seconds = (t_last - t_first).total_seconds()
        elapsed_min = total_span_seconds / 60.0

        # --- Overall OLS slope (%/hr and %/min) ---
        x_vals = [(_to_utc_naive(s.timestamp) - t_first).total_seconds() / 3600.0 for s in unique]
        y_vals = [float(s.memory_percent) for s in unique]
        slope_pct_per_hour = self._regress_slope_per_hour(x_vals, y_vals)
        slope_pct_per_min = slope_pct_per_hour / 60.0

        # --- Step-drop / sawtooth analysis ---
        step_drops = [y_vals[i - 1] - y_vals[i] for i in range(1, len(y_vals))]
        sig_drop_count = sum(1 for d in step_drops if d >= 1.0)
        total_drop = sum(d for d in step_drops if d > 0.0)
        net_gain = y_vals[-1] - y_vals[0]
        is_repeatedly_falling = (
            sig_drop_count >= 2 and total_drop > 0.5 * max(net_gain, 1.0)
        )

        # --- Fixed sub-window analysis (10-minute buckets) ---
        bucket_seconds = self.window_width_minutes * 60.0
        num_windows = max(1, int(math.ceil(total_span_seconds / bucket_seconds)))
        complete_windows = int(total_span_seconds // bucket_seconds)
        buckets: list[list[tuple[datetime, float]]] = [[] for _ in range(num_windows)]
        for s in unique:
            ts = _to_utc_naive(s.timestamp)
            offset = (ts - t_first).total_seconds()
            idx = min(num_windows - 1, int(offset // bucket_seconds))
            buckets[idx].append((ts, float(s.memory_percent)))

        populated_growing = 0
        populated_total = 0
        window_grows: list[bool] = []
        prev_populated_mean: Optional[float] = None

        for b_idx, bucket in enumerate(buckets):
            if not bucket:
                # Missing window breaks the consecutive streak and resets previous window
                window_grows.append(False)
                prev_populated_mean = None
                continue

            populated_total += 1
            b_vals = [v for _, v in bucket]
            b_mean = sum(b_vals) / len(b_vals)

            # A window counts as growing only with >= 2 distinct timestamps,
            # net positive rise, positive intra-window slope, non-decreasing mean
            # over the immediately preceding window, and without sawtooth reversal.
            b_grows = False
            if len(bucket) >= 2:
                b_net = b_vals[-1] - b_vals[0]
                b_x = [(ts - bucket[0][0]).total_seconds() / 3600.0 for ts, _ in bucket]
                b_slope = self._regress_slope_per_hour(b_x, b_vals)
                b_drops = sum(max(0.0, b_vals[i - 1] - b_vals[i]) for i in range(1, len(b_vals)))
                mean_progressed = prev_populated_mean is None or b_mean > prev_populated_mean
                if b_net > 0 and b_slope > 0 and b_drops <= 0.5 * b_net and mean_progressed:
                    b_grows = True

            if b_grows:
                populated_growing += 1
            window_grows.append(b_grows)

            prev_populated_mean = b_mean

        # Ignore a non-growing partial tail, but include it when it continues
        # the streak. The qualifying streak must otherwise end at the latest
        # completed window rather than at an earlier historical peak.
        recent_consecutive = 0
        if len(window_grows) > complete_windows and window_grows[-1]:
            recent_consecutive = 1
        for grew in reversed(window_grows[:complete_windows]):
            if not grew:
                break
            recent_consecutive += 1

        ev = TrendEvidence(
            slope_pct_per_hour=slope_pct_per_hour,
            slope_pct_per_min=slope_pct_per_min,
            distinct_samples=distinct_count,
            observation_minutes=elapsed_min,
            populated_growing_windows=populated_growing,
            populated_windows_total=populated_total,
            consecutive_growing_windows=recent_consecutive,
        )

        # --- Classification & evidence explanation ---
        slope_above = slope_pct_per_hour > self.leak_slope_threshold
        short_elapsed = elapsed_min < self.min_elapsed_minutes
        short_samples = distinct_count < self.min_distinct_samples

        if is_repeatedly_falling:
            ev.trend_label = "No sustained leak trend"
            ev.evidence_note = (
                f"Sustained growth could not be established: memory dropped repeatedly "
                f"({sig_drop_count} drops totaling {total_drop:.1f}%)."
            )
            return ev

        if not slope_above:
            ev.trend_label = "Memory stable"
            ev.evidence_note = (
                f"Slope {slope_pct_per_hour:+.2f}%/hr is at or below the "
                f"{self.leak_slope_threshold:.2f}%/hr leak threshold."
            )
            return ev

        if short_elapsed or short_samples:
            ev.trend_label = "Memory rising · early observation"
            if short_elapsed:
                ev.evidence_note = (
                    f"{distinct_count} samples over {round(elapsed_min)} min "
                    f"· {round(self.min_elapsed_minutes)} min required to assess a leak"
                )
            else:
                ev.evidence_note = (
                    f"{distinct_count} samples over {round(elapsed_min)} min "
                    f"· {self.min_distinct_samples} distinct samples required to assess a leak"
                )
            return ev

        if recent_consecutive < self.required_growing_windows:
            ev.trend_label = "No sustained leak trend"
            if populated_total < num_windows:
                ev.evidence_note = (
                    f"Sustained growth could not be established: only {recent_consecutive} consecutive "
                    f"{self.window_width_minutes}-min windows with data grew "
                    f"(need {self.required_growing_windows}; missing windows broke continuity)."
                )
            else:
                ev.evidence_note = (
                    f"Sustained growth could not be established: only {recent_consecutive} consecutive "
                    f"{self.window_width_minutes}-min windows grew "
                    f"(need {self.required_growing_windows}; single step or plateau does not qualify)."
                )
            return ev

        ev.trend_label = "Suspected memory leak"
        ev.evidence_note = (
            f"{distinct_count} samples over {round(elapsed_min)} min "
            f"· {recent_consecutive} consecutive {self.window_width_minutes}-min windows growing."
        )
        return ev

    def _detect_idle(
        self,
        samples: list,
        window_minutes: int = 60,
    ) -> tuple[bool, Optional[str]]:
        """
        Detect containers with sustained near-zero CPU and memory usage.

        Uses the last ``window_minutes`` of the supplied samples, matching
        the ScaleAction.identify_idle_containers logic (60 m average).

        Returns:
            Tuple of (is_idle: bool, recommendation_string or None).
        """
        if not samples:
            return False, None

        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=window_minutes)
        recent = [s for s in samples if _to_utc_naive(s.timestamp) >= cutoff]

        if len(recent) < 2:
            return False, None

        cpu_vals = [s.cpu_percent for s in recent if s.cpu_percent is not None]
        mem_vals = [s.memory_percent for s in recent if s.memory_percent is not None]

        if not cpu_vals or not mem_vals:
            return False, None

        avg_cpu = _mean(cpu_vals)
        avg_mem = _mean(mem_vals)

        if avg_cpu < self.IDLE_CPU_THRESHOLD and avg_mem < self.IDLE_MEMORY_THRESHOLD:
            return True, "scale_to_zero"

        return False, None

    def _detect_memory_leak(
        self,
        samples: list,
        window_hours: float = 6.0,
    ) -> tuple[bool, float]:
        """
        Detect a sustained upward trend in memory usage, skipping pre-restart data.

        Returns (is_leak, slope_pct_per_hour) using the Phase 7E trend
        confidence criteria so legacy callers and Recommendation agree.
        """
        ev = self._assess_trend_confidence(samples, window_hours=window_hours)
        return ev.trend_label == "Suspected memory leak", ev.slope_pct_per_hour

    async def detect_memory_leak(
        self,
        container_id: str,
        window_hours: float = 6.0,
    ) -> tuple[bool, float]:
        """
        Public async wrapper for memory leak detection.
        """
        minutes = int(window_hours * 60) + 5
        samples = await self.storage.get_recent_metrics(container_id, minutes=minutes)
        return self._detect_memory_leak(samples, window_hours=window_hours)


# ---------------------------------------------------------------------------
# Statistical anomaly helpers (used by aggregator)
# ---------------------------------------------------------------------------


def z_score_anomaly(
    values: List[float],
    current: float,
    threshold: float = 3.0,
) -> tuple[bool, float]:
    """
    Compute z-score of ``current`` against the distribution in ``values``.
    """
    if len(values) < 5:
        return False, 0.0

    if np is not None:
        arr = np.array(values, dtype=float)
        mean = float(np.mean(arr))
        std = float(np.std(arr))
    else:
        import statistics
        mean = statistics.mean(values)
        std = statistics.stdev(values) if len(values) > 1 else 0.0

    if std < 1e-6:
        return False, 0.0

    z = (current - mean) / std
    return abs(z) > threshold, round(z, 3)
