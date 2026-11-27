"""Throttling actions for reducing CPU and memory."""

import asyncio
from typing import Optional, Dict, Any
import docker

from ..config import ThrottleConfig, throttle_resource


class _UnsafeThrottle(ValueError):
    """A pre-update check rejected the change before any Docker mutation."""


class ThrottleAction:
    """Apply CPU and memory throttling to containers."""

    _fields = {
        "cpu_shares": "CpuShares",
        "cpu_quota": "CpuQuota",
        "mem_limit": "Memory",
        "mem_reservation": "MemoryReservation",
    }

    def __init__(self, docker_client: docker.DockerClient, *, storage=None):
        self.client = docker_client
        self.storage = storage
        self.pending = {}
        self._lock = asyncio.Lock()

    async def initialize(self):
        if self.storage is not None:
            self.pending = await self.storage.get_pending_throttles()

    def _plan(self, cid, config, resource):
        container = self.client.containers.get(cid)
        host = container.attrs["HostConfig"]
        requested = {}
        if resource in ("cpu", "all"):
            requested.update(cpu_shares=config.cpu_shares, cpu_quota=config.cpu_quota)
        if resource in ("memory", "all"):
            for key, value in (
                ("mem_limit", config.memory_limit),
                ("mem_reservation", config.memory_reservation),
            ):
                if value is not None:
                    requested[key] = self._parse_memory_limit(value)

        previous, changes = {}, {}
        for key, target in requested.items():
            if target is None:
                continue
            old = host[self._fields[key]]
            # Docker ignores zero in a live update. Only CPU quota has an
            # explicit reset value (-1) equivalent to the unset quota.
            if target <= 0 or (old == 0 and key != "cpu_quota"):
                raise ValueError(f"{key} needs positive target and a restorable baseline")
            if old > 0 and target >= old:
                continue
            changes[key] = target
            previous[key] = old
        if not changes:
            raise ValueError("No tighter throttle parameters specified for this resource")
        return dict(container_id=container.id, parameters=changes, previous=previous)

    def _update(self, container, params):
        container.update(**params)
        container.reload()
        host = container.attrs["HostConfig"]
        for key, target in params.items():
            value = host[self._fields[key]]
            if value != target and not (key == "cpu_quota" and target == -1 and value == 0):
                raise RuntimeError(f"Docker did not apply {key}")

    def _apply(self, snapshot):
        container = self.client.containers.get(snapshot["container_id"])
        host = container.attrs["HostConfig"]
        params = {}
        for key, old in snapshot["previous"].items():
            current = host[self._fields[key]]
            target = snapshot["parameters"][key]
            if current == target:
                continue
            if current != old and not (key == "cpu_quota" and current == 0 and old == -1):
                raise _UnsafeThrottle("Container limits changed before throttle")
            params[key] = target
        if not params:
            return False
        if "mem_limit" in params:
            usage = container.stats(stream=False)["memory_stats"]["usage"]
            if params["mem_limit"] < usage:
                raise _UnsafeThrottle("Requested memory limit is below current usage")
        self._update(container, params)
        return True

    async def throttle(
        self, container_id: str, config: ThrottleConfig, *, metric: Optional[str] = None
    ) -> Dict[str, Any]:
        resource = "all" if metric is None else throttle_resource(metric)
        if resource is None:
            return {"success": False, "error": f"No throttle resource for {metric}"}
        key = (container_id, resource)
        async with self._lock:
            new_snapshot = key not in self.pending
            try:
                if new_snapshot:
                    snapshot = await asyncio.to_thread(self._plan, container_id, config, resource)
                    if self.storage is not None:
                        await self.storage.set_pending_throttle(*key, snapshot)
                    self.pending[key] = snapshot
                else:
                    snapshot = self.pending[key]
                try:
                    changed = await asyncio.to_thread(self._apply, snapshot)
                except _UnsafeThrottle:
                    if new_snapshot:
                        await self._forget(key)
                    raise
                return {"success": True, "already_applied": not changed, **snapshot}
            except docker.errors.NotFound:
                return {"success": False, "error": f"Container not found: {container_id}"}
            except Exception as exc:
                return {"success": False, "error": str(exc)}

    async def _forget(self, key):
        if self.storage is not None:
            await self.storage.set_pending_throttle(*key, None)
        del self.pending[key]

    def _restore(self, snapshot):
        try:
            container = self.client.containers.get(snapshot["container_id"])
        except docker.errors.NotFound:
            return {}
        host = container.attrs["HostConfig"]
        # Leave operator changes in place. This also handles an interrupted
        # apply or a retry after Docker restored the limits.
        restore = {
            key: snapshot["previous"][key]
            for key, applied in snapshot["parameters"].items()
            if host[self._fields[key]] == applied
        }
        if restore:
            if restore.get("cpu_quota") == 0:
                restore["cpu_quota"] = -1  # Docker's live-update value for unlimited.
            self._update(container, restore)
        return restore

    async def restore(self, container_id, resource):
        key = (container_id, resource)
        async with self._lock:
            snapshot = self.pending[key]
            try:
                restored = await asyncio.to_thread(self._restore, snapshot)
                await self._forget(key)
                return {"success": True, **snapshot, "restored": restored}
            except Exception as exc:
                return {"success": False, **snapshot, "error": str(exc)}

    @staticmethod
    def _parse_memory_limit(limit: str) -> int:
        """
        Parse memory limit string to bytes.

        Examples:
            "512m" -> 512 * 1024 * 1024
            "1g" -> 1024 * 1024 * 1024
        """
        units = {
            "k": 1024,
            "m": 1024**2,
            "g": 1024**3,
        }

        limit = str(limit).lower().strip()
        for unit, multiplier in units.items():
            if limit.endswith(unit):
                try:
                    value = int(limit[:-1])
                    return value * multiplier
                except ValueError:
                    pass

        # Try to parse as plain integer (bytes)
        try:
            return int(limit)
        except ValueError:
            raise ValueError(f"Invalid memory limit: {limit}")
