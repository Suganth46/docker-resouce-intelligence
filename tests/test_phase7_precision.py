from datetime import datetime, timedelta
from pathlib import Path

import pytest

from dri.core.log_analyzer import LogAnalyzer
from dri.core.log_parser import detect_level


class Replay:
    def __init__(self, name):
        self.name = name
        self.now = datetime(2026, 1, 1)
        self.analyzer = LogAnalyzer(now_fn=lambda: self.now)

    def line(self, text, count=1):
        for _ in range(count):
            self.analyzer.ingest(
                dict(
                    container_id="abc",
                    container_name=self.name,
                    timestamp=self.now,
                    message=text,
                    level=detect_level(text),
                )
            )

    def stats(self):
        return self.analyzer.stats("abc")


@pytest.mark.parametrize("minutes", [5, 40])
@pytest.mark.parametrize("name", ["dri-agent", "worker"])
@pytest.mark.parametrize("fixture", ["dri_agent_startup_with_ollama_restarts", "nginx_access_idle"])
def test_idle(name, minutes, fixture):
    replay = Replay(name)
    lines = (
        (Path(__file__).parent / "fixtures/real_logs" / (fixture + ".txt")).read_text().splitlines()
    )
    for line in lines:
        replay.line(line)
        assert replay.stats().anomaly is None
        replay.now += timedelta(minutes=minutes / len(lines))
    replay.line("INFO request", 5000)
    assert replay.stats().anomaly is None


def test_spike_and_new_pattern():
    r = Replay("app")
    r.line("INFO startup")
    r.now += timedelta(minutes=40)
    r.line("ERROR db down")
    assert r.stats().anomaly == "new_error_pattern"
    r.line("ERROR db down", 59)
    assert r.stats().anomaly == "log_error_spike"
    assert "60 errors" in r.stats().anomaly_detail


def test_surge_debounce_and_spike_hysteresis():
    r = Replay("app")
    for _ in range(8):
        r.line("ERROR job failed")
        r.now += timedelta(minutes=5)
    r.line("ERROR job failed", 60)
    assert r.stats().anomaly != "log_template_surge"
    assert r.stats().anomaly == "log_template_surge"
    assert "60 in 5m" in r.stats().anomaly_detail
    r = Replay("app")
    r.line("ERROR bad", 10)
    assert r.stats().anomaly == "log_error_spike"
    r.now += timedelta(minutes=1)
    assert r.stats().anomaly is None
    r.line("ERROR bad", 9)
    assert r.stats().anomaly != "log_error_spike"
    r.line("ERROR bad", 2)
    assert r.stats().anomaly == "log_error_spike"


def test_excluded_errors_still_count():
    r = Replay("dri-agent")
    r.line("ERROR storage slow", 80)
    stats = r.stats()
    assert stats.errors_1m == 80
    assert stats.anomaly is None
