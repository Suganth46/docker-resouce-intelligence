import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import text

from dri.api.models import MetricsResponse
from dri.config import DRIConfig, LogConfig, ThresholdRule
from dri.core.aggregator import MetricsAggregator
from dri.core.engine import PolicyEngine
from dri.core.log_analyzer import LogAnalyzer
from dri.core.log_collector import DockerLogCollector
from dri.core.log_parser import (
    MultilineJoiner,
    detect_level,
    mask_sensitive,
    parse_docker_line,
    truncate,
    utcnow,
)
from dri.core.storage import SqliteStorage
from dri.core.storage_timescale import TimescaleStorage
from tests.test_phase1 import _make_metrics


def test_parser():
    assert parse_docker_line(b"2026-11-19T05:30:01.123456789Z hi\n") == (
        datetime(2026, 11, 19, 5, 30, 1, 123456),
        "hi",
    )
    before = utcnow()
    ts, msg = parse_docker_line(b"missing \xff\n")
    assert before <= ts <= utcnow() and msg == "missing \ufffd"


@pytest.mark.parametrize(
    "message,expected",
    [
        ("warning", "WARN"),
        ("DEBUG", "DEBUG"),
        ('{"level":"error"}', "ERROR"),
        ('{"severity":"info"}', "INFO"),
        ("Traceback (most recent call last)", "ERROR"),
        ("INFORMATION", "UNKNOWN"),
    ],
)
def test_level(message, expected):
    assert detect_level(message) == expected


def test_mask():
    message = "a@b.org Bearer abc.def eyJabc.def.ghi AKIA1234567890123456 password=hunter2"
    masked = mask_sensitive(message)
    assert masked == "<email> Bearer <redacted> <redacted> <redacted> password=<redacted>"
    assert mask_sensitive(masked) == masked
    assert truncate("abcdef", 3) == "abc …[truncated]"


def test_multiline():
    joiner = MultilineJoiner()
    now = utcnow()
    lines = [
        "Traceback",
        '  File "app.py"',
        "    run()",
        '  File "worker.py"',
        "    fail()",
        "ValueError: failure",
    ]
    for line in lines:
        assert joiner.push(now, line) == []
    assert joiner.push(now + timedelta(seconds=2), "other") == [(now, "\n".join(lines))]
    joiner.flush()
    for _ in range(20):
        assert not joiner.push(now, " at worker")
    assert len(joiner.push(now, " at next")[0][1].splitlines()) == 20


def test_contract_and_config():
    assert not SqliteStorage.__abstractmethods__ and not TimescaleStorage.__abstractmethods__
    with pytest.raises(ValueError, match="Invalid log regex"):
        LogConfig(crash_patterns=["["])


async def check_storage(storage):
    now = utcnow()
    entries = [
        dict(
            container_id="logtest",
            container_name="app",
            timestamp=now - timedelta(seconds=i),
            level="ERROR" if i % 2 else "INFO",
            message=str(i),
        )
        for i in range(100)
    ]
    await storage.save_logs(entries + [dict(entries[0], timestamp=now - timedelta(days=2))])
    rows = await storage.get_recent_logs("logtest")
    assert [r.message for r in rows] == [str(i) for i in reversed(range(100))]
    assert rows[0].timestamp.tzinfo is None and rows[0].template_id is None
    filtered = await storage.get_recent_logs("logtest", level="ERROR", limit=3)
    assert [r.message for r in filtered] == ["5", "3", "1"]
    assert await storage.cleanup_old_logs() == 1
    assert len(await storage.get_recent_logs("logtest", minutes=4000)) == 100


async def test_sqlite_storage():
    storage = SqliteStorage(":memory:")
    await storage.init()
    try:
        await check_storage(storage)
    finally:
        await storage.close()


@pytest.mark.sqlite_only
async def test_sqlite_index():
    storage = SqliteStorage(":memory:")
    await storage.init()
    with storage.engine.connect() as conn:
        rows = conn.execute(
            text(
                "EXPLAIN QUERY PLAN SELECT * FROM container_logs WHERE "
                "container_id='logtest' AND timestamp >= '2026' ORDER BY timestamp"
            )
        ).all()
        assert "ix_container_logs_cid_ts" in str(rows)
    await storage.close()


async def test_log_policy(monkeypatch):
    now = utcnow()
    monkeypatch.setattr("dri.core.log_analyzer.utcnow", lambda: now)
    analyzer = LogAnalyzer()
    entry = dict(container_id="abc123", level="ERROR", timestamp=now, message="failure")
    for _ in range(30):
        analyzer.ingest(entry)
    analyzer.ingest(dict(entry, timestamp=now - timedelta(minutes=6)))
    analyzer.ingest(dict(entry, level="WARN", message="Connection Refused"))
    stats = analyzer.stats("abc123")
    assert (
        stats.errors_1m,
        stats.errors_5m,
        stats.warnings_5m,
        stats.crash_signals_5m,
        stats.total_5m,
        stats.last_error_ts,
    ) == (30, 30, 1, 1, 31, now)
    agg = MetricsAggregator(AsyncMock(get_recent_metrics=AsyncMock(return_value=[])))
    agg.add_metrics(_make_metrics())
    agg.update_log_stats("abc123", stats)
    metrics = await agg.aggregate("abc123")
    response = MetricsResponse(**metrics.to_dict())
    assert (
        response.log_errors_1m,
        response.log_errors_5m,
        response.log_warnings_5m,
        response.log_crash_signals_5m,
    ) == (30, 30, 1, 1)
    config = DRIConfig()
    config.global_policy.rules = [
        ThresholdRule(
            metric="log_errors_1m", operator=">", value=20, duration_seconds=0, action="warning"
        )
    ]
    (alert,) = await PolicyEngine(config).evaluate(metrics)
    assert "log_errors_1m > 20.0 for" in alert.message and "%" not in alert.message


async def test_threaded_flood():
    stamp = utcnow().isoformat().encode() + b"Z "
    lines = [stamp + b"INFO password=hunter2\n"] * 1000 + [stamp + b"ERROR failure\n"] * 10
    container = Mock(id="123456789012abcd")
    container.name = "worker"
    container.logs.return_value = iter(lines)
    client = Mock()
    client.containers.list.return_value = [container]
    storage = AsyncMock()
    entries = []
    collector = DockerLogCollector(
        client, LogConfig(max_lines_per_second=20), storage, entries.append
    )
    task = asyncio.create_task(collector.run())

    async def finish():
        while (
            not container.logs.called
            or collector.containers_followed
            or not collector.queue.empty()
        ):
            await asyncio.sleep(0.01)
        await collector.close()
        await task

    await asyncio.wait_for(finish(), timeout=5)
    assert sum(e["level"] == "ERROR" for e in entries) == 10
    assert 20 <= sum(e["level"] == "INFO" for e in entries) < 40
    assert [e["message"] for e in entries if e["level"] == "WARN"] == [
        f"DRI dropped {collector.lines_dropped} log lines (rate limit)"
    ]
    assert all("hunter2" not in e["message"] for e in entries)
    assert [e for call in storage.save_logs.await_args_list for e in call.args[0]] == entries


async def test_reconnect_and_unsupported_driver():
    from docker.errors import APIError

    collector_storage = AsyncMock()
    stamp = utcnow().isoformat().encode() + b"Z "
    normal = Mock(id="reader")
    normal.name = "reader"
    normal.logs.side_effect = [
        iter([stamp + b"INFO first"]),
        iter([stamp + b"INFO first", stamp + b"INFO second"]),
    ]
    unsupported = Mock(id="other")
    unsupported.logs.side_effect = APIError("configured logging driver does not support reading")
    client = Mock()
    client.containers.list.return_value = [normal, unsupported]
    entries = []
    collector = DockerLogCollector(client, LogConfig(), collector_storage, entries.append)
    rounds = 0

    async def wait_for_streams(interval):
        nonlocal rounds
        while collector.containers_followed:
            await asyncio.sleep(0.001)
        rounds += 1
        if rounds == 2:
            collector.stop()

    # Drive discovery twice without waiting 15 seconds between polls.
    collector._running = True
    collector._wait = wait_for_streams
    await asyncio.wait_for(collector._discover_loop(), 5)
    await collector.close()
    assert [entry["message"] for entry in entries] == ["INFO first", "INFO second"]
    assert unsupported.logs.call_count == 1
