import asyncio
import hashlib
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest

from dri.api.models import MetricsResponse
from dri.config import LogConfig
from dri.core.aggregator import MetricsAggregator
from dri.core.engine import _resolve_metric_value
from dri.core.log_analyzer import LogAnalyzer, LogStats, TemplateStore, error_spike
from dri.core.log_collector import DockerLogCollector
from dri.core.log_parser import normalize_template, template_id, utcnow
from dri.core.recommender import z_score_anomaly
from dri.core.storage import SqliteStorage
from tests.test_phase1 import _make_metrics


@pytest.mark.parametrize(
    "token",
    [
        "2026-11-19T12:00:00.987654321+05:30",
        "781f2200-00aa-44bc-8721-166721ce0281",
        "10.0.0.5",
        "10.0.0.5:5432",
        "0xAF",
        "deAdbEEF",
        '"value 52"',
        "'retry later'",
        "/var/log/nginx",
        "dir/file",
        "-42.5",
    ],
)
def test_tokens(token):
    assert normalize_template(f"field   {token}") == "field <*>"


def test_template_identity():
    template = "User <*> failed login from <*>"
    for line in (
        "User 4821 failed login from 10.0.0.5:5432",
        "User 628 failed login from 127.0.0.1",
        template,
    ):
        assert normalize_template(line) == template
        assert (
            template_id(normalize_template(line))
            == hashlib.sha1(template.encode()).hexdigest()[:12]
        )


def entry(ts, message, level="INFO", cid="app"):
    return dict(container_id=cid, timestamp=ts, message=message, level=level)


def test_template_store(monkeypatch):
    now = datetime(2026, 1, 1)
    monkeypatch.setattr("dri.core.log_analyzer.utcnow", lambda: now)
    store = TemplateStore()
    first = now - timedelta(minutes=8)
    old, _ = store.observe(entry(first, "old"))
    key, fresh = store.observe(entry(first, "job 32", "WARN"))
    assert fresh
    assert store.observe(entry(now, "job 57", "WARN")) == (key, False)
    record = store.templates[key]
    assert (record.count, record.first_seen, record.last_seen, record.sample_message) == (
        2,
        first,
        now,
        "job 32",
    )
    assert store.top(2) == [record, store.templates[old]]
    assert store.top(2, now, {"WARN"}) == [record]
    assert store.new_since(5) == []
    for i in range(499):
        # Distinct alphabetic tokens survive number normalization.
        store.observe(entry(now, "event " + chr(0x4000 + i)))
    assert len(store.templates) == 500 and old not in store.templates and key in store.templates


def test_spike():
    zeros = [0.0] * 60
    assert z_score_anomaly(zeros, 80.0) == (False, 0.0)
    assert error_spike(zeros, 80.0) == (True, 80.0)
    assert error_spike(zeros, 2.0) == (False, 2.0)
    assert error_spike([1.0, 3.0] * 30, 40.0)[0]
    assert error_spike([1.0] * 4, 80.0) == (False, 0.0)


@pytest.mark.parametrize(
    "level,message,expected",
    [
        ("ERROR", "broken", "new_error_pattern"),
        ("WARN", "slow", "new_error_pattern"),
        ("INFO", "Killed job", "new_error_pattern"),
        ("INFO", "started", None),
    ],
)
def test_new_pattern(monkeypatch, level, message, expected):
    now = datetime(2026, 1, 1)
    monkeypatch.setattr("dri.core.log_analyzer.utcnow", lambda: now)
    analyzer = LogAnalyzer(warmup_minutes=10)
    analyzer.ingest(entry(now, "first failure", "ERROR"))
    assert analyzer.stats("app").anomaly is None
    now += timedelta(minutes=10)
    analyzer.ingest(entry(now, message, level))
    stats = analyzer.stats("app")
    assert stats.anomaly == expected and stats.new_templates_5m == 1
    if expected:
        assert message in stats.anomaly_detail


def test_surge(monkeypatch):
    now = datetime(2026, 1, 1)
    monkeypatch.setattr("dri.core.log_analyzer.utcnow", lambda: now)
    analyzer = LogAnalyzer()
    for i in range(12):
        analyzer.ingest(entry(now, f"job {i}"))
        now += timedelta(minutes=5)
    for i in range(50):
        analyzer.ingest(entry(now, f"job {i}"))
    stats = analyzer.stats("app")
    assert stats.anomaly == "log_template_surge" and "average 1)" in stats.anomaly_detail


def test_spike_excludes_current_minute(monkeypatch):
    now = datetime(2026, 1, 1)
    monkeypatch.setattr("dri.core.log_analyzer.utcnow", lambda: now)
    analyzer = LogAnalyzer()
    analyzer.ingest(entry(now, "ready"))
    now += timedelta(hours=1)
    for _ in range(80):
        analyzer.ingest(entry(now, "failure", "ERROR"))
    stats = analyzer.stats("app")
    assert stats.anomaly == "log_error_spike" and "score 80" in stats.anomaly_detail


@pytest.mark.parametrize(
    "memory,expected", [(10, "log_error_spike"), (99, "sustained_high_memory")]
)
async def test_aggregation(memory, expected):
    agg = MetricsAggregator(AsyncMock(get_recent_metrics=AsyncMock(return_value=[])))
    sample = _make_metrics()
    sample.memory_percent = memory
    agg.add_metrics(sample)
    agg.update_log_stats(
        sample.container_id, LogStats(anomaly="log_error_spike", new_templates_5m=3)
    )
    result = await agg.aggregate(sample.container_id)
    assert result.is_anomaly and result.anomaly_type == expected
    assert _resolve_metric_value("log_new_templates_5m", result) == 3
    assert MetricsResponse(**result.to_dict()).log_new_templates_5m == 3


async def test_persisted_template_id():
    storage = SqliteStorage(":memory:")
    await storage.init()
    container = Mock(id="app")
    container.name = "worker"
    container.logs.return_value = iter([(utcnow().isoformat() + "Z INFO job 123\n").encode()])
    client = Mock()
    client.containers.list.return_value = [container]
    collector = DockerLogCollector(client, LogConfig(), storage, LogAnalyzer().ingest)
    run = asyncio.create_task(collector.run())

    async def finish():
        while not container.logs.called or collector.containers_followed:
            await asyncio.sleep(0.01)
        await collector.close()
        await run

    try:
        await asyncio.wait_for(finish(), 5)
        (row,) = await storage.get_recent_logs("app")
        assert row.template_id == template_id("INFO job <*>")
    finally:
        await collector.close()
        await storage.close()
