"""Minute-level policy counters for sanitized log events."""

import re
import statistics
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from .log_parser import DEFAULT_CRASH, normalize_template, template_id, utcnow
from .recommender import z_score_anomaly


def error_spike(
    values: list[float], current: float, threshold=3.0, min_abs=5
) -> tuple[bool, float]:
    """Use current minus mean as an absolute fallback for a constant baseline."""
    if len(values) < 5:
        return False, 0.0
    if statistics.pstdev(values) < 1e-6:
        return current >= min_abs, current - statistics.mean(values)
    anomalous, score = z_score_anomaly(values, current, threshold)
    return anomalous and current >= min_abs, score


@dataclass
class Template:
    template: str
    level: str
    count: int
    first_seen: datetime
    last_seen: datetime
    sample_message: str
    crash: bool = False
    minutes: dict = field(default_factory=dict, repr=False)

    def window_count(self, start, end):
        return sum(count for minute, count in self.minutes.items() if start <= minute < end)


class TemplateStore:
    """One container's memory-only templates. After restart the store is empty.
    LogAnalyzer uses warmup_minutes to prevent a flood of false new-pattern alerts.
    """

    def __init__(self):
        self.templates: dict[str, Template] = {}

    def observe(self, entry) -> tuple[str, bool]:
        message, ts = entry["message"], entry["timestamp"]
        normalized = normalize_template(message)
        key = template_id(normalized)
        record = self.templates.get(key)
        is_new = record is None
        if is_new:
            record = Template(normalized, entry["level"], 0, ts, ts, message)
            self.templates[key] = record
            if len(self.templates) > 500:
                oldest = min(self.templates, key=lambda k: self.templates[k].last_seen)
                del self.templates[oldest]
        record.count += 1
        record.first_seen = min(record.first_seen, ts)
        minute = ts.replace(second=0, microsecond=0)
        if minute > record.last_seen.replace(second=0, microsecond=0):
            cutoff = minute - timedelta(minutes=64)
            record.minutes = {m: n for m, n in record.minutes.items() if m >= cutoff}
        record.last_seen = max(record.last_seen, ts)
        if minute >= record.last_seen.replace(second=0, microsecond=0) - timedelta(minutes=64):
            record.minutes[minute] = record.minutes.get(minute, 0) + 1
        if entry["level"] == "ERROR" or (record.level != "ERROR" and entry["level"] == "WARN"):
            record.level = entry["level"]
        return key, is_new

    def top(self, n=10, since=None, levels=None):
        """Rank by lifetime count, filtering by last observation and level."""
        records = (
            r
            for r in self.templates.values()
            if (since is None or r.last_seen >= since) and (levels is None or r.level in levels)
        )
        return sorted(records, key=lambda r: (r.count, r.last_seen), reverse=True)[:n]

    def new_since(self, minutes):
        cutoff = utcnow() - timedelta(minutes=minutes)
        return [r for r in self.templates.values() if r.first_seen >= cutoff]


@dataclass
class LogStats:
    errors_1m: int = 0
    errors_5m: int = 0
    warnings_5m: int = 0
    crash_signals_5m: int = 0
    total_5m: int = 0
    lines_per_min: float = 0
    last_error_ts: Optional[datetime] = None
    anomaly: Optional[str] = None
    anomaly_detail: Optional[str] = None
    new_templates_5m: int = 0


@dataclass
class Bucket:
    minute: datetime
    total: int = 0
    errors: int = 0
    warnings: int = 0
    crashes: int = 0


@dataclass
class History:
    last_seen: datetime
    observed_from: datetime
    templates: TemplateStore = field(default_factory=TemplateStore)
    buckets: deque = field(default_factory=lambda: deque(maxlen=61))
    last_error: Optional[datetime] = None


class LogAnalyzer:
    def __init__(self, crash_patterns=DEFAULT_CRASH, *, warmup_minutes=10):
        self.warmup_minutes = warmup_minutes
        self.patterns = [re.compile(pattern, re.I) for pattern in crash_patterns]
        self.history = {}
        self.next_cleanup = datetime.min

    def _expire(self, now):
        if now >= self.next_cleanup:
            cutoff = now - timedelta(days=1)
            self.history = {cid: h for cid, h in self.history.items() if h.last_seen >= cutoff}
            self.next_cleanup = now + timedelta(minutes=1)

    def ingest(self, entry: dict) -> None:
        now = utcnow()
        self._expire(now)
        ts = entry["timestamp"]
        minute = ts.replace(second=0, microsecond=0)
        cid = entry["container_id"]
        if cid not in self.history:
            self.history[cid] = History(ts, now)
        history = self.history[cid]
        history.last_seen = max(ts, history.last_seen)
        store = history.templates
        key, _ = store.observe(entry)
        entry["template_id"] = key
        crash = any(p.search(entry["message"]) for p in self.patterns)
        if crash and key in store.templates:
            store.templates[key].crash = True
        if minute < now.replace(second=0, microsecond=0) - timedelta(minutes=60) or minute > now:
            return
        buckets = history.buckets
        if not buckets or buckets[-1].minute < minute:
            buckets.append(Bucket(minute))
        bucket = next((b for b in reversed(buckets) if b.minute == minute), None)
        if bucket is None:
            bucket = Bucket(minute)
            history.buckets = deque(
                sorted([*buckets, bucket], key=lambda b: b.minute)[-61:], maxlen=61
            )
        bucket.total += 1
        if entry["level"] == "ERROR":
            bucket.errors += 1
            history.last_error = max(history.last_error or ts, ts)
        bucket.warnings += entry["level"] == "WARN"
        bucket.crashes += crash

    def stats(self, container_id: str) -> LogStats:
        now = utcnow()
        self._expire(now)
        history = self.history.get(container_id)
        result = LogStats()
        if history:
            result.last_error_ts = history.last_error
            minute = now.replace(second=0, microsecond=0)
            for bucket in reversed(history.buckets):
                if bucket.minute <= minute - timedelta(minutes=5):
                    break
                if bucket.minute == minute:
                    result.errors_1m = bucket.errors
                result.total_5m += bucket.total
                result.errors_5m += bucket.errors
                result.warnings_5m += bucket.warnings
                result.crash_signals_5m += bucket.crashes
            result.lines_per_min = result.total_5m / 5
            self._detect(history, result, minute, now)
        return result

    def _detect(self, history, result, minute, now):
        recent = history.templates.new_since(5)
        result.new_templates_5m = len(recent)
        errors = {bucket.minute: bucket.errors for bucket in history.buckets}
        baseline = [errors.get(minute - timedelta(minutes=i), 0) for i in range(1, 61)]
        spike, score = error_spike(baseline, result.errors_1m)
        if spike:
            result.anomaly = "log_error_spike"
            result.anomaly_detail = f"{result.errors_1m} errors this minute (score {score:g})"
            return

        warmed_at = history.observed_from + timedelta(minutes=self.warmup_minutes)
        if now >= warmed_at:
            for record in recent:
                if record.level in ("WARN", "ERROR") or record.crash:
                    result.anomaly = "new_error_pattern"
                    result.anomaly_detail = record.template
                    return

        end = minute + timedelta(minutes=1)
        start = end - timedelta(minutes=5)
        baseline_start = start - timedelta(minutes=60)
        for record in history.templates.top(500, since=start):
            count = record.window_count(start, end)
            if count < 5:
                continue
            average = record.window_count(baseline_start, start) / 12
            if average > 0 and count >= 10 * average:
                result.anomaly = "log_template_surge"
                result.anomaly_detail = (
                    f"{record.template}: {count} in 5 minutes (average {average:g})"
                )
                return
