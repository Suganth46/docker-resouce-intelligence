"""Follow Docker logs on dedicated daemon threads.

A quiet stream may block in the Docker SDK after stop until another line
arrives or Docker closes the connection. Late callbacks cannot write to storage.
"""

import asyncio
import logging
import re
import threading
import time
from collections import Counter
from datetime import timezone
from typing import Callable, Optional

from docker.errors import APIError
from ..config import LogConfig
from .log_parser import (
    MultilineJoiner,
    compile_errors,
    detect_level,
    mask_sensitive,
    parse_docker_line,
    truncate,
    utcnow,
)
from .storage_base import StorageBackend

logger = logging.getLogger(__name__)


class DockerLogCollector:
    def __init__(
        self,
        docker_client,
        config: LogConfig,
        storage: StorageBackend,
        analyzer_callback: Optional[Callable] = None,
    ):
        self.client = docker_client
        self.config = config
        self.storage = storage
        self.callback = analyzer_callback
        self.errors = compile_errors(config.error_patterns)
        self.crashes = [re.compile(p, re.I) for p in config.crash_patterns]
        capacity = max(1024, config.flush_batch_size)
        self.queue = asyncio.Queue(maxsize=capacity)
        # Bound both queued lines and callbacks waiting on the event loop.
        self.handoff = threading.Lock()
        self.slots = threading.BoundedSemaphore(capacity)
        self.threads = {}
        self.unsupported = set()
        self.cursors = {}
        self.joiners = {}
        self.last_arrival = {}
        self.names = {}
        self.tokens = {}
        self.drops = Counter()
        self.batch = []
        self.lock = asyncio.Lock()
        self.stopped = asyncio.Event()
        self.finished = asyncio.Event()
        self._started = False
        self._running = False
        self.lines_ingested = self.lines_dropped = 0

    def collection_state(self, cid):
        if cid in self.unsupported:
            return "unsupported_driver"
        thread = self.threads.get(cid)
        return "following" if thread and thread.is_alive() else "not_following"

    @property
    def containers_followed(self):
        return sum(thread.is_alive() for thread in self.threads.values())

    def _enqueue(self, cid, name, line):
        if not self.finished.is_set():
            try:
                self.queue.put_nowait((cid, name, line))
                return
            except asyncio.QueueFull:
                self.lines_dropped += 1
        self.slots.release()

    def _follow(self, container, cid, loop):
        # Docker SDK converts since to integer seconds. Skip the exact number
        # of previously delivered lines in the final second on reconnect.
        resume = self.cursors.get(cid)
        second, skip = resume or (int(time.time()), 0)
        try:
            stream = container.logs(stream=True, follow=True, timestamps=True, since=second)
            for raw in stream:
                if not self._running:
                    break
                parsed = parse_docker_line(raw)
                if parsed is None:
                    continue
                stamp = int(parsed[0].replace(tzinfo=timezone.utc).timestamp())
                if resume and stamp < second:
                    continue
                if stamp == second and skip:
                    skip -= 1
                    continue
                while self._running:
                    if self.slots.acquire(timeout=0.2):
                        with self.handoff:
                            if not self._running:
                                self.slots.release()
                                return
                            loop.call_soon_threadsafe(self._enqueue, cid, container.name, raw)
                        previous, count = self.cursors.get(cid, (stamp, 0))
                        self.cursors[cid] = (stamp, count + 1 if previous == stamp else 1)
                        break
                else:
                    break
        except APIError as exc:
            if "logging driver does not support reading" in str(exc).lower():
                self.unsupported.add(cid)
                logger.warning("Log driver for %s does not support reading", cid)
            else:
                logger.warning("Docker log stream failed for %s", cid)
        except Exception:
            if self._running:
                logger.warning("Lost Docker log stream for %s", cid)

    async def _discover_loop(self):
        loop = asyncio.get_running_loop()
        while self._running:
            try:
                containers = await loop.run_in_executor(None, self.client.containers.list)
                self.threads = {cid: t for cid, t in self.threads.items() if t.is_alive()}
                for container in containers:
                    if (
                        not self._running
                        or len(self.threads) >= self.config.max_followed_containers
                    ):
                        break
                    cid = container.id[:12]
                    if cid in self.threads or cid in self.unsupported:
                        continue
                    thread = threading.Thread(
                        target=self._follow, args=(container, cid, loop), daemon=True
                    )
                    self.threads[cid] = thread
                    thread.start()
            except Exception:
                logger.exception("Could not discover container log streams")
            await self._wait(self.config.discover_interval_seconds)

    async def _wait(self, seconds):
        try:
            await asyncio.wait_for(self.stopped.wait(), seconds)
        except asyncio.TimeoutError:
            pass

    def _record(self, cid, ts, level, message):
        entry = dict(
            container_id=cid,
            container_name=self.names[cid],
            timestamp=ts,
            level=level,
            message=message,
        )
        if self.callback:
            self.callback(entry)
        self.batch.append(entry)

    def _event(self, cid, ts, message):
        # Mask before truncation too: cutting a credential in half can defeat
        # recognition. Classify the complete message so a late ERROR survives.
        level = detect_level(message, self.errors)
        protected = level in ("WARN", "ERROR") or any(p.search(message) for p in self.crashes)
        now = time.monotonic()
        rate = self.config.max_lines_per_second
        remaining, previous = self.tokens.get(cid, (rate, now))
        remaining = min(rate, remaining + (now - previous) * rate)
        if remaining < 1 and not protected:
            self.drops[cid] += 1
            self.lines_dropped += 1
        else:
            remaining = max(0, remaining - 1)
            self._record(
                cid, ts, level, truncate(mask_sensitive(message), self.config.max_line_length)
            )
            self.lines_ingested += 1
        self.tokens[cid] = remaining, now

    def _consume(self, item):
        self.slots.release()
        cid, name, raw = item
        parsed = parse_docker_line(raw)
        if parsed:
            self.names[cid] = name
            if cid not in self.joiners:
                self.joiners[cid] = MultilineJoiner()
            for ts, msg in self.joiners[cid].push(*parsed):
                self._event(cid, ts, msg)
            self.last_arrival[cid] = time.monotonic()

    async def _consume_loop(self):
        while True:
            item = await self.queue.get()
            if item is None:
                return
            self._consume(item)
            if len(self.batch) >= self.config.flush_batch_size:
                await self._save()

    def _finish_events(self, now):
        for cid in list(self.joiners):
            if now - self.last_arrival[cid] >= 1:
                for ts, msg in self.joiners.pop(cid).flush():
                    self._event(cid, ts, msg)
                del self.last_arrival[cid]

    def _report_drops(self):
        for cid, count in self.drops.items():
            self._record(cid, utcnow(), "WARN", f"DRI dropped {count} log lines (rate limit)")
        self.drops.clear()

    async def _save(self):
        async with self.lock:
            if self.batch:
                batch, self.batch = self.batch, []
                await self.storage.save_logs(batch)

    async def _flush_loop(self):
        report = time.monotonic() + 60
        cleanup = time.monotonic()
        while self._running:
            now = time.monotonic()
            self._finish_events(now)
            if now >= report:
                self._report_drops()
                report = now + 60
            await self._save()
            if now >= cleanup:
                try:
                    await self.storage.cleanup_old_logs(self.config.retention_hours)
                except Exception:
                    logger.exception("Failed to expire container logs")
                cleanup = now + 3600
            await self._wait(min(self.config.flush_interval_seconds, max(0, report - now)))

    async def run(self):
        if self.stopped.is_set():
            return
        self._started = True
        self._running = True
        consumer = asyncio.create_task(self._consume_loop())
        try:
            await asyncio.gather(self._discover_loop(), self._flush_loop())
        finally:
            self.stop()
            try:
                await asyncio.sleep(0)  # Deliver thread callbacks scheduled before stop.
                if not consumer.done():
                    await self.queue.put(None)
                await consumer
            finally:
                self.finished.set()

    def stop(self):
        with self.handoff:
            self._running = False
        self.stopped.set()

    async def close(self):
        self.stop()
        if self._started:
            await self.finished.wait()
        await asyncio.sleep(0)  # Drain callbacks accepted before stop acquired handoff.
        while not self.queue.empty():
            self._consume(self.queue.get_nowait())
        self._finish_events(float("inf"))
        self._report_drops()
        await self._save()
