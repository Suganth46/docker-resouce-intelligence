import asyncio
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from httpx import AsyncClient, ASGITransport
from pydantic import ValidationError

from dri.api import routes
from dri.api.server import DRIAPIServer, set_api_key
from dri.config import DRIConfig, LLMConfig, load_config
from dri.core.llm_client import LLMError, OllamaClient
from dri.core.log_analyzer import LogAnalyzer
from dri.core.root_cause import ModelAnswer, RootCauseService, build_prompt, grounded_summary
from dri.main import DRIAgent


def fixture_report(index=2):
    cases = json.loads((Path(__file__).parent / "fixtures/llm_cases.json").read_text())
    case = cases[index]
    for row in case["top_templates"]:
        row.update(template_id="masked-id", sample="password=hunter2")
    return SimpleNamespace(**dict(case, container_id="web"))


ANSWER = dict(
    root_cause="Dependency failure",
    confidence="medium",
    suggested_actions=["Check the upstream"],
    evidence_used=["connection refused"],
)


@pytest.fixture
async def fake():
    state = SimpleNamespace(
        names=["test:latest"], tags=0, chats=[], response=json.dumps(ANSWER), code=200, delay=0
    )

    state.active = state.max_active = 0

    async def tags(request):
        state.tags += 1
        return web.json_response({"models": [{"name": name} for name in state.names]})

    async def chat(request):
        state.chats.append(await request.json())
        state.active += 1
        state.max_active = max(state.active, state.max_active)
        try:
            await asyncio.sleep(state.delay)
        finally:
            state.active -= 1
        return web.json_response({"message": {"content": state.response}}, status=state.code)

    app = web.Application()
    app.router.add_get("/api/tags", tags)
    app.router.add_post("/api/chat", chat)
    async with TestServer(app) as server:
        state.url = str(server.make_url("/")).rstrip("/")
        yield state


def client_for(fake, **kwargs):
    return OllamaClient(fake.url, "test", kwargs.pop("timeout", 1), {}, **kwargs)


async def test_disabled(fake):
    cfg = LLMConfig(base_url=fake.url)
    service = RootCauseService(client_for(fake), None, cfg)
    assert service.enqueue(fixture_report()) == service.status() == "disabled"
    await service.run()
    assert (await LogAnalyzer().explain("web")).llm_status == "disabled"
    assert fake.tags == 0 and not fake.chats


def test_env_override(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("logs: {llm: {enabled: false, base_url: 'http://other', model: old}}")
    for key, value in dict(ENABLED="true", BASE_URL="http://example", MODEL="test").items():
        monkeypatch.setenv("DRI_LLM_" + key, value)
    cfg = load_config(str(path)).logs.llm
    assert cfg.enabled and cfg.base_url == "http://example" and cfg.model == "test"
    with pytest.raises(ValidationError):
        LLMConfig(provider="remote")


async def test_probe_and_request(fake):
    client = client_for(fake)
    try:
        assert await client.probe() == "ready"
        fake.names.clear()
        assert await client.probe() == "ready" and fake.tags == 1
        schema = ModelAnswer.model_json_schema()
        assert await client.generate_json("system", "user", schema) == ANSWER
        assert fake.chats[0] == dict(
            model="test",
            messages=[dict(role="system", content="system"), dict(role="user", content="user")],
            stream=False,
            format=schema,
            options=dict(temperature=0, seed=42, num_ctx=2048, num_predict=200),
            keep_alive="10m",
        )
    finally:
        await client.close()
    other = client_for(fake)
    try:
        assert await other.probe() == "model_missing"
    finally:
        await other.close()


async def test_unreachable(unused_tcp_port):
    client = OllamaClient(f"http://127.0.0.1:{unused_tcp_port}", "test", 1, {})
    try:
        assert await client.probe() == "unreachable"
    finally:
        await client.close()


@pytest.mark.parametrize("mode", ["invalid_json", "server_error", "timeout"])
async def test_breaker(fake, mode):
    client = client_for(fake, failure_threshold=2, timeout=0.08 if mode == "timeout" else 1)
    if mode == "invalid_json":
        fake.response = "{"
    elif mode == "timeout":
        fake.delay = 0.3
    else:
        fake.code = 500
    try:
        for _ in range(3):
            with pytest.raises(LLMError):
                await client.generate_json("system", "user", {})
        assert len(fake.chats) == 2
    finally:
        await client.close()


def test_prompt_and_guard():
    report = fixture_report()
    report.related_events = [{"action": "die", "attributes": {"token": "private-event"}}]
    system, user = build_prompt(report)
    assert "hunter2" not in user and "private-event" not in user and "masked-id" not in user
    assert len(build_prompt(report, 60)[1]) <= 60 and "oom" in system
    answer = ModelAnswer(
        **dict(ANSWER, root_cause="An OOM kill", evidence_used=["connection refused", "invented"])
    )
    summary = grounded_summary(answer, report, user, "test", 8)
    assert summary.evidence_used == ["connection refused"] and summary.confidence == "low"
    assert report.verdict == "dependency_failure" and summary.generated_at.tzinfo is None


async def test_queue_worker_and_cache(fake):
    config = LLMConfig(enabled=True, model="test", queue_size=1)
    client = client_for(fake)
    service = RootCauseService(client, None, config)
    first = fixture_report()
    second = fixture_report(0)
    second.container_id = "another"
    assert service.enqueue(first) == service.enqueue(first) == "pending"
    assert service.enqueue(second) == "unavailable" and service.jobs_dropped == 1
    fake.delay = 0.1
    worker = asyncio.create_task(service.run())
    try:

        async def observe():
            while not fake.chats:
                await asyncio.sleep(0.005)
            assert service.enqueue(second) == "pending"
            await service._queue.join()

        await asyncio.wait_for(observe(), 3)
        assert fake.max_active == 1
        assert service.jobs_done == len(fake.chats) == 2
        assert service.lookup(first)[0].model == "test" and service.enqueue(first) == "ready"
    finally:
        await service.close()
        await worker


async def test_slow_client_off_alert_path():
    client = AsyncMock()
    client.probe.return_value = "ready"
    began = asyncio.Event()

    async def slow(*args):
        began.set()
        await asyncio.sleep(30)

    client.generate_json.side_effect = slow
    agent = DRIAgent(DRIConfig())
    service = RootCauseService(client, None, LLMConfig(enabled=True))
    agent.root_cause = service
    agent.storage = AsyncMock()
    agent.log_analyzer = AsyncMock()
    agent.log_analyzer.explain.return_value = fixture_report()
    worker = asyncio.create_task(service.run())
    alert = SimpleNamespace(
        container_id="web", metric="cpu", alert_type="warning", current_value=80, message="high"
    )
    try:
        await asyncio.wait_for(agent._on_alert(alert), 0.5)
        await asyncio.wait_for(began.wait(), 0.5)
        other = fixture_report()
        other.container_id = "another"
        t = time.perf_counter()
        assert service.enqueue(other) == "pending"
        assert time.perf_counter() - t < 0.005
        agent.storage.save_alert.assert_awaited_once()
    finally:
        await service.close()
        await worker


async def test_api(fake, monkeypatch):
    cfg = DRIConfig()
    cfg.api.api_key = "local-test"
    service = RootCauseService(
        client_for(fake), None, LLMConfig(enabled=True, model="test", base_url=fake.url)
    )
    analyzer = LogAnalyzer(root_cause=service)
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
    server = DRIAPIServer(cfg, AsyncMock(), None, None, log_analyzer=analyzer)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=server.app), base_url="http://test"
        ) as http:
            url = "/api/v1/containers/web/explain"
            assert (await http.post(url + "/ai")).status_code == 401
            assert (await http.get("/api/v1/llm/status")).status_code == 401
            http.headers["X-Api-Key"] = "local-test"
            data = (await http.get(url)).json()
            assert data["llm_summary"] is None and "llm_status" in data
            assert (await http.post(url + "/ai")).status_code == 202
            assert (await http.post(url + "/ai")).status_code == 429
            assert (await http.get("/api/v1/llm/status")).json()["base_url_host"] == "127.0.0.1"
            analyzer.root_cause = None
            assert (await http.post(url + "/ai")).status_code == 503
    finally:
        set_api_key(None)
        await service.close()


@pytest.mark.ollama
@pytest.mark.skipif(
    not os.getenv("OLLAMA_TEST_URL"), reason="Set OLLAMA_TEST_URL for real inference"
)
async def test_local_inference():
    client = OllamaClient(
        os.environ["OLLAMA_TEST_URL"], os.getenv("DRI_LLM_MODEL", LLMConfig().model), 60, {}
    )
    try:
        result = await client.generate_json(
            *build_prompt(fixture_report()), ModelAnswer.model_json_schema()
        )
        ModelAnswer.model_validate(result)
    finally:
        await client.close()
