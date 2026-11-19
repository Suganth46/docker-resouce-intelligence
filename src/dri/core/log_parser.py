"""Parse and sanitize Docker timestamped output without I/O."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Optional

DEFAULT_ERRORS = ("ERROR", "FATAL", "CRITICAL", "Exception", "Traceback", "panic")
DEFAULT_CRASH = (
    "OutOfMemoryError",
    "Killed",
    "connection refused",
    "timed out",
    "too many open files",
    "ECONNREFUSED",
    "no space left",
)


def compile_errors(patterns):
    return tuple(
        re.compile(r"\bERROR\b" if pattern.upper() == "ERROR" else pattern, re.I)
        for pattern in patterns
    )


ERROR_REGEX = compile_errors(DEFAULT_ERRORS)
LEVEL = re.compile(r"\b(ERROR|WARN(?:ING)?|INFO|DEBUG)\b", re.I)
STAMP = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d) ")
EMAIL = re.compile(r"[\w.+%-]+@[\w.-]+\.[\w-]+")
BEARER = re.compile(r"\bBearer\s+[^\s,;\"']+", re.I)
JWT = re.compile(r"\beyJ[\w-]*\.[\w-]+\.[\w-]+")
AWS = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
SECRET = re.compile(
    r"""(?i)\b(password|passwd|secret|token|api[_-]?key)["']?\s*[:=]\s*(?:"[^"]*(?:"|$)|'[^']*(?:'|$)|Bearer\s+\S+|[^\s,;&}]+)"""
)


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_docker_line(raw: bytes | str) -> Optional[tuple[datetime, str]]:
    line = (raw.decode(errors="replace") if isinstance(raw, bytes) else raw).rstrip("\r\n")
    match = STAMP.match(line)
    if match:
        date, fraction, offset = match.groups()
        try:
            stamp = datetime.fromisoformat(
                date + (fraction or "")[:7] + offset.replace("Z", "+00:00")
            )
            return stamp.astimezone(timezone.utc).replace(tzinfo=None), line[match.end() :]
        except ValueError:
            pass
    return (utcnow(), line) if line else None


def detect_level(message: str, error_patterns=ERROR_REGEX) -> str:
    if any(pattern.search(message) for pattern in error_patterns):
        return "ERROR"
    if message.lstrip().startswith("{"):
        try:
            record = json.loads(message)
            level = str(record.get("level") or record.get("severity") or "")
            match = LEVEL.fullmatch(level)
            if match:
                return match[1].upper().replace("WARNING", "WARN")
        except (ValueError, AttributeError, RecursionError):
            pass
    match = LEVEL.search(message)
    return match[1].upper().replace("WARNING", "WARN") if match else "UNKNOWN"


def mask_sensitive(message: str) -> str:
    message = SECRET.sub(r"\1=<redacted>", message)
    message = BEARER.sub("Bearer <redacted>", message)
    return EMAIL.sub("<email>", AWS.sub("<redacted>", JWT.sub("<redacted>", message)))


def truncate(message: str, max_len: int) -> str:
    return message if len(message) <= max_len else message[:max_len] + " …[truncated]"


class MultilineJoiner:
    def __init__(self):
        self.lines = []
        self.first = self.last = None

    def push(self, ts, msg):
        continuation = msg[:1].isspace() or msg.startswith(
            ("Traceback", "at ", "Caused by", "...", 'File "')
        )
        # Python tracebacks end with an unindented exception type and message.
        if self.lines and any("Traceback" in line for line in self.lines):
            continuation |= bool(re.match(r"^[\w.]+(?:Error|Exception):", msg))
        completed = []
        if self.lines and not (
            continuation and len(self.lines) < 20 and 0 <= (ts - self.last).total_seconds() <= 1
        ):
            completed = self.flush()
        if not self.lines:
            self.first = ts
        self.last = ts
        self.lines.append(msg)
        return completed

    def flush(self):
        events = [(self.first, "\n".join(self.lines))] if self.lines else []
        self.lines.clear()
        return events
