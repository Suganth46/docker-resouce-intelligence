"""Minute-level policy counters for sanitized log events."""

import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from .log_parser import DEFAULT_CRASH, utcnow


@dataclass
class LogStats:
    errors_1m: int = 0
    errors_5m: int = 0
    warnings_5m: int = 0
    crash_signals_5m: int = 0
    total_5m: int = 0
    lines_per_min: float = 0
    last_error_ts: Optional[datetime] = None


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
    buckets: deque = field(default_factory=lambda: deque(maxlen=60))
    last_error: Optional[datetime] = None


class LogAnalyzer:
    def __init__(self, crash_patterns=DEFAULT_CRASH):
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
        if minute <= now - timedelta(hours=1) or minute > now:
            return
        cid = entry["container_id"]
        if cid not in self.history:
            self.history[cid] = History(ts)
        history = self.history[cid]
        history.last_seen = max(ts, history.last_seen)
        buckets = history.buckets
        if not buckets or buckets[-1].minute < minute:
            buckets.append(Bucket(minute))
        bucket = next((b for b in reversed(buckets) if b.minute == minute), None)
        if bucket is None:
            bucket = Bucket(minute)
            history.buckets = deque(
                sorted([*buckets, bucket], key=lambda b: b.minute)[-60:], maxlen=60
            )
        bucket.total += 1
        if entry["level"] == "ERROR":
            bucket.errors += 1
            history.last_error = max(history.last_error or ts, ts)
        bucket.warnings += entry["level"] == "WARN"
        bucket.crashes += any(p.search(entry["message"]) for p in self.patterns)

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
        return result
