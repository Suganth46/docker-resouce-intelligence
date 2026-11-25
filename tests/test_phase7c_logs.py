"""Windowed explanations, alert enrichment, and protected HTTP routes."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient, ASGITransport

from dri.api import routes, server
from dri.api.server import DRIAPIServer
from dri.config import DRIConfig
from dri.core.aggregator import MetricsAggregator
from dri.core.engine import PolicyEngine
from dri.core.log_analyzer import LogAnalyzer, summary
from dri.core.log_parser import utcnow
from dri.core.storage import SqliteStorage
from dri.main import DRIAgent


def event(action, **attrs):
    return SimpleNamespace(action=action, timestamp=utcnow(), attributes=attrs)


async def report(events=(), lines=(), memory=30):
    storage = AsyncMock()
    storage.get_recent_logs.return_value = []
    storage.get_recent_events.return_value = events
    analyzer = LogAnalyzer(storage=storage)
    for line in lines:
        analyzer.ingest(dict(container_id="web", message=line, timestamp=utcnow(), level="ERROR"))
    return await analyzer.explain(
        "web",
        aggregated=SimpleNamespace(memory_percent=memory, cpu_percent=10, memory_growth_rate_5m=0),
    )


async def test_oom_precedes_dependency():
    data = await report([event("oom")], ["Killed worker"] + ["connection refused"] * 8, 96)
    assert (data.verdict, data.confidence) == ("oom_kill_confirmed", "high")
    text = summary(data)
    assert all(term in text for term in ("oom event", "96%", "Killed"))
    assert data.error_count == 9 and data.crash_signals["connection refused"] == 8
    assert data.top_templates[0]["count"] == 8 and len(data.new_templates) == 2


async def test_137_needs_corroboration():
    old = event("oom")
    old.timestamp -= timedelta(minutes=11)
    data = await report([event("die", exitCode="137"), old])
    assert data.verdict == "none" and len(data.related_events) == 1


@pytest.mark.parametrize(
    "actions,lines,verdict,confidence",
    [
        (["die"] * 3, [], "crash_loop", "high"),
        (["die", "start"], [], "crash_loop", "high"),
        ([], ["connection refused"] * 8, "dependency_failure", "medium"),
        ([], ["no route to host and name resolution"] * 4, "none", "low"),
        ([], ["no space left"], "resource_exhaustion", "medium"),
        ([], ["unexpected exception"] * 8, "error_spike", "medium"),
        ([], [], "none", "low"),
    ],
)
async def test_rules(actions, lines, verdict, confidence):
    data = await report([event(a) for a in actions], lines)
    assert (data.verdict, data.confidence) == (verdict, confidence)
    assert data.reasons


@pytest.mark.parametrize("mode", ["success", "exception", "timeout"])
async def test_alert_saved(mode):
    agent = DRIAgent(DRIConfig())
    agent.storage = AsyncMock()
    alert = SimpleNamespace(
        container_id="web",
        alert_type="warning",
        metric="cpu",
        current_value=90,
        message="CPU warning",
    )

    if mode == "success":
        alert.message = "X" * 500

    async def explain(*args):
        if mode == "exception":
            raise ValueError("unavailable")
        if mode == "timeout":
            await asyncio.sleep(5)
        return await report(lines=["timed out"] * 5)

    agent.log_analyzer = SimpleNamespace(explain=explain)
    await agent._on_alert(alert)
    agent.storage.save_alert.assert_awaited_once_with(alert)
    if mode == "success":
        assert " | evidence: dependency_failure" in alert.message and len(alert.message) <= 500
    else:
        assert alert.message == "CPU warning"


async def test_http(monkeypatch):
    for key in (
        "storage",
        "aggregator",
        "policy_engine",
        "agent",
        "recommender",
        "forecaster",
        "log_analyzer",
    ):
        monkeypatch.setattr(routes, key, None)
    monkeypatch.setattr(server, "_configured_api_key", None)
    st = SqliteStorage(":memory:")
    await st.init()
    try:
        cfg = DRIConfig()
        cfg.api.api_key = "test-key"
        ag, pe = MetricsAggregator(st), PolicyEngine(cfg)
        analyzer = LogAnalyzer(storage=st)
        entries = [
            dict(
                timestamp=utcnow(),
                message=level,
                level=level,
                container_id="web",
                container_name="worker",
            )
            for level in ("INFO", "ERROR", "ERROR")
        ]
        for entry in entries:
            analyzer.ingest(entry)
        await st.save_logs(entries)
        service = DRIAPIServer(cfg, st, ag, pe, log_analyzer=analyzer)
        async with AsyncClient(
            transport=ASGITransport(app=service.app), base_url="http://test"
        ) as client:
            logs = "/api/v1/logs/web"
            explain = "/api/v1/containers/web/explain"
            for url in (logs, explain):
                assert (await client.get(url)).status_code == 401
            client.headers["X-Api-Key"] = "test-key"
            assert (await client.get(logs)).json()["total_returned"] == 3
            data = (await client.get(logs, params={"level": "ERROR", "limit": 1})).json()
            assert data["total_returned"] == 1 and data["logs"][0]["level"] == "ERROR"
            for params in ({"limit": 1001}, {"level": "INVALID"}, {"minutes": 1441}):
                assert (await client.get(logs, params=params)).status_code == 422
            assert (await client.get(explain, params={"window_minutes": 121})).status_code == 422
            data = (await client.get(explain)).json()
            assert set(data) == {
                "container_id",
                "window_minutes",
                "generated_at",
                "error_count",
                "top_templates",
                "new_templates",
                "crash_signals",
                "related_events",
                "llm_summary",
                "llm_status",
                "metric_context",
                "coverage",
                "active_anomaly",
                "verdict",
                "confidence",
                "reasons",
            }
            assert data["error_count"] == 2 and data["top_templates"][0]["sample"] == "ERROR"
            routes.set_dependencies(st, ag, pe, None, None, None)
            for url in (logs, explain):
                res = await client.get(url)
                assert res.status_code == 503 and res.json()["detail"] == "Log analysis not enabled"
    finally:
        await st.close()
