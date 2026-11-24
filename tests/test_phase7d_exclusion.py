from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dri.main import DRIAgent
from tests.test_engine_dedup import _make_agg_metrics, _make_engine


@pytest.mark.parametrize(
    "name,patterns,excluded",
    [
        ("dri-ollama", ["dri-*"], True),
        ("my-app", ["dri-*"], False),
        ("db-prod", ["db-?rod"], True),
        ("dri-agent", [], False),
    ],
)
async def test_policy_exclusions(name, patterns, excluded):
    engine = _make_engine(duration_seconds=0)
    engine.config.global_policy.rules[0].metric = "cpu"
    engine.config.policy_exclude_names = patterns
    metrics = _make_agg_metrics(container_name=name)
    metrics.cpu_percent = 99
    assert bool(await engine.evaluate(metrics)) == (not excluded)


@pytest.mark.parametrize("action", ["throttle", "isolate", "restart"])
async def test_excluded_action(action):
    agent = DRIAgent(_make_engine().config)
    agent.throttle_action = agent.isolate_action = agent.heal_action = AsyncMock()
    await agent._handle_alert_action(
        SimpleNamespace(container_name="dri-ollama", container_id="dri", alert_type=action)
    )
    assert not agent.throttle_action.mock_calls and not agent._action_cooldowns
