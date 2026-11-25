"""Minute-level policy counters for sanitized log events."""

from fnmatch import fnmatchcase
import json
import threading
import re
import statistics
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from .log_parser import DEFAULT_CRASH, normalize_template, template_id, utcnow
from ..utils.metrics import AggregatedMetrics
from .storage_base import StorageBackend
from .root_cause import LLMSummary
from .recommender import z_score_anomaly

DEPENDENCIES = {
    "connection refused",
    "timed out",
    "ECONNREFUSED",
    "no route to host",
    "name resolution",
}


@dataclass
class Explanation:
    container_id: str
    window_minutes: int
    generated_at: datetime
    error_count: int
    top_templates: list
    new_templates: list
    crash_signals: dict
    related_events: list
    metric_context: dict
    verdict: str
    confidence: str
    reasons: list[str]
    llm_summary: Optional[LLMSummary] = None
    llm_status: str = "disabled"


def summary(explanation: Explanation, max_len=300) -> str:
    return " ".join((explanation.verdict + ": " + "; ".join(explanation.reasons)).split())[:max_len]


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
            cutoff = minute - timedelta(minutes=120)
            record.minutes = {m: n for m, n in record.minutes.items() if m >= cutoff}
        record.last_seen = max(record.last_seen, ts)
        if minute >= record.last_seen.replace(second=0, microsecond=0) - timedelta(minutes=120):
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

    def new_since(self, minutes, now=None):
        cutoff = (now or utcnow()) - timedelta(minutes=minutes)
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
    last_timestamp: datetime = datetime.min
    total: int = 0
    errors: int = 0
    warnings: int = 0
    crashes: int = 0
    signals: Counter = field(default_factory=Counter)
    dependency_lines: int = 0


@dataclass
class History:
    last_seen: datetime
    observed_from: datetime
    templates: TemplateStore = field(default_factory=TemplateStore)
    buckets: deque = field(default_factory=lambda: deque(maxlen=121))
    excluded: bool = False
    surge_candidates: set = field(default_factory=set)
    last_signal: Optional[tuple] = None
    cleared: dict = field(default_factory=dict)
    last_error: Optional[datetime] = None


class LogAnalyzer:
    def __init__(
        self,
        crash_patterns=DEFAULT_CRASH,
        *,
        warmup_minutes=10,
        storage: Optional[StorageBackend] = None,
        root_cause=None,
        now_fn=None,
        surge_warmup_minutes=30,
        policy_exclude_names=("dri-*",),
    ):
        self.now = now_fn or utcnow
        self.surge_warmup_minutes = surge_warmup_minutes
        self.exclusions = policy_exclude_names
        self.root_cause = root_cause
        self.storage = storage
        self._lock = threading.RLock()
        self.evidence_patterns = [
            re.compile(p, re.I)
            for p in dict.fromkeys((*crash_patterns, *DEFAULT_CRASH, *DEPENDENCIES))
        ]
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
        with self._lock:
            self._ingest(entry)

    def _ingest(self, entry):
        now = self.now()
        self._expire(now)
        ts = entry["timestamp"]
        minute = ts.replace(second=0, microsecond=0)
        cid = entry["container_id"]
        if cid not in self.history:
            self.history[cid] = History(ts, now)
        history = self.history[cid]
        history.last_seen = max(ts, history.last_seen)
        history.excluded = any(
            fnmatchcase(entry.get("container_name", cid), pat) for pat in self.exclusions
        )
        store = history.templates
        key, _ = store.observe(entry)
        entry["template_id"] = key
        telemetry = bool(
            re.search(r"\bDocker event: \w+|^DRI dropped \d+ log lines", entry["message"])
        )
        crash = not telemetry and any(p.search(entry["message"]) for p in self.patterns)
        if crash and key in store.templates:
            store.templates[key].crash = True
        if minute < now.replace(second=0, microsecond=0) - timedelta(minutes=120) or minute > now:
            return
        buckets = history.buckets
        if not buckets or buckets[-1].minute < minute:
            buckets.append(Bucket(minute))
        bucket = next((b for b in reversed(buckets) if b.minute == minute), None)
        if bucket is None:
            bucket = Bucket(minute)
            history.buckets = deque(
                sorted([*buckets, bucket], key=lambda b: b.minute)[-121:], maxlen=121
            )
        bucket.last_timestamp = max(bucket.last_timestamp, ts)
        bucket.total += 1
        if telemetry:
            return
        if entry["level"] == "ERROR":
            bucket.errors += 1
            history.last_error = max(history.last_error or ts, ts)
        bucket.warnings += entry["level"] == "WARN"
        bucket.crashes += crash
        matches = self._signals(entry["message"])
        bucket.signals.update(matches)
        bucket.dependency_lines += bool(matches.intersection(DEPENDENCIES))

    def _signals(self, message):
        return {p.pattern for p in self.evidence_patterns if p.search(message)}

    def stats(self, container_id: str) -> LogStats:
        with self._lock:
            return self._stats(container_id)

    def _stats(self, container_id):
        now = self.now()
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
            recent = history.templates.new_since(5, now)
            result.new_templates_5m = len(recent)
            if not history.excluded:
                signal = self._detect(history, result, minute, now, recent)
                previous = history.last_signal
                key = signal[:2] if signal else None
                if previous and previous[:2] != key:
                    history.cleared[previous[:2]] = (now, previous[2])
                history.last_signal = None
                history.cleared = {
                    k: v for k, v in history.cleared.items() if now - v[0] < timedelta(minutes=5)
                }
                if signal and (key not in history.cleared or signal[2] > history.cleared[key][1]):
                    peak = (
                        max(signal[2], previous[2])
                        if previous and previous[:2] == key
                        else signal[2]
                    )
                    history.last_signal = (*key, peak)
                    history.cleared.pop(key, None)
                    result.anomaly, result.anomaly_detail = signal[0], signal[3]
        return result

    def _detect(self, history, result, minute, now, recent):
        end = minute + timedelta(minutes=1)
        start = end - timedelta(minutes=5)
        baseline_start = start - timedelta(minutes=60)
        windows = {
            (start - b.minute - timedelta(minutes=1)) // timedelta(minutes=5)
            for b in history.buckets
            if baseline_start <= b.minute < start
        }
        eligible = [
            r
            for r in history.templates.templates.values()
            if (r.level in ("WARN", "ERROR") or r.crash)
            and not re.search(r"\bDocker event: \w+|^DRI dropped ", r.template)
        ]
        candidates = set()
        surge = None
        if len(windows) >= 6 and now - history.observed_from >= timedelta(
            minutes=self.surge_warmup_minutes
        ):
            for r in eligible:
                prior = r.window_count(baseline_start, start)
                count = r.window_count(start, end)
                average = max(prior / len(windows), 1.0)
                if prior and count >= max(20, 10 * average):
                    candidates.add(r.template)
                    if r.template in history.surge_candidates and (
                        surge is None or count > surge[2]
                    ):
                        surge = (
                            "log_template_surge",
                            r.template,
                            count,
                            f"{r.template}: {count} in 5m vs {average:.1f} average per 5m "
                            f"over the preceding 60m ({count / average:.1f}x)",
                        )
        history.surge_candidates = candidates
        if surge:
            return surge
        errors = {b.minute: b.errors for b in history.buckets}
        baseline = [errors.get(minute - timedelta(minutes=i), 0) for i in range(1, 61)]
        spike, score = error_spike(baseline, result.errors_1m)
        if spike and result.errors_1m > statistics.mean(baseline):
            return (
                "log_error_spike",
                "",
                result.errors_1m,
                f"{result.errors_1m} errors this minute vs {statistics.mean(baseline):.1f}/min over 60m (score {score:g})",
            )
        warmed_at = history.observed_from + timedelta(minutes=self.warmup_minutes)
        for r in eligible:
            if r.first_seen >= warmed_at and r in recent:
                count = r.window_count(start, end)
                return (
                    "new_error_pattern",
                    r.template,
                    count,
                    f"New {r.level} pattern '{r.template}' occurred {count} times in 5m",
                )

    async def explain(
        self, container_id: str, window_minutes=10, aggregated: Optional[AggregatedMetrics] = None
    ) -> Explanation:
        """Correlate recent observations using ordered heuristic verdicts."""
        now = self.now()
        since = now - timedelta(minutes=window_minutes)
        signals = Counter()
        errors = dependency_lines = 0
        templates = {}
        with self._lock:
            stats = self._stats(container_id)
            history = self.history.get(container_id)
            if history:
                start = since.replace(second=0, microsecond=0)
                for bucket in history.buckets:
                    if since <= bucket.last_timestamp <= now:
                        signals.update(bucket.signals)
                        dependency_lines += bucket.dependency_lines
                        errors += bucket.errors
                for key, record in history.templates.templates.items():
                    if record.last_seen >= since:
                        templates[key] = dict(
                            template_id=key,
                            template=record.template,
                            level=record.level,
                            count=record.window_count(start, now + timedelta(microseconds=1)),
                            first_seen=record.first_seen,
                            sample=record.sample_message,
                        )

        events = []
        if self.storage is not None:
            rows = await self.storage.get_recent_logs(
                container_id, minutes=window_minutes, limit=50
            )
            sampled_signals = Counter()
            sampled_errors = sampled_dependencies = 0
            for row in rows:
                matches = self._signals(row.message)
                sampled_signals.update(matches)
                sampled_errors += row.level == "ERROR"
                sampled_dependencies += bool(matches.intersection(DEPENDENCIES))
                if row.template_id in templates:
                    templates[row.template_id]["sample"] = row.message
            # The persisted sample overlaps live counters. Take a lower bound
            # from each source without counting the same lines twice.
            signals |= sampled_signals
            errors = max(errors, sampled_errors)
            dependency_lines = max(dependency_lines, sampled_dependencies)
            for event in await self.storage.get_recent_events(container_id=container_id, limit=100):
                ts = event.timestamp
                if ts.tzinfo is not None:
                    ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
                if since <= ts <= now:
                    attrs = event.attributes or {}
                    if isinstance(attrs, str):
                        attrs = json.loads(attrs)
                    events.append(dict(action=event.action, timestamp=ts, attributes=attrs))
        context = {
            key: getattr(aggregated, key, None)
            for key in ("memory_percent", "memory_growth_rate_5m", "cpu_percent")
        }
        ordered = sorted(templates.values(), key=lambda t: t["count"], reverse=True)
        result = Explanation(
            container_id,
            window_minutes,
            now,
            errors,
            ordered[:10],
            [t for t in ordered if t["first_seen"] >= since],
            dict(signals),
            events,
            context,
            "none",
            "low",
            [],
        )
        result.verdict, result.confidence, result.reasons = _verdict(
            result, dependency_lines, stats
        )
        if self.root_cause:
            result.llm_summary, result.llm_status = self.root_cause.lookup(result)
        return result


def _verdict(report, dependency_lines, stats):
    """Evaluate the first matching heuristic, keeping the evidence that supports it."""
    deaths = [e for e in report.related_events if e["action"] == "die"]
    oom = [
        e
        for e in report.related_events
        if e["action"] == "oom"
        or (e["action"] == "die" and str(e["attributes"].get("exitCode")) == "137")
    ]
    memory = report.metric_context["memory_percent"]
    growth = report.metric_context["memory_growth_rate_5m"]
    signals = report.crash_signals

    def matches(patterns):
        return [f"{signals[p]} lines match '{p}'" for p in sorted(patterns) if signals.get(p)]

    support = matches(("Killed", "OutOfMemoryError"))
    if memory is not None and memory >= 90:
        support.append(f"Memory usage reached {memory:g}%")
    if growth is not None and growth > 5:
        support.append(f"Memory growth reached {growth:g}%/min")
    if oom and support:
        event = oom[0]
        label = "exitCode=137 die" if event["action"] == "die" else "oom"
        return (
            "oom_kill_confirmed",
            "high",
            [f"{label} event at {event['timestamp']:%H:%M:%S} UTC", *support],
        )
    if len(deaths) >= 3:
        return (
            "crash_loop",
            "high",
            [f"{len(deaths)} die events in {report.window_minutes} minutes"],
        )
    if deaths and any(
        e["action"] == "start" and e["timestamp"] > min(d["timestamp"] for d in deaths)
        for e in report.related_events
    ):
        return "crash_loop", "high", ["Docker recorded a start after a die event in this window"]
    if dependency_lines >= 5:
        return "dependency_failure", "medium", matches(DEPENDENCIES)
    exhausted = matches(("too many open files", "no space left"))
    if exhausted:
        return "resource_exhaustion", "medium", exhausted
    if stats.anomaly == "log_error_spike":
        return "error_spike", "medium", [stats.anomaly_detail]
    return (
        "none",
        "low",
        [
            f"No heuristic matched across {report.error_count} errors and {len(report.related_events)} Docker events"
        ],
    )
