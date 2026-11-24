"""Async client for a single Ollama server and model."""

import asyncio
import json
import time
from typing import Literal

import aiohttp

LLMStatus = Literal["ready", "model_missing", "unreachable"]


class LLMError(Exception):
    """Inference or model server failure."""


class OllamaClient:
    def __init__(
        self,
        base_url,
        model,
        timeout,
        options,
        *,
        probe_interval_seconds=30,
        failure_threshold=3,
        cooldown_seconds=60,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.options = {
            "temperature": 0,
            "seed": 42,
            "num_ctx": 2048,
            "num_predict": 200,
            **options,
        }
        self.probe_interval_seconds = probe_interval_seconds
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._session = None
        self._probe_status = "unreachable"
        self._probe_until = self._open_until = 0.0
        self._failures = {"probe": 0, "chat": 0}

    async def _request(self, method, path, payload=None):
        if time.monotonic() < self._open_until:
            raise LLMError("Ollama circuit is open")
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        async with self._session.request(
            method, self.base_url + path, json=payload, allow_redirects=False
        ) as response:
            if 300 <= response.status < 400:
                raise ValueError("Unexpected Ollama redirect")
            response.raise_for_status()
            return await response.json()

    def _failed(self, operation):
        self._failures[operation] += 1
        if self._failures[operation] >= self.failure_threshold:
            self._open_until = time.monotonic() + self.cooldown_seconds
            self._failures[operation] = 0

    async def probe(self) -> LLMStatus:
        now = time.monotonic()
        if now < self._open_until:
            return "unreachable"
        if now < self._probe_until:
            return self._probe_status
        try:
            data = await self._request("GET", "/api/tags")
            name = self.model if ":" in self.model.split("/")[-1] else self.model + ":latest"
            names = {item["name"] for item in data["models"]}
            self._failures["probe"] = 0
            self._probe_status = "ready" if {name, self.model} & names else "model_missing"
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError, KeyError):
            self._failed("probe")
            self._probe_status = "unreachable"
        self._probe_until = now + self.probe_interval_seconds
        return self._probe_status

    async def generate_json(self, system: str, user: str, schema: dict) -> dict:
        payload = dict(
            model=self.model,
            stream=False,
            format=schema,
            keep_alive="10m",
            options=self.options,
            messages=[dict(role="system", content=system), dict(role="user", content=user)],
        )
        try:
            data = await self._request("POST", "/api/chat", payload)
            result = json.loads(data["message"]["content"])
            if not isinstance(result, dict):
                raise ValueError("Expected a JSON object")
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError, KeyError) as exc:
            self._failed("chat")
            raise LLMError(f"Ollama chat failed ({type(exc).__name__})") from exc
        self._failures["chat"] = 0
        return result

    async def close(self):
        if self._session is not None:
            await self._session.close()
            self._session = None
