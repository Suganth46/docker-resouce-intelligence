"""
Tests for action handlers: throttle, isolate, heal (graceful_restart).

All Docker interactions are mocked — no running Docker daemon required.
Covers:
  - Successful action -> success=True
  - Logger kwargs TypeError regression (P0-1)
  - graceful_restart: stop() called before start() (P0-1)
  - _handle_alert_action dispatches restart alerts (P0-2)
  - Failed actions are recorded in storage (P0-2)
  - Per-container/rule cooldown prevents repeat firing (P0-2)
"""

import asyncio

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch, call


def _make_docker_client(container):
    client = MagicMock()
    client.containers.get = MagicMock(return_value=container)
    return client


def _make_container(name="test-container"):
    c = MagicMock()
    c.name = name
    c.status = "running"
    c.id = "abc123def456"
    c.attrs = {"HostConfig": {"CpuShares": 1024, "CpuQuota": 0, "Memory": 2**30,
                              "MemoryReservation": 512 * 2**20, "MemorySwap": 2**31}}
    c.stats.return_value = {"memory_stats": {"usage": 128 * 2**20}}
    def update(**params):
        from dri.actions.throttle import ThrottleAction
        for key, value in params.items():
            if key in ThrottleAction._fields:
                c.attrs["HostConfig"][ThrottleAction._fields[key]] = value
    c.update = MagicMock(side_effect=update)
    c.stop = MagicMock(return_value=None)
    c.start = MagicMock(return_value=None)
    return c


def _make_alert(container_id="abc123", container_name="test-container",
                alert_type="throttle", metric="memory"):
    a = MagicMock()
    a.alert_id = "alert-001"
    a.container_id = container_id
    a.container_name = container_name
    a.alert_type = alert_type
    a.metric = metric
    a.message = f"{metric} threshold breached"
    a.to_dict = MagicMock(return_value={"container_id": container_id})
    return a


def _make_agent():
    from dri.main import DRIAgent
    from dri.config import DRIConfig
    config = DRIConfig()
    agent = DRIAgent(config)
    agent.storage = AsyncMock()
    agent.storage.save_action = AsyncMock()
    agent.throttle_action = AsyncMock()
    agent.throttle_action.throttle = AsyncMock(return_value={"success": True, "parameters": {}})
    agent.isolate_action = AsyncMock()
    agent.isolate_action.isolate = AsyncMock(return_value={"success": True, "cpuset": "0-1"})
    agent.heal_action = AsyncMock()
    agent.heal_action.graceful_restart = AsyncMock(
        return_value={"success": True, "action": "graceful_restart", "reason": "policy"}
    )
    agent.heal_action.trigger_webhook = AsyncMock(return_value=True)
    return agent


# --- ThrottleAction ---

@pytest.mark.asyncio
async def test_throttle_success():
    from dri.actions.throttle import ThrottleAction
    from dri.config import ThrottleConfig
    container = _make_container()
    action = ThrottleAction(_make_docker_client(container))
    result = await action.throttle("abc", ThrottleConfig(cpu_shares=512, memory_limit="512m"))
    assert result["success"] is True
    container.update.assert_called_once_with(
        cpu_shares=512, mem_limit=536870912
    )


@pytest.mark.asyncio
async def test_throttle_not_found():
    import docker
    from dri.actions.throttle import ThrottleAction
    from dri.config import ThrottleConfig
    client = MagicMock()
    client.containers.get = MagicMock(side_effect=docker.errors.NotFound("x"))
    result = await ThrottleAction(client).throttle("abc", ThrottleConfig(cpu_shares=512, memory_limit="512m"))
    assert result["success"] is False
    assert "not found" in result["error"].lower()


@pytest.mark.asyncio
async def test_throttle_empty_config():
    from dri.actions.throttle import ThrottleAction
    from dri.config import ThrottleConfig
    container = _make_container()
    result = await ThrottleAction(_make_docker_client(container)).throttle("abc123", ThrottleConfig())
    assert result["success"] is False
    container.update.assert_not_called()


@pytest.mark.asyncio
async def test_throttle_memory_reservation_uses_correct_docker_key():
    """P0-2: memory_reservation must be passed as mem_reservation to container.update(),
    not as 'memory_reservation' (which docker-py ignores / rejects).
    The memory limit is independent of swap.
    """
    from dri.actions.throttle import ThrottleAction
    from dri.config import ThrottleConfig
    container = _make_container()
    result = await ThrottleAction(_make_docker_client(container)).throttle(
        "abc123", ThrottleConfig(memory_limit="512m", memory_reservation="256m")
    )
    assert result["success"] is True
    call_kwargs = container.update.call_args[1]
    # docker-py keys
    assert "mem_limit" in call_kwargs, "mem_limit not passed to container.update()"
    assert "memswap_limit" not in call_kwargs
    assert "mem_reservation" in call_kwargs, "mem_reservation not passed (got memory_reservation?)"
    assert "memory_reservation" not in call_kwargs, "wrong key 'memory_reservation' passed to container.update()"
    assert call_kwargs["mem_limit"] == 512 * 1024 * 1024
    assert call_kwargs["mem_reservation"] == 256 * 1024 * 1024


@pytest.mark.asyncio
async def test_throttle_no_typeerror_from_logger():
    """P0-1: logger kwargs must not raise TypeError."""
    from dri.actions.throttle import ThrottleAction
    from dri.config import ThrottleConfig
    container = _make_container()
    result = await ThrottleAction(_make_docker_client(container)).throttle(
        "abc123", ThrottleConfig(cpu_shares=256)
    )
    assert result["success"] is True


# --- IsolateAction ---

@pytest.mark.asyncio
async def test_isolate_success():
    from dri.actions.isolate import IsolateAction
    container = _make_container()
    result = await IsolateAction(_make_docker_client(container)).isolate("abc", "0-1")
    assert result["success"] is True
    container.update.assert_called_once_with(cpuset_cpus="0-1")


@pytest.mark.asyncio
async def test_isolate_not_found():
    import docker
    from dri.actions.isolate import IsolateAction
    client = MagicMock()
    client.containers.get = MagicMock(side_effect=docker.errors.NotFound("x"))
    result = await IsolateAction(client).isolate("missing", "0")
    assert result["success"] is False


@pytest.mark.asyncio
async def test_isolate_no_typeerror_from_logger():
    """P0-1: logger kwargs must not raise TypeError."""
    from dri.actions.isolate import IsolateAction
    container = _make_container()
    result = await IsolateAction(_make_docker_client(container)).isolate("abc123", "0")
    assert result["success"] is True


# --- HealAction.graceful_restart ---

@pytest.mark.asyncio
async def test_graceful_restart_success_stop_before_start():
    """P0-1: stop() must be called BEFORE start(), and result must be success=True."""
    from dri.actions.heal import HealAction
    container = _make_container()
    with patch("dri.actions.heal.time.sleep"):
        result = await HealAction(_make_docker_client(container)).graceful_restart("abc")
    assert result["success"] is True
    container.stop.assert_called_with(timeout=30)
    container.start.assert_called_once()


@pytest.mark.asyncio
async def test_graceful_restart_no_typeerror_from_logger():
    """P0-1 regression: logger inside _restart() must not abort the restart."""
    from dri.actions.heal import HealAction
    container = _make_container()
    with patch("dri.actions.heal.time.sleep"):
        result = await HealAction(_make_docker_client(container)).graceful_restart("abc123")
    assert result["success"] is True
    container.stop.assert_called_once()
    container.start.assert_called_once()


@pytest.mark.asyncio
async def test_graceful_restart_not_found():
    import docker
    from dri.actions.heal import HealAction
    client = MagicMock()
    client.containers.get = MagicMock(side_effect=docker.errors.NotFound("gone"))
    with patch("dri.actions.heal.time.sleep"):
        result = await HealAction(client).graceful_restart("gone")
    assert result["success"] is False


# --- DRIAgent._handle_alert_action ---

@pytest.mark.asyncio
async def test_alert_throttle_saved_to_storage():
    agent = _make_agent()
    await agent._handle_alert_action(_make_alert(alert_type="throttle"))
    agent.throttle_action.throttle.assert_called_once()
    agent.storage.save_action.assert_called_once()
    assert agent.storage.save_action.call_args[0][0].action_type == "throttle"
    assert agent.actions_executed == 1


@pytest.mark.asyncio
async def test_alert_isolate_saved_to_storage():
    agent = _make_agent()
    # isolate_cpuset must be configured or the branch guard skips the action
    agent.config.actions.isolate_cpuset = "0-1"
    await agent._handle_alert_action(_make_alert(alert_type="isolate", metric="cpu"))
    agent.isolate_action.isolate.assert_called_once()
    saved = agent.storage.save_action.call_args[0][0]
    assert saved.action_type == "isolate"


@pytest.mark.asyncio
async def test_alert_restart_dispatched():
    """P0-2: restart alert_type must call graceful_restart (was a no-op before)."""
    agent = _make_agent()
    alert = _make_alert(alert_type="restart")
    await agent._handle_alert_action(alert)
    agent.heal_action.graceful_restart.assert_called_once_with(
        alert.container_id,
        stop_timeout=agent.config.actions.restart_grace_period,
        reason=alert.message,
    )
    saved = agent.storage.save_action.call_args[0][0]
    assert saved.action_type == "restart"
    assert saved.success is True


@pytest.mark.asyncio
async def test_failed_action_still_recorded():
    """P0-2: failed actions must be recorded, not silently dropped."""
    agent = _make_agent()
    agent.throttle_action.throttle = AsyncMock(
        return_value={"success": False, "error": "Container not found"}
    )
    await agent._handle_alert_action(_make_alert(alert_type="throttle"))
    agent.storage.save_action.assert_called_once()
    saved = agent.storage.save_action.call_args[0][0]
    assert saved.success is False
    assert saved.error_message is not None
    assert agent.actions_executed == 0


@pytest.mark.asyncio
async def test_cooldown_prevents_repeat():
    """P0-2: same (container, type, metric) within cooldown must be suppressed."""
    agent = _make_agent()
    alert = _make_alert(alert_type="throttle")
    await agent._handle_alert_action(alert)
    await agent._handle_alert_action(alert)
    assert agent.throttle_action.throttle.call_count == 1
    assert agent.storage.save_action.call_count == 1


@pytest.mark.asyncio
async def test_cooldown_expired_allows_repeat():
    """Expired cooldown allows the action to fire again."""
    agent = _make_agent()
    alert = _make_alert(alert_type="throttle")
    await agent._handle_alert_action(alert)
    key = (alert.container_id, alert.alert_type, alert.metric)
    agent._action_cooldowns[key] = datetime.now(timezone.utc) - timedelta(
        seconds=agent._ACTION_COOLDOWN_SECONDS + 1
    )
    await agent._handle_alert_action(alert)
    assert agent.throttle_action.throttle.call_count == 2


@pytest.mark.asyncio
async def test_warning_alert_does_nothing():
    """warning alerts must not trigger any Docker action."""
    agent = _make_agent()
    await agent._handle_alert_action(_make_alert(alert_type="warning"))
    agent.throttle_action.throttle.assert_not_called()
    agent.isolate_action.isolate.assert_not_called()
    agent.heal_action.graceful_restart.assert_not_called()
    agent.storage.save_action.assert_not_called()


# --- P0-3: predict_oom wired into _policy_check_loop ---

async def _run_one_policy_tick(agent):
    ticks = 0

    async def interval(_):
        nonlocal ticks
        if ticks:
            await asyncio.gather(*agent._policy_tasks.values())
            agent.running = False
        ticks += 1

    agent.running = True
    with patch("asyncio.sleep", side_effect=interval):
        await agent._policy_check_loop()


@pytest.mark.asyncio
async def test_policy_check_loop_calls_predict_oom_and_emits_warning_alert():
    """P0-3: _policy_check_loop must call heal_action.predict_oom for each container
    and emit a 'warning' alert (via _on_alert) when risk_level is 'critical'.
    """
    from dri.main import DRIAgent
    from dri.config import DRIConfig
    from dri.utils.metrics import AggregatedMetrics
    from datetime import datetime, timezone

    config = DRIConfig()
    agent = DRIAgent(config)
    agent.storage = AsyncMock()
    agent.storage.save_alert = AsyncMock()

    critical_metrics = AggregatedMetrics(
        container_id="crit001",
        container_name="crit-container",
        timestamp=datetime.now(timezone.utc),
        cpu_percent=10.0, cpu_percent_5m=10.0, cpu_percent_15m=10.0,
        cpu_percent_60m=10.0,
        memory_percent=97.0,  # > 95 -> critical
        memory_percent_5m=96.0, memory_percent_15m=95.0, memory_percent_60m=80.0,
        memory_growth_rate_1m=0.0, memory_growth_rate_5m=0.0,
    )

    mock_aggregator = AsyncMock()
    mock_aggregator.is_container_stopped = MagicMock(return_value=False)
    mock_aggregator.aggregate_all = AsyncMock(return_value={"crit001": critical_metrics})
    agent.aggregator = mock_aggregator

    mock_engine = AsyncMock()
    mock_engine.evaluate = AsyncMock(return_value=None)
    agent.policy_engine = mock_engine

    agent.heal_action = AsyncMock()
    agent.heal_action.predict_oom = AsyncMock(
        return_value={
            "container_id": "crit001",
            "container_name": "crit-container",
            "risk_level": "critical",
            "reason": "Memory at 97.0% of limit",
            "memory_percent": 97.0,
            "growth_rate_5m": 0.0,
        }
    )

    alerts_received = []

    async def fake_on_alert(alert):
        alerts_received.append(alert)
        if agent.storage:
            await agent.storage.save_alert(alert)

    agent._on_alert = fake_on_alert

    await _run_one_policy_tick(agent)

    # predict_oom MUST have been called by _policy_check_loop
    agent.heal_action.predict_oom.assert_called_once_with(critical_metrics)
    # And a warning alert MUST have been emitted
    assert len(alerts_received) >= 1, "_on_alert was never called with a warning"
    assert any(a.alert_type == "warning" for a in alerts_received)
    assert any(a.container_id == "crit001" for a in alerts_received)



@pytest.mark.asyncio
async def test_shipped_policy_only_warns():
    from dri.config import load_config
    from dri.core.engine import PolicyEngine
    from tests.test_engine_dedup import _make_agg_metrics

    engine = PolicyEngine(load_config("config/config.yaml"))
    metrics = _make_agg_metrics(memory_percent=99, growth_rate_5m=25)
    metrics.cpu_percent = 80
    start = datetime.now(timezone.utc)
    with patch("dri.core.engine.datetime") as clock:
        clock.now.return_value = start
        await engine.evaluate(metrics)
        clock.now.return_value += timedelta(seconds=61)
        alerts = await engine.evaluate(metrics)
    assert len(alerts) == 4
    assert all(alert.alert_type == "warning" for alert in alerts)


@pytest.mark.asyncio
@pytest.mark.parametrize("existing,usage", [
    (256, 64),  # The requested 512 MiB would raise the existing limit.
    (1024, 600),  # The running workload already uses more than the target.
    (0, 64),  # Docker cannot restore an unset hard limit with update(memory=0).
])
async def test_unsafe_memory_target_skips_update(existing, usage):
    from dri.actions.throttle import ThrottleAction
    from dri.config import ThrottleConfig

    container = _make_container()
    container.attrs["HostConfig"]["Memory"] = existing * 2**20
    container.stats.return_value["memory_stats"]["usage"] = usage * 2**20
    action = ThrottleAction(_make_docker_client(container))
    result = await action.throttle("abc", ThrottleConfig(memory_limit="512m", cpu_shares=256), metric="memory")
    assert not result["success"]
    container.update.assert_not_called()
    assert not action.pending


@pytest.mark.asyncio
async def test_throttle_recovery_after_restart(tmp_path):
    from dri.actions.throttle import ThrottleAction
    from dri.config import DRIConfig, ThresholdRule, ThrottleConfig
    from dri.core.engine import PolicyEngine
    from dri.core.storage import MetricsStorage
    from dri.main import DRIAgent
    from tests.test_engine_dedup import _make_agg_metrics
    import json

    cfg = DRIConfig()
    cfg.actions.throttle = ThrottleConfig(cpu_shares=512, memory_limit="512m")
    cfg.global_policy.rules = [ThresholdRule(metric="cpu", operator=">", value=50, action="throttle")]
    container = _make_container()
    client = _make_docker_client(container)
    path = str(tmp_path / "metrics.db")
    agent = DRIAgent(cfg)
    agent.storage = MetricsStorage(path)
    await agent.storage.init()
    agent.throttle_action = ThrottleAction(client, storage=agent.storage)
    await agent._handle_alert_action(_make_alert(metric="cpu"))
    container.update.assert_called_once_with(cpu_shares=512)
    container.stats.assert_not_called()
    record, = await agent.storage.get_recent_actions()
    assert json.loads(record.details)["previous"] == {"cpu_shares": 1024}
    await agent.storage.close()

    agent = DRIAgent(cfg)
    agent.storage = MetricsStorage(path)
    await agent.storage.init()
    agent.policy_engine = PolicyEngine(cfg)
    agent.throttle_action = ThrottleAction(client, storage=agent.storage)
    await agent.throttle_action.initialize()
    metrics = _make_agg_metrics(container_id="abc123")
    metrics.cpu_percent = 75
    await agent._restore_cleared_throttles(metrics)
    assert container.update.call_count == 1
    metrics.cpu_percent = 49
    await agent._restore_cleared_throttles(metrics)
    container.update.assert_called_with(cpu_shares=1024)
    assert await agent.storage.get_pending_throttles() == {}
    restored, _ = await agent.storage.get_recent_actions()
    assert restored.action_type == "restore" and restored.success
    await agent.storage.close()


@pytest.mark.asyncio
async def test_external_memory_change_survives_restore():
    from dri.actions.throttle import ThrottleAction
    from dri.config import ThrottleConfig

    container = _make_container()
    action = ThrottleAction(_make_docker_client(container))
    result = await action.throttle("abc", ThrottleConfig(cpu_shares=256, memory_limit="512m"), metric="memory_5m")
    assert result["previous"] == {"mem_limit": 2**30}
    container.update.assert_called_once_with(mem_limit=512 * 2**20)
    container.attrs["HostConfig"]["Memory"] = 768 * 2**20
    assert (await action.restore("abc", "memory"))["restored"] == {}
    assert container.update.call_count == 1


async def test_restore_retries_failed_memory_update():
    from dri.actions.throttle import ThrottleAction
    from dri.config import ThrottleConfig

    container = _make_container()
    action = ThrottleAction(_make_docker_client(container))
    cfg = ThrottleConfig(memory_limit="512m")
    assert (await action.throttle("abc", cfg, metric="memory"))["success"]
    real_update = container.update.side_effect
    container.update.side_effect = RuntimeError("daemon unavailable")
    assert not (await action.restore("abc", "memory"))["success"]
    assert ("abc", "memory") in action.pending
    container.update.side_effect = real_update
    result = await action.restore("abc", "memory")
    assert result["restored"] == {"mem_limit": 2**30}
    assert not action.pending


async def test_no_update_when_rollback_snapshot_cannot_be_saved():
    from dri.actions.throttle import ThrottleAction
    from dri.config import ThrottleConfig

    storage = AsyncMock()
    storage.set_pending_throttle.side_effect = RuntimeError("database unavailable")
    container = _make_container()
    action = ThrottleAction(_make_docker_client(container), storage=storage)
    result = await action.throttle("abc", ThrottleConfig(cpu_quota=50000), metric="cpu")
    assert not result["success"]
    container.update.assert_not_called()


async def test_unlimited_cpu_quota_recorded_and_restored():
    from dri.actions.throttle import ThrottleAction
    from dri.config import ThrottleConfig

    container = _make_container()
    action = ThrottleAction(_make_docker_client(container))
    result = await action.throttle("abc", ThrottleConfig(cpu_quota=50000), metric="cpu")
    assert result["previous"] == {"cpu_quota": 0}
    assert (await action.restore("abc", "cpu"))["success"]
    assert container.update.call_args_list == [call(cpu_quota=50000), call(cpu_quota=-1)]


@pytest.mark.parametrize("status", ["exited", "paused", "restarting"])
async def test_graceful_restart_skips_non_running_container(status):
    from dri.actions.heal import HealAction

    container = _make_container()
    container.status = status
    result = await HealAction(_make_docker_client(container)).graceful_restart(container.id)
    assert not result["success"]
    assert status in result["error"]
    container.stop.assert_not_called()
    container.start.assert_not_called()


async def test_stopped_window_emits_no_alerts_or_actions():
    from dri.config import ThresholdRule
    from dri.core.aggregator import MetricsAggregator
    from dri.core.engine import PolicyEngine
    from tests.test_phase1 import _make_metrics

    agent = _make_agent()
    agent.config.global_policy.rules = [
        ThresholdRule(metric="memory", operator=">", value=95, duration_seconds=0, action="restart")
    ]
    agent.policy_engine = PolicyEngine(agent.config)
    agent.policy_engine.register_alert_callback(agent._on_alert)
    agent.storage.get_recent_metrics.return_value = []
    agent.aggregator = MetricsAggregator(agent.storage)
    sample = _make_metrics()
    sample.memory_percent = 97.0
    agent.aggregator.add_metrics(sample)
    agent.aggregator.mark_container_stopped(sample.container_id)
    agent.heal_action.predict_oom.return_value = {"risk_level": "critical", "reason": "high usage"}
    agent._restore_cleared_throttles = AsyncMock()

    await _run_one_policy_tick(agent)

    assert (await agent.aggregator.aggregate(sample.container_id)).container_status == "stopped"
    agent.storage.save_alert.assert_not_awaited()
    agent.storage.save_action.assert_not_awaited()
    agent.heal_action.graceful_restart.assert_not_awaited()
    agent.heal_action.predict_oom.assert_not_awaited()
    agent._restore_cleared_throttles.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_during_throttle_restore_skips_oom_prediction():
    from tests.test_engine_dedup import _make_agg_metrics

    agent = _make_agent()
    metrics = _make_agg_metrics(container_id="crit001", memory_percent=97.0)
    agent.aggregator = AsyncMock()
    agent.aggregator.aggregate_all = AsyncMock(return_value={"crit001": metrics})
    stopped = False
    agent.aggregator.is_container_stopped = MagicMock(side_effect=lambda _cid: stopped)
    agent.policy_engine = AsyncMock()
    agent.policy_engine.evaluate = AsyncMock(return_value=[])

    async def stop_during_restore(_metrics):
        nonlocal stopped
        stopped = True

    agent._restore_cleared_throttles = AsyncMock(side_effect=stop_during_restore)

    await _run_one_policy_tick(agent)

    agent._restore_cleared_throttles.assert_awaited_once_with(metrics)
    agent.heal_action.predict_oom.assert_not_awaited()


async def test_first_status_poll_recognizes_exited_window():
    from dri.core.aggregator import MetricsAggregator
    from tests.test_engine_dedup import _make_agg_metrics, _make_engine
    from tests.test_phase1 import _make_metrics

    agent = _make_agent()
    agent.aggregator = MetricsAggregator(agent.storage)
    agent.storage.get_recent_metrics.return_value = []
    container = _make_container()
    container.status = "exited"
    cid = container.id
    agent.aggregator.add_metrics(_make_metrics(container_id=cid))
    agent.policy_engine = _make_engine(duration_seconds=0)
    breach = _make_agg_metrics(container_id=cid)
    assert await agent.policy_engine.evaluate(breach)
    agent.collector = AsyncMock()
    agent.collector.get_all_containers.return_value = [container]

    async def tick(_):
        agent.running = agent.collector.get_all_containers.await_count == 0

    agent.running = True
    with patch("asyncio.sleep", side_effect=tick):
        await agent._container_status_loop()

    retained = await agent.aggregator.aggregate(cid)
    assert retained.container_status == "stopped"
    assert await agent.policy_engine.evaluate(breach)  # previous notification cooldown ended


async def test_stop_during_alert_callback_blocks_restart_dispatch():
    from dri.core.aggregator import MetricsAggregator
    from dri.events.listener import ContainerEvent

    agent = _make_agent()
    agent.aggregator = MetricsAggregator(agent.storage)
    alert = _make_alert(alert_type="restart")
    event = ContainerEvent({"Action": "stop", "Actor": {"ID": alert.container_id}})
    await agent._on_docker_event(event)
    await agent._handle_alert_action(alert)
    agent.heal_action.graceful_restart.assert_not_awaited()
    agent.storage.save_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_oom_prediction_deduplicates_alerts_across_ticks():
    """Repeated critical OOM ticks emit one alert until renotify, recovery, or stop."""
    from dri.actions.heal import HealAction
    from dri.core.engine import PolicyEngine
    from dri.events.listener import ContainerEvent
    from tests.test_engine_dedup import _make_agg_metrics

    agent = _make_agent()
    agent.config.alert_renotify_seconds = 300
    agent.policy_engine = PolicyEngine(agent.config)
    agent.heal_action = HealAction(_make_docker_client(_make_container()))
    agent.storage.save_alert = AsyncMock()
    agent._restore_cleared_throttles = AsyncMock()

    critical = _make_agg_metrics(container_id="crit001", memory_percent=97.0)
    other = _make_agg_metrics(container_id="crit002", memory_percent=97.0)
    recovered = _make_agg_metrics(container_id="crit001", memory_percent=70.0)

    snapshots = [
        {"crit001": critical, "crit002": other},
        {"crit001": critical, "crit002": other},
        {"crit001": critical},
        {"crit001": recovered},
        {"crit001": critical},
        {"crit001": critical},
    ]
    tick_times = [0, 10, 301, 311, 321, 331]
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    tick = 0

    mock_aggregator = AsyncMock()
    mock_aggregator.is_container_stopped = MagicMock(return_value=False)
    mock_aggregator.mark_container_stopped = MagicMock()
    mock_aggregator.unmark_container_stopped = MagicMock()
    mock_aggregator.update_restart_count = MagicMock()
    agent.aggregator = mock_aggregator

    async def record_alert(alert):
        await agent.storage.save_alert(alert)

    agent._on_alert = AsyncMock(side_effect=record_alert)
    agent.running = True

    with patch("dri.main.datetime") as clock:
        # Finish fast workers before advancing the simulated policy clock.
        async def drive_sleep(_):
            await asyncio.gather(*agent._policy_tasks.values())
            if tick == len(snapshots):
                agent.running = False
                return
            clock.now.return_value = start + timedelta(seconds=tick_times[tick])

        async def aggregate_and_advance():
            nonlocal tick
            data = snapshots[tick]
            tick += 1
            return data

        mock_aggregator.aggregate_all = aggregate_and_advance
        with patch("asyncio.sleep", side_effect=drive_sleep):
            await agent._policy_check_loop()

    # crit001: tick0 (1), tick1 suppressed, tick2 renotified at 301s (2),
    # tick3 cleared, tick4 re-armed (3), tick5 suppressed -> 3 alerts for crit001 + 1 for crit002.
    saved_alerts = [c.args[0] for c in agent.storage.save_alert.await_args_list]
    assert [a.container_id for a in saved_alerts] == ["crit001", "crit002", "crit001", "crit001"]

    # Stopping the container clears the active OOM episode so a restart fires immediately.
    await agent._on_docker_event(ContainerEvent({"Action": "stop", "Actor": {"ID": "crit001"}}))
    with patch("dri.main.datetime") as clock:
        clock.now.return_value = start + timedelta(seconds=335)
        await agent._check_oom_prediction(critical)
    assert agent.storage.save_alert.await_count == 5


@pytest.mark.asyncio
async def test_oom_prediction_renotify_zero_disables_dedup():
    """Setting alert_renotify_seconds=0 disables OOM alert suppression."""
    from dri.actions.heal import HealAction
    from tests.test_engine_dedup import _make_agg_metrics

    agent = _make_agent()
    agent.config.alert_renotify_seconds = 0
    agent.heal_action = HealAction(_make_docker_client(_make_container()))
    agent._on_alert = AsyncMock()
    critical = _make_agg_metrics(container_id="crit001", memory_percent=97.0)

    await agent._check_oom_prediction(critical)
    await agent._check_oom_prediction(critical)
    assert agent._on_alert.await_count == 2


@pytest.mark.asyncio
async def test_failed_oom_alert_save_clears_only_its_provisional_timestamp():
    from dri.utils.metrics import Alert

    agent = _make_agent()
    cid = "crit001"
    timestamp = datetime.now(timezone.utc)
    alert = Alert(
        alert_id="alert-oom",
        container_id=cid,
        container_name="critical-container",
        timestamp=timestamp,
        alert_type="warning",
        metric="memory_percent",
        current_value=97.0,
        threshold=95.0,
        message="OOM risk critical",
    )

    async def fail_save(_alert):
        raise RuntimeError("storage unavailable")

    agent.storage.save_alert = AsyncMock(side_effect=fail_save)
    agent._oom_last_alerted[cid] = timestamp
    with pytest.raises(RuntimeError, match="storage unavailable"):
        await agent._on_alert(alert)
    assert cid not in agent._oom_last_alerted

    newer_timestamp = timestamp + timedelta(seconds=1)

    async def replace_then_fail(_alert):
        agent._oom_last_alerted[cid] = newer_timestamp
        raise RuntimeError("storage unavailable")

    agent.storage.save_alert = AsyncMock(side_effect=replace_then_fail)
    agent._oom_last_alerted[cid] = timestamp
    with pytest.raises(RuntimeError, match="storage unavailable"):
        await agent._on_alert(alert)
    assert agent._oom_last_alerted[cid] == newer_timestamp


async def test_slow_restart_allows_other_containers_to_progress_and_shutdown_drains():
    from tests.test_engine_dedup import _make_agg_metrics, _make_engine

    agent = _make_agent()
    agent.policy_engine = _make_engine(duration_seconds=0, renotify_seconds=0)
    agent.policy_engine.evaluate = AsyncMock(wraps=agent.policy_engine.evaluate)
    agent.config = agent.policy_engine.config
    agent.config.global_policy.rules[0].action = "restart"
    first = _make_agg_metrics(container_id="busy")
    second = _make_agg_metrics(container_id="other")
    agent.aggregator = MagicMock()
    started, other_checked, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    agent.aggregator.is_container_stopped.return_value = False

    async def snapshots():
        # Introduce the second container on a later tick while the first is busy.
        return {"busy": first, "other": second} if started.is_set() else {"busy": first}

    agent.aggregator.aggregate_all = snapshots
    agent.config.policy_check_interval = 0
    agent._restore_cleared_throttles = AsyncMock()
    agent.heal_action.predict_oom.return_value = {"risk_level": "low"}
    calls = []

    async def restart(cid, **kwargs):
        calls.append(cid)
        if cid == "busy":
            started.set()
            await asyncio.wait_for(release.wait(), timeout=30)
        else:
            other_checked.set()
        return {"success": True}

    agent.heal_action.graceful_restart.side_effect = restart
    agent.running = True
    loop_task = asyncio.create_task(agent._policy_check_loop())
    try:
        await asyncio.wait_for(other_checked.wait(), timeout=2)
        assert calls == ["busy", "other"]
        assert "busy" in agent._policy_tasks
        assert agent.policy_engine.evaluate.await_args_list.count(call(first)) == 1
        shutdown = asyncio.create_task(agent.shutdown())
        await asyncio.sleep(0)
        assert not shutdown.done()
        agent.storage.close.assert_not_awaited()
        release.set()
        await asyncio.wait_for(shutdown, timeout=2)
        assert agent.storage.save_action.await_count == 2
        agent.storage.close.assert_awaited_once()
        assert not agent._policy_tasks
    finally:
        release.set()
        agent.running = False
        await loop_task
