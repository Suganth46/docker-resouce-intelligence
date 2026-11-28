"""
Tests for P0-5: Image selector matches actual image using regex.
"""

import pytest
import re
from datetime import datetime, timezone

def _make_agg_metrics(
    container_id: str = "abc123",
    container_name: str = "test-container",
    image: str = "redis:7-alpine",
):
    from dri.utils.metrics import AggregatedMetrics
    return AggregatedMetrics(
        container_id=container_id,
        container_name=container_name,
        image=image,
        timestamp=datetime.now(timezone.utc),
        cpu_percent=90.0,
        cpu_percent_5m=90.0,
        cpu_percent_15m=90.0,
        cpu_percent_60m=90.0,
        memory_percent=0.0,
        memory_percent_5m=0.0,
        memory_percent_15m=0.0,
        memory_percent_60m=0.0,
        memory_growth_rate_1m=0.0,
        memory_growth_rate_5m=0.0,
    )

def _make_engine_with_image_selector(image_selector: str):
    from dri.core.engine import PolicyEngine
    from dri.config import DRIConfig, ContainerPolicy, ThresholdRule
    rule = ThresholdRule(
        metric="cpu",
        operator=">",
        value=80.0,
        duration_seconds=0,
        action="warning",
    )
    policy = ContainerPolicy(name="image_policy", enabled=True, selector={"image": image_selector}, rules=[rule])
    config = DRIConfig(global_policy=ContainerPolicy(name="global", enabled=False), container_policies=[policy])
    engine = PolicyEngine(config)
    return engine


@pytest.mark.asyncio
async def test_image_selector_matches_regex():
    """P0-5: image selector uses regex matching against the image field."""
    # Matches exact string
    engine = _make_engine_with_image_selector(r"redis:7-alpine")
    metrics = _make_agg_metrics(image="redis:7-alpine", container_name="my-container")
    alerts = await engine.evaluate(metrics)
    assert alerts is not None, "Alert expected, image regex matches"

    # Matches regex
    engine = _make_engine_with_image_selector(r"redis:.*")
    alerts = await engine.evaluate(metrics)
    assert alerts is not None, "Alert expected, image regex matches"

    # Doesn't match regex
    engine = _make_engine_with_image_selector(r"nginx:.*")
    alerts = await engine.evaluate(metrics)
    assert alerts is None, "No alert expected, image regex does not match"


@pytest.mark.parametrize(("metric", "selector", "error"), [
    ("memroy", {"container_id": "abc123"}, "Unknown policy metric"),
    ("cpu", {"label": "redis"}, "label"),
    ("cpu", {"image": "["}, "Invalid image selector regex"),
])
def test_invalid_yaml_policy_rejected(tmp_path, metric, selector, error):
    import yaml
    from pydantic import ValidationError
    from dri.config import load_config

    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"container_policies": [{
        "name": "worker", "selector": selector,
        "rules": [{"metric": metric, "operator": "<", "value": 10, "action": "restart"}],
    }]}))
    with pytest.raises(ValidationError, match=error):
        load_config(str(path))


@pytest.mark.parametrize(("cid", "image", "expected"), [
    ("abc123", "redis:7", 1), ("other", "redis:7", 0), ("abc123", "nginx", 0),
])
async def test_selectors_must_all_match_and_apply_once(cid, image, expected):
    engine = _make_engine_with_image_selector(r"redis:")
    engine.config.container_policies[0].selector["container_id"] = "abc123"
    engine.config.alert_renotify_seconds = 0
    alerts = await engine.evaluate(_make_agg_metrics(container_id=cid, image=image))
    assert len(alerts or []) == expected
