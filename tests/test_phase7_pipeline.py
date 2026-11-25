from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from dri.core.background import RecommendationCacheWorker
from dri.core.recommender import Recommendation, InsufficientDataResult


@pytest.mark.parametrize("interval", [30, 3600])
async def test_cache_retry_after_empty_and_insufficient(monkeypatch, interval):
    aggregator = SimpleNamespace(_windows={})
    rec = AsyncMock()
    success = Mock(spec=Recommendation)
    success.to_dict.return_value = {}
    rec.compute_recommendation.side_effect = [
        InsufficientDataResult("app", "app", samples_available=3, min_samples_required=10), success
    ]
    worker = RecommendationCacheWorker(AsyncMock(), aggregator, rec, None, interval)
    delays = []

    async def sleep(seconds):
        delays.append(seconds)
        aggregator._windows["app"] = [SimpleNamespace(container_name="app")]
        if len(delays) == 3:
            worker.stop()

    monkeypatch.setattr("dri.core.background.asyncio.sleep", sleep)
    await worker.run()
    assert delays == [min(60, interval), min(60, interval), interval]
    assert not worker.computing
