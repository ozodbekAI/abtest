from __future__ import annotations

import io
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from PIL import Image

from app.models.ab_test import ABTest, ABTestStatus, ABTestVariant
from app.services.ab_test_service import ABTestService


class FakeDB:
    class Bind:
        class Dialect:
            name = "sqlite"
        dialect = Dialect()
    def get_bind(self):
        return self.Bind()
    def add(self, value):
        return None
    async def flush(self):
        return None
    async def commit(self):
        return None


class FakePromotion:
    def __init__(self, totals):
        self.totals = list(totals)
        self.calls = 0
    async def fullstats(self, campaign_id, *, started_at=None, end_at=None, refresh=False):
        value = self.totals[min(self.calls, len(self.totals) - 1)]
        self.calls += 1
        return {"views": value[0], "clicks": value[1], "orders": value[2], "sum": value[3], "_has_data": True,
                "_available_metrics": {"views": True, "clicks": True, "orders": True, "sum": True}}


def build_test(position: int, settled: int = 0):
    test = ABTest(
        id=1, user_id=1, connection_id=1, nm_id=123, title="final", status=ABTestStatus.RUNNING,
        skip_current_photo=True, views_per_variant=100, cpm_rub=300, budget_rub=1200,
        current_variant_order=position, campaign_state="paused", operation_state="succeeded",
        media_status="variant_applied", stats_quality="preliminary", wb_campaign_id=777,
        started_at=datetime.now(timezone.utc), settled_total_views=settled,
        settled_total_clicks=0, settled_total_orders=0, settled_total_spend_rub=0,
        last_total_views=settled, last_total_clicks=0, last_total_orders=0, last_total_spend_rub=0,
        created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
    )
    test.variants = [
        ABTestVariant(test_id=1, position=1, source_type="upload", views=0, clicks=0, orders=0, spend_rub=0),
        ABTestVariant(test_id=1, position=2, source_type="upload", views=0, clicks=0, orders=0, spend_rub=0),
    ]
    return test


@pytest.mark.asyncio
async def test_stage_settlement_keeps_ordinary_aggregates_unallocated(monkeypatch):
    monkeypatch.setattr("app.services.ab_test_service.asyncio.sleep", AsyncMock())
    service = ABTestService(FakeDB())
    test = build_test(1, settled=0)
    promotion = FakePromotion([(100, 10, 0, 30.0)])
    await service._settle_stage_stats(test, promotion, attempts=1)
    assert test.variants[0].views == 0
    assert test.variants[0].clicks == 0
    assert test.variants[1].views == 0
    assert test.settled_total_views == 100
    assert test.stats_quality == "aggregate_unverified"
    assert test.unallocated_views == 100

    test.current_variant_order = 2
    test.stage_views = 0
    test.stage_clicks = 0
    test.stats_quality = "preliminary"
    promotion = FakePromotion([(200, 30, 0, 60.0)])
    await service._settle_stage_stats(test, promotion, attempts=1)
    assert test.variants[0].views == 0
    assert test.variants[1].views == 0
    assert test.variants[1].clicks == 0
    assert test.unallocated_views == 200
    assert test.settled_total_views == 200


@pytest.mark.asyncio
async def test_stage_settlement_does_not_double_count_cumulative_poll_growth(monkeypatch):
    monkeypatch.setattr("app.services.ab_test_service.asyncio.sleep", AsyncMock())
    service = ABTestService(FakeDB())
    test = build_test(1, settled=0)
    promotion = FakePromotion([(100, 10, 1, 30.0), (120, 12, 1, 36.0), (120, 12, 1, 36.0)])
    original_delay = service.__class__.__module__
    from app.core.config import settings
    old_delay = settings.ab_test_stats_settle_delay_sec
    settings.ab_test_stats_settle_delay_sec = 1
    try:
        await service._settle_stage_stats(test, promotion, attempts=3)
    finally:
        settings.ab_test_stats_settle_delay_sec = old_delay
    assert test.variants[0].views == 0
    assert test.variants[0].clicks == 0
    assert test.unallocated_views == 120
    assert test.unallocated_clicks == 12
    assert test.settled_total_views == 120


def test_truncated_jpeg_is_rejected_after_full_decode():
    image = Image.new("RGB", (900, 900), (90, 120, 160))
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=92)
    data = output.getvalue()
    truncated = data[:-max(100, len(data) // 5)]
    with pytest.raises(HTTPException):
        ABTestService._prepare_image("broken.jpg", "image/jpeg", truncated)


def test_winner_is_blocked_until_stage_is_settled():
    test = build_test(2, settled=200)
    test.stats_quality = "preliminary"
    test.variants[0].views = 300
    test.variants[0].clicks = 15
    test.variants[1].views = 300
    test.variants[1].clicks = 150
    winner, decision = ABTestService._winner_result(test)
    assert winner is None
    assert decision == "statistics_not_attributable"

    test.stats_quality = "stage_attributed"
    winner, decision = ABTestService._winner_result(test)
    assert winner is test.variants[1]
    assert decision == "winner_found"
