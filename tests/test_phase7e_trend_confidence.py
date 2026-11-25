"""
Phase 7E — Memory trend confidence and evidence freshness tests.

All tests use deterministic, timestamped synthetic samples so that they
are independent of wall-clock time and produce reproducible results.

Coverage:
  T1  17 rising samples over 4 minutes  → "Memory rising · early observation"
  T2  One jump followed by a plateau    → not "Suspected memory leak"
  T3  Sustained growth across 3 windows → "Suspected memory leak"
  T4  Sawtooth series                   → not a leak, explains why
  T5  Duplicate timestamps              → cannot inflate confidence
  T6  Missing window breaks streak      → consecutive window count < 3
  T7  Legacy cached payload             → deserializes cleanly, trend_evidence is None
  T8  Integration: live memory-spike badge + early-observation trend together
  T9  Configurable thresholds & observation_minutes
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List

import pytest

from dri.config import DRIConfig
from dri.core.recommender import (
    InsufficientDataResult,
    Recommendation,
    ResourceRecommender,
    TREND_MIN_DISTINCT_SAMPLES,
    TREND_MIN_ELAPSED_MINUTES,
    TREND_REQUIRED_GROWING_WINDOWS,
    TrendEvidence,
)


# ---------------------------------------------------------------------------
# Minimal stub objects
# ---------------------------------------------------------------------------


@dataclass
class FakeSample:
    """Minimal sample object matching the .timestamp / .memory_percent interface."""

    timestamp: datetime
    memory_percent: float
    cpu_percent: float = 5.0
    memory_usage: int = 100_000_000
    memory_limit: int = 1_000_000_000


class FakeStorage:
    """In-memory stub for StorageBackend."""

    def __init__(self, samples: List[FakeSample]):
        self._samples = samples

    async def get_recent_metrics(self, container_id: str, minutes: int = 60):
        return list(self._samples)

    async def get_all_recommendation_caches(self):
        return []


def make_recommender(samples: List[FakeSample], **kwargs) -> ResourceRecommender:
    return ResourceRecommender(storage=FakeStorage(samples), **kwargs)  # type: ignore[arg-type]


BASE = datetime(2026, 1, 1, 12, 0, 0)  # deterministic anchor


def rising_samples(
    n: int,
    span_minutes: float,
    start_pct: float = 10.0,
    end_pct: float = 30.0,
    base: datetime = BASE,
) -> List[FakeSample]:
    """Build *n* samples uniformly spread over *span_minutes* with linearly rising memory."""
    samples = []
    for i in range(n):
        t = base + timedelta(minutes=span_minutes * i / max(n - 1, 1))
        pct = start_pct + (end_pct - start_pct) * i / max(n - 1, 1)
        samples.append(FakeSample(timestamp=t, memory_percent=pct))
    return samples


def assess(samples: List[FakeSample], **kwargs) -> TrendEvidence:
    return make_recommender(samples, **kwargs)._assess_trend_confidence(samples)


# ---------------------------------------------------------------------------
# T1 — 17 rising samples over 4 minutes → early observation
# ---------------------------------------------------------------------------


def test_t1_early_observation_exact_wording():
    """17 samples over 4 min must produce exact early-observation label and note."""
    samples = rising_samples(17, span_minutes=4.0, start_pct=10.0, end_pct=30.0)
    ev = assess(samples)
    assert ev.trend_label == "Memory rising · early observation"
    assert ev.evidence_note == "17 samples over 4 min · 30 min required to assess a leak"
    assert ev.distinct_samples == 17
    assert ev.observation_minutes == pytest.approx(4.0, abs=0.01)
    assert ev.slope_pct_per_hour > 0
    assert ev.slope_pct_per_min == pytest.approx(ev.slope_pct_per_hour / 60.0, rel=1e-4)


async def test_t1_compute_recommendation_exposes_observation_minutes():
    samples = rising_samples(17, span_minutes=4.0, start_pct=10.0, end_pct=30.0)
    rec = make_recommender(samples)
    result = await rec.compute_recommendation("c1", "test")
    assert isinstance(result, Recommendation)
    assert result.is_memory_leak is False
    assert result.observation_minutes == pytest.approx(4.0, abs=0.01)
    assert result.trend_evidence is not None
    assert result.trend_evidence.trend_label == "Memory rising · early observation"


# ---------------------------------------------------------------------------
# T2 — One jump followed by a plateau → NOT a suspected leak
# ---------------------------------------------------------------------------


def _jump_plateau_samples() -> List[FakeSample]:
    """10 samples flat at 10% (0..45m), jump to 60% at 46m, then 20 flat at 60% (50..145m)."""
    samples: List[FakeSample] = []
    for i in range(10):
        samples.append(FakeSample(timestamp=BASE + timedelta(minutes=i * 5), memory_percent=10.0))
    jump_t = BASE + timedelta(minutes=46)
    samples.append(FakeSample(timestamp=jump_t, memory_percent=60.0))
    for i in range(20):
        samples.append(
            FakeSample(timestamp=BASE + timedelta(minutes=50 + i * 5), memory_percent=60.0)
        )
    return samples


def test_t2_jump_plateau_not_a_leak():
    samples = _jump_plateau_samples()
    rec = make_recommender(samples)
    ev = rec._assess_trend_confidence(samples)
    assert ev.trend_label == "No sustained leak trend"
    assert ev.consecutive_growing_windows < TREND_REQUIRED_GROWING_WINDOWS
    assert "sustained growth could not be established" in ev.evidence_note.lower()

    is_leak, slope = rec._detect_memory_leak(samples)
    assert is_leak is False
    assert slope > 0


# ---------------------------------------------------------------------------
# T3 — Sustained growth across ≥3 consecutive 10-minute windows → leak
# ---------------------------------------------------------------------------


def _sustained_growth_samples() -> List[FakeSample]:
    """40 samples over 50 minutes, strictly rising 10%→50%."""
    return rising_samples(40, span_minutes=50.0, start_pct=10.0, end_pct=50.0)


def test_t3_sustained_growth_is_a_leak():
    samples = _sustained_growth_samples()
    rec = make_recommender(samples)
    ev = rec._assess_trend_confidence(samples)
    assert ev.trend_label == "Suspected memory leak"
    assert ev.consecutive_growing_windows >= TREND_REQUIRED_GROWING_WINDOWS
    assert ev.populated_growing_windows >= TREND_REQUIRED_GROWING_WINDOWS
    assert ev.observation_minutes >= TREND_MIN_ELAPSED_MINUTES
    assert ev.distinct_samples >= TREND_MIN_DISTINCT_SAMPLES
    assert ev.slope_pct_per_min == pytest.approx(0.8, abs=0.05)
    assert ev.slope_pct_per_hour == pytest.approx(48.0, abs=1.0)

    is_leak, slope_hr = rec._detect_memory_leak(samples)
    assert is_leak is True
    assert slope_hr == pytest.approx(ev.slope_pct_per_min * 60.0, abs=0.1)


# ---------------------------------------------------------------------------
# T4 — Sawtooth series → not a leak, explains why
# ---------------------------------------------------------------------------


def _sawtooth_flat_samples() -> List[FakeSample]:
    """Repeatedly rising 10%→30% then dropping back to 10% over 60 minutes."""
    samples: List[FakeSample] = []
    for i in range(40):
        # Keep each cycle's drop below the restart threshold.
        pct = 10.0 + (i % 10) * 1.5
        t = BASE + timedelta(minutes=60.0 * i / 39)
        samples.append(FakeSample(timestamp=t, memory_percent=pct))
    return samples


def _sawtooth_drifting_samples() -> List[FakeSample]:
    """Sawtooth with slight upward drift and sub-threshold cycle drops."""
    samples: List[FakeSample] = []
    for i in range(40):
        cycle = i // 8
        step = i % 8
        pct = 10.0 + cycle * 2.0 + step * 1.5
        t = BASE + timedelta(minutes=50.0 * i / 39)
        samples.append(FakeSample(timestamp=t, memory_percent=pct))
    return samples


def test_t4_sawtooth_not_a_leak():
    for builder in (_sawtooth_flat_samples, _sawtooth_drifting_samples):
        ev = assess(builder())
        assert ev.trend_label == "No sustained leak trend"
        assert "sustained growth could not be established" in ev.evidence_note.lower()
        assert "dropped repeatedly" in ev.evidence_note.lower()


def test_historical_growth_streak_does_not_qualify():
    """A recent plateau invalidates an otherwise qualifying historical streak."""
    samples = rising_samples(60, span_minutes=59.0, start_pct=10.0, end_pct=40.0)
    for i, sample in enumerate(samples):
        if i >= 30:
            sample.memory_percent = 40.0

    ev = assess(samples)
    assert ev.trend_label == "No sustained leak trend"
    assert ev.consecutive_growing_windows < TREND_REQUIRED_GROWING_WINDOWS


def test_incomplete_trailing_bucket_can_complete_recent_growth_streak():
    samples = rising_samples(40, span_minutes=35.0, start_pct=10.0, end_pct=50.0)
    ev = assess(samples)

    assert ev.trend_label == "Suspected memory leak"
    assert ev.consecutive_growing_windows >= TREND_REQUIRED_GROWING_WINDOWS


# ---------------------------------------------------------------------------
# T5 — Duplicate timestamps cannot inflate confidence
# ---------------------------------------------------------------------------


def test_t5_duplicate_timestamps_deduplicated():
    """100 samples across only 10 distinct timestamps over 4.5 min → early observation."""
    samples: List[FakeSample] = []
    for i in range(10):
        ts = BASE + timedelta(seconds=i * 30)  # 4.5 min span
        for _ in range(10):
            samples.append(FakeSample(timestamp=ts, memory_percent=10.0 + i * 2.0))
    ev = assess(samples)
    assert ev.distinct_samples == 10
    assert ev.observation_minutes == pytest.approx(4.5, abs=0.01)
    assert ev.trend_label == "Memory rising · early observation"


def test_t5_all_identical_timestamps_insufficient():
    """25 samples with the exact same timestamp cannot establish elapsed time or a leak."""
    samples = [FakeSample(timestamp=BASE, memory_percent=10.0 + i) for i in range(25)]
    ev = assess(samples)
    assert ev.distinct_samples == 1
    assert ev.trend_label == "Insufficient data"


# ---------------------------------------------------------------------------
# T6 — Missing window breaks consecutive streak
# ---------------------------------------------------------------------------


def test_t6_missing_window_breaks_continuity():
    """
    Window 0 [0..10m) grows, Window 1 [10..20m) is missing,
    Window 2 [20..30m) grows, Window 3 [30..40m) grows.
    3 populated windows total, but max consecutive is only 2 (< 3).
    """
    samples: List[FakeSample] = []
    # Window 0: minutes 0..9 (10 samples)
    for i in range(10):
        samples.append(FakeSample(timestamp=BASE + timedelta(minutes=i), memory_percent=10.0 + i))
    # Window 1: minutes 10..19 -> MISSING
    # Window 2: minutes 20..29 (10 samples)
    for i in range(10):
        samples.append(FakeSample(timestamp=BASE + timedelta(minutes=20 + i), memory_percent=20.0 + i))
    # Window 3: minutes 30..39 (10 samples)
    for i in range(10):
        samples.append(FakeSample(timestamp=BASE + timedelta(minutes=30 + i), memory_percent=30.0 + i))

    ev = assess(samples)
    assert ev.populated_growing_windows == 3
    assert ev.consecutive_growing_windows == 2
    assert ev.trend_label == "No sustained leak trend"
    assert "missing windows" in ev.evidence_note.lower()


# ---------------------------------------------------------------------------
# T7 — Legacy cached payload (no trend_evidence or observation_minutes)
# ---------------------------------------------------------------------------


def test_t7_pydantic_accepts_legacy_payload():
    """RecommendationResponse accepts legacy payload lacking Phase 7E fields."""
    from dri.api.models import RecommendationResponse

    payload = {
        "container_id": "abc123",
        "container_name": "legacy-container",
        "recommended_cpu_pct": 25.0,
        "recommended_memory_bytes": 268_435_456,
        "p95_cpu": 20.0,
        "p95_memory_bytes": 220_000_000,
        "p95_memory_pct": 55.0,
        "sample_count": 350,
        "lookback_days": 7,
        "lookback_actual_days": 3.2,
        "safety_margin": 0.20,
        "data_completeness": "0.0 of 7.0 days available",
        "confidence_tier": "high",
        "is_memory_leak": True,
        "memory_leak_slope_pct_per_hour": 1.2,
        "is_idle": False,
        "idle_recommendation": None,
    }
    resp = RecommendationResponse(**payload)
    assert resp.trend_evidence is None
    assert resp.observation_minutes is None
    assert resp.is_memory_leak is True
    assert resp.memory_leak_slope_pct_per_hour == 1.2
    assert resp.data_completeness == "0.0 of 7.0 days available"


async def test_t7_api_serves_legacy_cached_payload():
    """GET /api/v1/recommendations serves an older SQLite cache record without error."""
    from httpx import ASGITransport, AsyncClient
    from dri.api import routes as route_module
    from dri.api.server import DRIAPIServer
    from dri.core.aggregator import MetricsAggregator
    from dri.core.engine import PolicyEngine
    from dri.core.storage import MetricsStorage

    storage = MetricsStorage(":memory:")
    await storage.init()

    legacy_payload = {
        "container_id": "legacy01",
        "container_name": "old-worker",
        "recommended_cpu_pct": 30.0,
        "recommended_memory_bytes": 128 * 1024 * 1024,
        "p95_cpu": 25.0,
        "p95_memory_bytes": 100 * 1024 * 1024,
        "p95_memory_pct": 40.0,
        "sample_count": 120,
        "lookback_days": 7,
        "lookback_actual_days": 0.0,
        "safety_margin": 0.20,
        "data_completeness": "0.0 of 7.0 days available",
        "confidence_tier": "medium",
        "is_memory_leak": False,
        "memory_leak_slope_pct_per_hour": 0.0,
        "is_idle": False,
        "idle_recommendation": None,
    }
    await storage.save_recommendation_cache("legacy01", legacy_payload, datetime.utcnow())

    cfg = DRIConfig()
    aggregator = MetricsAggregator(storage)
    pe = PolicyEngine(cfg)
    server = DRIAPIServer(cfg, storage, aggregator, pe)
    route_module.set_dependencies(storage, aggregator, pe, None, server.recommender, server.forecaster)

    async with AsyncClient(transport=ASGITransport(app=server.app), base_url="http://test") as client:
        resp = await client.get("/api/v1/recommendations")
        assert resp.status_code == 200
        recs = resp.json()["recommendations"]
        assert len(recs) == 1
        assert recs[0]["trend_evidence"] is None
        assert recs[0]["observation_minutes"] is None

    await storage.close()


# ---------------------------------------------------------------------------
# T8 — Integration: live memory-spike badge + early-observation trend together
# ---------------------------------------------------------------------------


async def test_t8_integration_live_spike_badge_with_early_observation_trend():
    """
    A sharp rise from 8.5% average to 74.5% now over 4 minutes must:
      1. Trigger the real-time spike anomaly immediately in /api/v1/metrics.
      2. Report 'Memory rising · early observation' (NOT 'Suspected memory leak')
         in /api/v1/recommendations while history is still < 30 min.
    """
    from httpx import ASGITransport, AsyncClient
    from dri.api import routes as route_module
    from dri.api.server import DRIAPIServer
    from dri.core.aggregator import MetricsAggregator
    from dri.core.engine import PolicyEngine
    from dri.core.storage import MetricsStorage
    from dri.utils.metrics import ContainerMetrics

    storage = MetricsStorage(":memory:")
    await storage.init()
    aggregator = MetricsAggregator(storage)

    now = datetime.now(timezone.utc)
    cid = "spike1234567"
    cname = "spike-app"

    # 19 baseline samples over ~3.8 min around 8.5%, then sharp spike to 74.5% at t=4 min
    samples: List[ContainerMetrics] = []
    for i in range(19):
        ts = now - timedelta(seconds=240 - i * 12)
        mem = 8.5 + (i % 3) * 0.1
        m = ContainerMetrics(
            container_id=cid,
            container_name=cname,
            timestamp=ts,
            cpu_percent=12.0,
            cpu_count=2,
            memory_usage=int(mem * 10_000_000),
            memory_limit=1_000_000_000,
            memory_percent=mem,
            net_io_in=0,
            net_io_out=0,
            block_io_read=0,
            block_io_write=0,
            pids=10,
        )
        samples.append(m)

    # 20th sample: sharp rise to 74.5% at t=4 min (now)
    spike_sample = ContainerMetrics(
        container_id=cid,
        container_name=cname,
        timestamp=now,
        cpu_percent=12.0,
        cpu_count=2,
        memory_usage=int(74.5 * 10_000_000),
        memory_limit=1_000_000_000,
        memory_percent=74.5,
        net_io_in=0,
        net_io_out=0,
        block_io_read=0,
        block_io_write=0,
        pids=10,
    )
    samples.append(spike_sample)

    for m in samples:
        await storage.save_metrics(m)
        aggregator.add_metrics(m)

    cfg = DRIConfig()
    pe = PolicyEngine(cfg)
    server = DRIAPIServer(cfg, storage, aggregator, pe)
    route_module.set_dependencies(storage, aggregator, pe, None, server.recommender, server.forecaster)

    async with AsyncClient(transport=ASGITransport(app=server.app), base_url="http://test") as client:
        # 1. Live metrics endpoint shows real-time spike immediately
        m_resp = await client.get("/api/v1/metrics")
        assert m_resp.status_code == 200
        m_data = m_resp.json()["containers"][0]
        assert m_data["is_anomaly"] is True
        assert m_data["anomaly_type"] == "memory_spike"
        assert m_data["memory_percent"] == pytest.approx(74.5)

        # 2. Recommendations endpoint gathers trend history without false leak label
        r_resp = await client.get("/api/v1/recommendations?force=true")
        assert r_resp.status_code == 200
        rec_data = r_resp.json()["recommendations"][0]
        assert rec_data["is_memory_leak"] is False
        assert rec_data["observation_minutes"] == pytest.approx(4.0, abs=0.1)
        te = rec_data["trend_evidence"]
        assert te is not None
        assert te["trend_label"] == "Memory rising · early observation"
        assert te["evidence_note"] == "20 samples over 4 min · 30 min required to assess a leak"

    await storage.close()


# ---------------------------------------------------------------------------
# T9 — Configurable thresholds
# ---------------------------------------------------------------------------


def test_t9_configurable_thresholds():
    """Thresholds in RecommenderConfig and ResourceRecommender are configurable."""
    cfg = DRIConfig()
    assert cfg.recommender.trend_min_elapsed_minutes == 30.0
    assert cfg.recommender.trend_min_distinct_samples == 20
    assert cfg.recommender.trend_required_growing_windows == 3
    assert cfg.recommender.trend_window_width_minutes == 10
    assert cfg.recommender.leak_slope_threshold_pct_per_hour == 0.5

    # Relaxed thresholds allow a 15-min, 12-sample, 3x5-min window series to qualify
    samples = rising_samples(16, span_minutes=15.0, start_pct=10.0, end_pct=40.0)
    assert assess(samples).trend_label == "Memory rising · early observation"
    ev_relaxed = assess(
        samples,
        min_elapsed_minutes=15.0,
        min_distinct_samples=15,
        required_growing_windows=3,
        window_width_minutes=5,
    )
    assert ev_relaxed.trend_label == "Suspected memory leak"
