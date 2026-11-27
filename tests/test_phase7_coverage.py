from types import SimpleNamespace

import pytest

from dri.core.log_analyzer import LogAnalyzer


@pytest.mark.parametrize("cpu,growth,active,verdict,detail", [
    (0, 19.8, True, "memory_growth_suspected", "19.8%/min"),
    (145, 0, False, "cpu_saturation", "145.0%"),
    (10, 0, True, "inconclusive_no_logs", "Active anomaly"),
    (10, 0, False, "none", "0 log lines"),
])
async def test_silent(cpu, growth, active, verdict, detail):
    analyzer = LogAnalyzer()
    analyzer.collection_state = lambda cid: "following"
    metrics = SimpleNamespace(cpu_percent_5m=cpu, memory_growth_rate_5m=growth,
                              memory_percent=35.5, memory_percent_5m=58.4, is_anomaly=active,
                              anomaly_type="cpu_spike", anomaly_detail="CPU z=4 over 20 samples")
    report = await analyzer.explain("silent", aggregated=metrics)
    assert report.verdict == verdict and detail in " ".join(report.reasons)
    assert report.coverage["logs_status"] == "no_output"
    assert report.coverage["metrics_available"]
    if growth:
        assert report.confidence == "low" and "noisy" in " ".join(report.reasons)


async def test_crash_loop_lists_death_times():
    from tests.test_phase7c_logs import event, report

    deaths = [event("die") for _ in range(3)]
    result = await report(deaths)
    assert result.verdict == "crash_loop"
    assert result.reasons[1:] == [f"die at {e.timestamp:%H:%M:%S} UTC" for e in deaths]


async def test_metrics_remain_visible_when_log_collection_disabled(monkeypatch):
    from unittest.mock import AsyncMock
    from dri.api import routes
    from dri.config import DRIConfig

    cfg = DRIConfig()
    cfg.logs.enabled = False
    aggregator = AsyncMock()
    aggregator.aggregate.return_value = SimpleNamespace(cpu_percent_5m=145)
    monkeypatch.setattr(routes, "agent", SimpleNamespace(config=cfg))
    monkeypatch.setattr(routes, "aggregator", aggregator)
    monkeypatch.setattr(routes, "log_analyzer", None)
    monkeypatch.setattr(routes, "storage", None)
    result = await routes.explain_container("app", 10)
    assert result.verdict == "cpu_saturation"
    assert result.coverage["logs_status"] == "disabled"


async def test_restart_preserves_window_coverage_and_top_templates():
    from dri.core.storage import SqliteStorage

    store = SqliteStorage(":memory:")
    await store.init()
    try:
        await assert_restart_coverage(store)
    finally:
        await store.close()


async def assert_restart_coverage(store):
    from datetime import timedelta
    from dri.core.log_parser import utcnow

    now = utcnow()
    live = LogAnalyzer(storage=store, now_fn=lambda: now)
    entries = []
    for i in range(120):
        row = dict(container_id="app", timestamp=now - timedelta(seconds=120-i),
                   level="INFO", message="INFO ready" if i < 70 else "INFO stopped")
        live.ingest(row)
        entries.append(row)
    await store.save_logs(entries + [
        dict(entries[0], container_id="other"),
        dict(entries[0], timestamp=now - timedelta(minutes=11)),
        dict(entries[0], timestamp=now + timedelta(minutes=1)),
    ])
    fresh = LogAnalyzer(storage=store, now_fn=lambda: now)
    for analyzer in (live, fresh):
        report = await analyzer.explain("app")
        assert report.coverage["log_lines_in_window"] == 120
        assert [(t["template"], t["count"]) for t in report.top_templates] == [
            ("INFO ready", 70), ("INFO stopped", 50)
        ]
    assert not (await fresh.explain("app")).new_templates


async def test_older_errors_without_template_ids_count_beyond_the_sample():
    from datetime import timedelta
    from dri.core.log_parser import utcnow
    from dri.core.storage import SqliteStorage

    store = SqliteStorage(":memory:")
    await store.init()
    try:
        now = utcnow()
        old = dict(container_id="app", timestamp=now - timedelta(minutes=1),
                   level="ERROR", message="failure")
        await store.save_logs([old] * 60 + [dict(old, timestamp=now, level="INFO")] * 50)
        report = await LogAnalyzer(storage=store, now_fn=lambda: now).explain("app")
        assert report.coverage["log_lines_in_window"] == 110
        assert report.error_count == 60
        assert report.top_templates == []
    finally:
        await store.close()
