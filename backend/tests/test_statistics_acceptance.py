"""Execution regressions for the external F01–F09 acceptance findings.

WB and media effects are deterministic doubles; the real sync, settlement,
completion, arithmetic and decision methods execute. No real ad is funded.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models.ab_test import ABTest, ABTestStatus, ABTestVariant
from app.models.wb_connection import WBConnection
from app.services.ab_test_budget import calculate_budget_breakdown, calculate_protected_budget
from app.services.ab_test_service import ABTestReconciliationRequired, ABTestService
from app.services.dashboard_service import DashboardService


NOW = datetime(2026, 9, 27, 21, 30, tzinfo=timezone.utc)


def snapshot(views=0, clicks=0, spend=0, **overrides):
    return {"views": views, "clicks": clicks, "orders": 0, "sum": spend,
            "_has_data": True, "_data_complete": True,
            "_available_metrics": {"views": True, "clicks": True, "orders": True, "sum": True},
            **overrides}


def experiment(**overrides):
    values = dict(id=1, user_id=1, connection_id=1, nm_id=123, title="statistics acceptance",
                  status=ABTestStatus.RUNNING, wb_campaign_id=777, skip_current_photo=True,
                  keep_winner_as_main=False, delete_test_media=False,
                  views_per_variant=400, cpm_rub=300, budget_rub=5000,
                  current_variant_order=1, campaign_state="running", operation_state="succeeded",
                  media_status="variant_applied", media_state={}, stats_quality="preliminary",
                  started_at=NOW - timedelta(hours=1), stage_started_at=NOW - timedelta(minutes=2),
                  last_synced_at=NOW - timedelta(minutes=1),
                  settled_total_views=0, settled_total_clicks=0, settled_total_orders=0,
                  settled_total_spend_rub=0, last_total_views=0, last_total_clicks=0,
                  last_total_orders=0, last_total_spend_rub=0, stage_views=0,
                  stage_clicks=0, stage_spend_rub=0, unallocated_views=0,
                  unallocated_clicks=0, unallocated_spend_rub=0,
                  created_at=NOW, updated_at=NOW, placement="combined", bid_type="unified")
    values.update(overrides)
    result = ABTest(**values)
    result.connection = WBConnection(id=1, user_id=1, store_name="acceptance")
    result.variants = [ABTestVariant(id=pos, test_id=1, position=pos, source_type="upload",
                                    views=0, clicks=0, orders=0, spend_rub=0, is_winner=False)
                       for pos in (1, 2)]
    return result


class Promotion:
    def __init__(self, *observations, minimum=300):
        self.observations = list(observations)
        self.calls = []
        self.minimum = minimum
        self.start_campaign = AsyncMock()

    def cached_fullstats(self, *args, **kwargs):
        return None

    async def fullstats(self, campaign_id, **kwargs):
        self.calls.append((campaign_id, kwargs))
        return self.observations.pop(0) if len(self.observations) > 1 else self.observations[0]

    async def get_min_bid(self, **kwargs):
        return self.minimum


@asynccontextmanager
async def unlocked(*args, **kwargs):
    yield


def service_for(monkeypatch, promotion, *, real_finish=False):
    db = SimpleNamespace(commit=AsyncMock(), flush=AsyncMock())
    service = ABTestService(db)
    monkeypatch.setattr(service, "_now", lambda: NOW)
    monkeypatch.setattr(service, "_clients", AsyncMock(return_value=(object(), promotion)))
    monkeypatch.setattr(service, "_stats_operation_lock", unlocked)
    monkeypatch.setattr(service, "_audit", AsyncMock())
    monkeypatch.setattr("app.services.ab_test_service.asyncio.sleep", AsyncMock())
    async def pause(test, promotion):
        test.campaign_state = "paused"
    async def stop(test, promotion):
        test.campaign_state = "stopped"
    async def restore(test, content):
        test.media_status = "restored"
    async def apply(test, variant, content, promotion):
        test.media_status = "variant_applied"
    monkeypatch.setattr(service, "_pause_campaign_confirmed", AsyncMock(side_effect=pause))
    monkeypatch.setattr(service, "_stop_campaign_confirmed", AsyncMock(side_effect=stop))
    monkeypatch.setattr(service, "_restore_original", AsyncMock(side_effect=restore))
    monkeypatch.setattr(service, "_assert_media_snapshot_current", AsyncMock())
    monkeypatch.setattr(service, "_apply_variant", AsyncMock(side_effect=apply))
    monkeypatch.setattr(service, "_confirm_campaign_active", AsyncMock(return_value=9))
    if not real_finish:
        async def finish(test, content, promotion, **kwargs):
            test.status = ABTestStatus.STOPPED
            test.campaign_state = "stopped"
            test.media_status = "restored"
            test.operation_state = "succeeded"
        monkeypatch.setattr(service, "_finish_loaded", AsyncMock(side_effect=finish))
    return service


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [snapshot(_has_data=False), snapshot(_data_complete=False),
                                      {"views": 100, "clicks": 10}])
async def test_missing_statistics_stop_after_deadline_and_commit(monkeypatch, data):
    service = service_for(monkeypatch, Promotion(data))
    test = experiment(last_synced_at=NOW - timedelta(minutes=6))
    await service._sync_loaded(test)
    assert test.status == ABTestStatus.STOPPED
    assert test.stats_quality == "reconciliation_required"
    assert test.last_total_views == 0
    assert test.incident_id and test.last_error
    service._finish_loaded.assert_awaited_once()
    service.db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_recent_no_data_is_persisted_without_invented_zeros(monkeypatch):
    service = service_for(monkeypatch, Promotion(snapshot(_has_data=False)))
    test = experiment(last_total_views=150, last_total_clicks=10)
    await service._sync_loaded(test)
    assert test.stats_quality == "no_data"
    assert test.last_total_views == 150 and test.last_total_clicks == 10
    service._finish_loaded.assert_not_awaited()
    service.db.commit.assert_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [snapshot(99, 5, 10), snapshot(100, 4, 10),
                                      snapshot(100, 5, 9), snapshot(50, 51, 10)])
async def test_regression_and_impossible_ctr_stop_without_overwriting_totals(monkeypatch, data):
    service = service_for(monkeypatch, Promotion(data))
    # Cached-path inputs bypass the client's validation, exercising the
    # service's independent validation and durable stop.
    monkeypatch.setattr(service, "_fullstats_windowed", AsyncMock(return_value=data))
    test = experiment(last_total_views=100, last_total_clicks=5, last_total_spend_rub=10)
    await service._sync_loaded(test)
    assert test.status == ABTestStatus.STOPPED
    assert (test.last_total_views, test.last_total_clicks, test.last_total_spend_rub) == (100, 5, 10)
    assert test.stats_quality == "reconciliation_required"
    assert service._audit.await_args_list[0].kwargs["details"]["campaign_totals"] == data
    service.db.commit.assert_awaited()


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), True, "invalid", 1.5])
def test_invalid_impression_values_are_rejected(value):
    quality, totals = ABTestService._validated_stats(snapshot(value))
    assert (quality, totals) == ("invalid", None)


@pytest.mark.asyncio
async def test_zero_progress_uses_current_stage_age_not_test_age(monkeypatch):
    service = service_for(monkeypatch, Promotion(snapshot()))
    test = experiment(started_at=NOW - timedelta(days=4), stage_started_at=NOW - timedelta(minutes=1))
    await service._sync_loaded(test)
    service._finish_loaded.assert_not_awaited()
    assert test.status == ABTestStatus.RUNNING


@pytest.mark.asyncio
async def test_zero_progress_old_stage_stops_and_commits(monkeypatch):
    service = service_for(monkeypatch, Promotion(snapshot()))
    test = experiment(started_at=NOW - timedelta(days=4), stage_started_at=NOW - timedelta(days=3))
    await service._sync_loaded(test)
    service._finish_loaded.assert_awaited_once()
    service.db.commit.assert_awaited()
    assert test.status == ABTestStatus.STOPPED


@pytest.mark.asyncio
async def test_live_progress_is_not_treated_as_zero_when_variants_unallocated(monkeypatch):
    service = service_for(monkeypatch, Promotion(snapshot(20, 1, 5)))
    test = experiment(started_at=NOW - timedelta(days=4), stage_started_at=NOW - timedelta(days=3))
    await service._sync_loaded(test)
    assert test.stage_views == test.unallocated_views == 20
    assert test.variants[0].views == 0
    service._finish_loaded.assert_not_awaited()


@pytest.mark.asyncio
async def test_repeated_observation_does_not_duplicate_totals(monkeypatch):
    service = service_for(monkeypatch, Promotion(snapshot(100, 10, 20)))
    test = experiment()
    await service._sync_loaded(test)
    await service._sync_loaded(test)
    assert test.last_total_views == test.unallocated_views == 100
    assert sum(v.views for v in test.variants) + test.unallocated_views == test.last_total_views
    assert sum(v.clicks for v in test.variants) + test.unallocated_clicks == test.last_total_clicks


@pytest.mark.asyncio
async def test_ordinary_full_flow_advances_and_finishes_without_false_winner(monkeypatch):
    from app.core.config import settings
    old = (settings.ab_test_stats_min_settle_sec, settings.ab_test_stats_max_settle_sec, settings.ab_test_stats_stable_snapshots)
    settings.ab_test_stats_min_settle_sec = 0
    settings.ab_test_stats_max_settle_sec = 8
    settings.ab_test_stats_stable_snapshots = 3
    try:
        promotion = Promotion(snapshot(400, 24, 120), snapshot(450, 27, 135), snapshot(450, 27, 135),
                              snapshot(450, 27, 135), snapshot(450, 27, 135), snapshot(450, 27, 135),
                              snapshot(850, 43, 255), snapshot(850, 43, 255), snapshot(850, 43, 255))
        service = service_for(monkeypatch, promotion, real_finish=True)
        test = experiment()
        await service._sync_loaded(test)
        assert test.current_variant_order == 2 and test.stage_views == 0
        assert test.settled_total_views == 450
        test.stage_started_at = NOW - timedelta(minutes=4)
        for _ in range(4):
            await service._sync_loaded(test)
        assert test.status == ABTestStatus.FINISHED
        assert test.variants[1].views == 400
        assert test.winner_variant_order is None
        assert test.winner_decision in {"statistics_not_attributable", "not_statistically_significant", "attribution_uncertainty", "data_unstable", "media_not_confirmed"}
        assert test.stats_quality in {"stage_estimated", "aggregate_unverified", "data_unstable", "preliminary"}
        assert test.last_total_views == 850
        assert sum(v.views for v in test.variants) + test.unallocated_views == test.last_total_views
        assert round(sum(float(v.spend_rub or 0) for v in test.variants) + float(test.unallocated_spend_rub or 0), 2) == 255
        assert service._stop_campaign_confirmed.await_count == 1
        assert service._restore_original.await_count == 1
        assert promotion.start_campaign.await_count == 1
    finally:
        settings.ab_test_stats_min_settle_sec, settings.ab_test_stats_max_settle_sec, settings.ab_test_stats_stable_snapshots = old


@pytest.mark.asyncio
async def test_unstable_settlement_never_certifies_attribution(monkeypatch):
    service = service_for(monkeypatch, Promotion(snapshot(400, 20, 120), snapshot(420, 21, 126)))
    test = experiment(campaign_state="paused", media_state={"verified_positions": [1, 2]})
    await service._settle_stage_stats(test, service._clients.return_value[1], attempts=2)
    assert test.settled_total_views == 420
    assert test.stats_quality in {"stage_estimated", "data_unstable"}
    assert test.last_total_views == 420


@pytest.mark.asyncio
async def test_incomplete_settlement_never_uses_missing_values_as_zero(monkeypatch):
    promotion = Promotion(snapshot(_data_complete=False))
    service = service_for(monkeypatch, promotion)
    test = experiment(campaign_state="paused", last_total_views=400)
    await service._settle_stage_stats(test, promotion, attempts=2)
    assert test.last_total_views == 400
    assert test.stats_quality == "data_unstable"


@pytest.mark.asyncio
async def test_settlement_requires_confirmed_pause_and_fresh_observations(monkeypatch):
    promotion = Promotion(snapshot(400, 20, 120))
    service = service_for(monkeypatch, promotion)
    test = experiment()
    with pytest.raises(ABTestReconciliationRequired, match="пауза"):
        await service._settle_stage_stats(test, promotion)
    assert not promotion.calls
    test.campaign_state = "paused"
    await service._settle_stage_stats(test, promotion, attempts=2)
    assert len(promotion.calls) == 2
    assert all(call[1]["refresh"] for call in promotion.calls)
    assert test.stats_quality in {"stage_estimated", "data_unstable"}


@pytest.mark.asyncio
async def test_unstable_provider_breakdown_cannot_certify_winner(monkeypatch):
    # WB may return a complete per-variant breakdown even while cumulative
    # fullstats are still changing. That must not bypass the 3/10/30 settlement
    # gate or produce a winner before reconciliation is stable.
    snapshots = [
        snapshot(820, 84, 246, _variant_attribution_complete=True, _variant_totals={
            "1": {"views": 400, "clicks": 60, "orders": 0, "sum": 120},
            "2": {"views": 420, "clicks": 24, "orders": 0, "sum": 126},
        }),
        snapshot(830, 85, 249, _variant_attribution_complete=True, _variant_totals={
            "1": {"views": 405, "clicks": 61, "orders": 0, "sum": 121.5},
            "2": {"views": 425, "clicks": 24, "orders": 0, "sum": 127.5},
        }),
        snapshot(840, 86, 252, _variant_attribution_complete=True, _variant_totals={
            "1": {"views": 410, "clicks": 62, "orders": 0, "sum": 123},
            "2": {"views": 430, "clicks": 24, "orders": 0, "sum": 129},
        }),
        snapshot(850, 87, 255, _variant_attribution_complete=True, _variant_totals={
            "1": {"views": 415, "clicks": 63, "orders": 0, "sum": 124.5},
            "2": {"views": 435, "clicks": 24, "orders": 0, "sum": 130.5},
        }),
    ]
    service = service_for(monkeypatch, Promotion(*snapshots), real_finish=False)
    test = experiment(campaign_state="paused", media_state={"verified_positions": [1, 2]})
    await service._settle_stage_stats(
        test, service._clients.return_value[1],
        poll_interval_sec=1, min_settle_sec=2, max_settle_sec=3, stable_required=3,
    )
    assert test.stats_quality == "data_unstable"
    assert (test.media_state.get("stats_reconciliation") or {}).get("status") == "data_unstable"
    winner, decision = service._winner_result(test)
    assert winner is None
    assert decision == "data_unstable"


@pytest.mark.asyncio
async def test_explicit_variant_breakdown_enables_safe_winner(monkeypatch):
    attributed = snapshot(820, 84, 246, _variant_attribution_complete=True, _variant_totals={
        "1": {"views": 400, "clicks": 60, "orders": 0, "sum": 120},
        "2": {"views": 420, "clicks": 24, "orders": 0, "sum": 126},
    })
    service = service_for(monkeypatch, Promotion(attributed), real_finish=False)
    test = experiment(campaign_state="paused", media_state={"verified_positions": [1, 2]})
    await service._settle_stage_stats(test, service._clients.return_value[1], attempts=2)
    assert test.stats_quality == "stage_attributed"
    assert test.variants[0].views == 400 and test.variants[1].views == 420
    winner, decision = service._winner_result(test)
    assert winner is test.variants[0] and decision == "winner_found"


@pytest.mark.asyncio
async def test_transition_window_accumulates_multiple_distinct_deltas(monkeypatch):
    service = service_for(monkeypatch, Promotion(snapshot(100, 10, 30), snapshot(200, 20, 60)))
    test = experiment(stage_started_at=NOW - timedelta(seconds=60))
    await service._sync_loaded(test)
    await service._sync_loaded(test)
    assert test.unallocated_views == 200
    assert test.unallocated_clicks == 20
    transition = (test.media_state or {}).get("stats_transition") or {}
    assert transition["unallocated_views"] == 200
    assert transition["unallocated_clicks"] == 20


def test_unresolved_previous_stage_blocks_final_winner():
    test = experiment(stats_quality="stage_attributed", media_state={"verified_positions": [1, 2], "unresolved_stats_stages": [{"variant_position": 1, "reason": "max_wait_exceeded"}]})
    test.variants[0].views, test.variants[0].clicks = 1000, 200
    test.variants[1].views, test.variants[1].clicks = 1000, 50
    winner, decision = ABTestService._winner_result(test)
    assert winner is None
    assert decision == "attribution_uncertainty"


def test_aggregate_stage_estimate_cannot_certify_winner_after_delayed_events():
    test = experiment(stats_quality="stage_estimated", media_state={"verified_positions": [1, 2]})
    test.variants[0].views, test.variants[0].clicks = 2000, 0
    test.variants[1].views, test.variants[1].clicks = 2000, 150
    winner, decision = ABTestService._winner_result(test)
    assert winner is None
    assert decision == "statistics_not_attributable"


def test_winner_checks_third_variant_worst_case():
    test = experiment(stats_quality="stage_attributed", media_state={"verified_positions": [1, 2, 3]}, unallocated_views=1000, unallocated_clicks=100)
    test.variants.append(ABTestVariant(id=3, test_id=1, position=3, source_type="upload", views=300, clicks=12))
    test.variants[0].views, test.variants[0].clicks = 10000, 1000
    test.variants[1].views, test.variants[1].clicks = 10000, 500
    winner, decision = ABTestService._winner_result(test)
    assert winner is None
    assert decision == "attribution_uncertainty"


@pytest.mark.asyncio
async def test_settlement_resume_respects_persisted_30_minute_deadline(monkeypatch):
    service = service_for(monkeypatch, Promotion(snapshot(500, 50, 150)))
    started = NOW - timedelta(minutes=29)
    test = experiment(
        campaign_state="paused",
        media_state={
            "stats_reconciliation": {
                "status": "waiting",
                "stage_variant_position": 1,
                "started_at": started.isoformat(),
                "elapsed_sec": 1740,
                "stable_snapshots": 3,
                "last_snapshot": {"views": 500, "clicks": 50, "orders": 0, "spend": 150.0},
            },
            "verified_positions": [1, 2],
        },
        last_total_views=500, last_total_clicks=50, last_total_spend_rub=150,
        settled_total_views=500, settled_total_clicks=50, settled_total_spend_rub=150,
    )
    await service._settle_stage_stats(test, service._clients.return_value[1])
    reconcile = (test.media_state or {}).get("stats_reconciliation") or {}
    assert reconcile["status"] in {"settled", "data_unstable"}
    assert reconcile["elapsed_sec"] <= 1800
    assert len(service._clients.return_value[1].calls) == 1


@pytest.mark.parametrize(
    "a,b,expected",
    [
        ((60, 400), (24, 420), True),
        ((32, 400), (22, 420), False),
    ],
)
def test_two_proportion_significance(a, b, expected):
    p = ABTestService._two_proportion_p_value(a[0], a[1], b[0], b[1])
    assert p is not None
    assert (p < 0.05) is expected


def test_worst_case_unallocated_can_invalidate_winner():
    a = ABTestVariant(id=1, test_id=1, position=1, source_type="upload", views=400, clicks=60)
    b = ABTestVariant(id=2, test_id=1, position=2, source_type="upload", views=420, clicks=24)
    leader_p, runner_p, safe = ABTestService._worst_case_unallocated(a, b, 500, 100)
    assert leader_p <= a.ctr / 100
    assert runner_p >= b.ctr / 100
    assert safe is False


@pytest.mark.asyncio
async def test_post_stop_preserves_observed_partial_stage_data(monkeypatch):
    # F06-style budget protection may stop the test before delayed fullstats
    # catches up. Preserve the observed serving-stage delta on the active variant
    # rather than returning 0/0 and hiding it in global unallocated counters.
    service = service_for(monkeypatch, Promotion(snapshot(900, 54, 210)))
    test = experiment(campaign_state="stopped")
    await service._settle_stage_stats(
        test, service._clients.return_value[1],
        attempts=1, poll_interval_sec=1, min_settle_sec=0, max_settle_sec=0, stable_required=1,
        post_stop=True, preserve_interrupted_stage=True,
    )
    variant = next(v for v in test.variants if v.position == 1)
    assert (variant.views, variant.clicks, variant.spend_rub) == (900, 54, 210)
    assert test.stage_views == 900
    assert test.stage_clicks == 54
    assert test.stats_quality == "data_unstable"
    assert test.unallocated_views == 0
    assert test.unallocated_clicks == 0


@pytest.mark.asyncio
async def test_post_stop_preserves_observed_partial_stage_when_transition_buffered(monkeypatch):
    # High-traffic stages can have real observations buffered in stats_transition
    # during the first seconds after a photo switch. A safety stop must retain the
    # observed 900/54 exposure instead of subtracting that buffer into 0/0.
    service = service_for(monkeypatch, Promotion(snapshot(900, 54, 210)))
    test = experiment(
        campaign_state="stopped",
        last_total_views=900,
        last_total_clicks=54,
        last_total_spend_rub=210,
        media_state={
            "stats_transition": {
                "variant_position": 1,
                "unallocated_views": 900,
                "unallocated_clicks": 54,
                "unallocated_spend_rub": 210.0,
            }
        },
    )

    await service._settle_stage_stats(
        test, service._clients.return_value[1],
        attempts=1, poll_interval_sec=1, min_settle_sec=0, max_settle_sec=0, stable_required=1,
        post_stop=True, preserve_interrupted_stage=True,
    )

    variant = next(v for v in test.variants if v.position == 1)
    assert (variant.views, variant.clicks, variant.spend_rub) == (900, 54, 210)
    assert (test.stage_views, test.stage_clicks, test.stage_spend_rub) == (900, 54, 210)
    assert test.unallocated_views == 0
    assert test.unallocated_clicks == 0


@pytest.mark.asyncio
async def test_post_stop_preserve_is_idempotent_after_settlement(monkeypatch):
    service = service_for(monkeypatch, Promotion(snapshot(900, 54, 210)))
    test = experiment(
        campaign_state="stopped",
        last_total_views=900,
        last_total_clicks=54,
        last_total_spend_rub=210,
        media_state={
            "stats_transition": {
                "variant_position": 1,
                "unallocated_views": 900,
                "unallocated_clicks": 54,
                "unallocated_spend_rub": 210.0,
            }
        },
    )

    promotion = service._clients.return_value[1]
    await service._settle_stage_stats(
        test, promotion,
        attempts=1, poll_interval_sec=1, min_settle_sec=0, max_settle_sec=0, stable_required=1,
        post_stop=True, preserve_interrupted_stage=True,
    )
    first = (test.variants[0].views, test.variants[0].clicks, test.variants[0].spend_rub)

    await service._settle_stage_stats(
        test, promotion,
        attempts=1, poll_interval_sec=1, min_settle_sec=0, max_settle_sec=0, stable_required=1,
        post_stop=True, preserve_interrupted_stage=True,
    )
    assert (test.variants[0].views, test.variants[0].clicks, test.variants[0].spend_rub) == first


@pytest.mark.asyncio
async def test_settlement_contract_requires_three_stable_snapshots_and_ten_minutes(monkeypatch):
    observations = [snapshot(400, 20, 120), snapshot(430, 21, 129), snapshot(450, 22, 135), snapshot(450, 22, 135), snapshot(450, 22, 135)]
    service = service_for(monkeypatch, Promotion(*observations))
    test = experiment(campaign_state="paused")
    await service._settle_stage_stats(test, service._clients.return_value[1], attempts=5, poll_interval_sec=1, min_settle_sec=4, max_settle_sec=8, stable_required=3)
    state = test.media_state.get("stats_reconciliation") or {}
    assert state["status"] == "settled"
    assert state["stable_snapshots"] >= 3
    assert state["elapsed_sec"] >= 4

@pytest.mark.asyncio
async def test_unstable_after_max_wait_continues_with_explicit_uncertainty(monkeypatch):
    service = service_for(monkeypatch, Promotion(snapshot(400, 20, 120), snapshot(410, 21, 123), snapshot(420, 22, 126), snapshot(430, 23, 129)))
    test = experiment(campaign_state="paused")
    await service._settle_stage_stats(test, service._clients.return_value[1], poll_interval_sec=1, min_settle_sec=2, max_settle_sec=3, stable_required=3)
    state = test.media_state.get("stats_reconciliation") or {}
    assert state["status"] == "data_unstable"
    assert test.winner_decision == "data_unstable"



@pytest.mark.asyncio
async def test_moscow_midnight_and_31_day_windows_are_complete(monkeypatch):
    promotion = Promotion(snapshot(10, 1, 3))
    service = service_for(monkeypatch, promotion)
    totals = await service._fullstats_windowed(promotion, 777, datetime(2026, 7, 24, 21, 1, tzinfo=timezone.utc))
    periods = [(kw["started_at"], kw["end_at"]) for _, kw in promotion.calls]
    assert periods == [(date(2026, 7, 25), date(2026, 8, 24)),
                       (date(2026, 8, 25), date(2026, 9, 24)),
                       (date(2026, 9, 25), date(2026, 9, 28))]
    assert totals["views"] == 30 and totals["_data_complete"]
    assert all((end - begin).days <= 30 for begin, end in periods)


@pytest.mark.asyncio
async def test_missing_historical_window_makes_whole_snapshot_incomplete(monkeypatch):
    promotion = Promotion(snapshot(10, 1, 3), snapshot(_has_data=False))
    service = service_for(monkeypatch, promotion)
    totals = await service._fullstats_windowed(promotion, 777, date(2026, 8, 1))
    assert totals["_has_data"] and not totals["_data_complete"]
    assert ABTestService._validated_stats(totals)[0] == "incomplete"


@pytest.mark.asyncio
async def test_minimum_bid_growth_pauses_before_target_without_changing_approval(monkeypatch):
    promotion = Promotion(snapshot(20, 1, 6), minimum=900)
    service = service_for(monkeypatch, promotion)
    test = experiment()
    await service._sync_loaded(test)
    assert test.campaign_state == "paused"
    assert test.status == ABTestStatus.RUNNING
    assert test.cpm_rub == 300 and test.budget_rub == 5000
    assert test.current_variant_order == 1 and test.stage_views == 20
    assert test.started_at == NOW - timedelta(hours=1)
    assert test.media_state["minimum_bid_pause"]["minimum_cpm"] == 900
    await service._sync_loaded(test)
    assert len(promotion.calls) == 1
    promotion.start_campaign.assert_not_awaited()
    service._stop_campaign_confirmed.assert_not_awaited()
    service.db.commit.assert_awaited()


def test_budget_forecast_funding_and_reserve_are_separate_and_exact():
    assert calculate_protected_budget(5, 1000, 500, 300) == 2800
    assert calculate_budget_breakdown(3, 333, 333, 300) == {
        "forecast_spend_rub": 332.67, "minimum_campaign_budget_rub": 1200,
        "funding_step_rub": 100, "base_funding_rub": 1200, "safety_reserve_rub": 300,
        "funding_required_rub": 1500, "stop_threshold_rub": 1200, "funding_rounding_rub": 0,
    }


@pytest.mark.asyncio
async def test_dashboard_zero_ctr_and_incomplete_quality_are_truthful(monkeypatch):
    test = experiment(stats_quality="no_data")
    service = DashboardService(SimpleNamespace())
    service.repository.get_for_user = AsyncMock(return_value=test.connection)
    monkeypatch.setattr("app.services.dashboard_service.ABTestRepository.list_for_user", AsyncMock(return_value=[test]))
    result = await service.stats(1, 1, period="custom", begin_date=date(2026, 9, 1), end_date=date(2026, 9, 30))
    assert result["ctr"] is None
    assert result["stats_complete"] is False

@pytest.mark.asyncio
async def test_background_scheduler_workers_are_independent(monkeypatch):
    """A long-running reconciliation for one test must not block other tests."""
    import app.services.ab_test_service as service_module
    import app.repositories.ab_test_repository as repo_module
    from app.services.ab_test_service import _SCHEDULER_TASKS

    _SCHEDULER_TASKS.clear()
    started: list[int] = []
    release = asyncio.Event()

    class SessionContext:
        async def __aenter__(self):
            return SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
        async def __aexit__(self, exc_type, exc, tb):
            return False

    candidates = [SimpleNamespace(user_id=1, id=i) for i in range(1, 7)]

    async def fake_list_running(self):
        return list(candidates)

    async def fake_list_unresolved(self):
        return []

    async def fake_get_for_user(self, user_id, test_id):
        return SimpleNamespace(
            id=test_id,
            user_id=user_id,
            connection_id=test_id,
            nm_id=1000 + test_id,
            status=ABTestStatus.RUNNING,
        )

    async def fake_sync(self, test, semaphore):
        started.append(test.id)
        await release.wait()

    monkeypatch.setattr(service_module, "AsyncSessionLocal", lambda: SessionContext())
    monkeypatch.setattr(service_module.ABTestService, "recover_unfinished_operations", AsyncMock())
    monkeypatch.setattr(service_module.ABTestService, "_scheduler_sync_one", fake_sync)
    monkeypatch.setattr(repo_module.ABTestRepository, "list_running", fake_list_running)
    monkeypatch.setattr(repo_module.ABTestRepository, "list_unresolved_campaigns", fake_list_unresolved)
    monkeypatch.setattr(repo_module.ABTestRepository, "get_for_user", fake_get_for_user)

    service = ABTestService(SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock()))
    await service.scheduler_tick(background=True)
    await asyncio.sleep(0.05)

    assert sorted(started) == [1, 2, 3, 4, 5, 6]
    assert len(_SCHEDULER_TASKS) == 6

    release.set()
    await asyncio.gather(*list(_SCHEDULER_TASKS.values()))
    _SCHEDULER_TASKS.clear()

@pytest.mark.asyncio
async def test_post_stop_settlement_keeps_late_aggregate_unallocated(monkeypatch):
    """Provider totals observed after STOP must never be attributed to the last photo."""
    promotion = Promotion(snapshot(1450, 87, 452.4), snapshot(1450, 87, 452.4))
    service = service_for(monkeypatch, promotion)
    test = experiment(
        status=ABTestStatus.STOPPED,
        campaign_state="stopped",
        current_variant_order=2,
        settled_total_views=1200,
        settled_total_clicks=80,
        settled_total_spend_rub=400,
    )
    test.variants[0].views = 1200
    test.variants[0].clicks = 80
    test.variants[0].spend_rub = 400
    test.variants[1].views = 0
    test.variants[1].clicks = 0
    test.variants[1].spend_rub = 0
    await service._settle_stage_stats(test, promotion, attempts=2, post_stop=True)
    assert test.variants[1].views == 0
    assert test.variants[1].clicks == 0
    assert test.unallocated_views == 250
    assert test.unallocated_clicks == 7
    assert test.stage_views == 0
    assert test.stage_clicks == 0


@pytest.mark.asyncio
async def test_high_traffic_budget_guard_forecasts_missing_provider_lag(monkeypatch):
    """A 600 views/min stage must reserve the configured delayed-statistics window."""
    from app.core.config import settings
    old = settings.ab_test_budget_guard_reserve_rub
    settings.ab_test_budget_guard_reserve_rub = 300
    try:
        promotion = Promotion(snapshot(600, 30, 180))
        service = service_for(monkeypatch, promotion)
        test = experiment(
            budget_rub=1000,
            cpm_rub=300,
            settled_total_views=0,
            settled_total_spend_rub=0,
            last_total_views=0,
            stage_started_at=NOW - timedelta(minutes=1),
        )
        await service._sync_loaded(test)
        assert service._finish_loaded.await_count == 1
        assert test.status == ABTestStatus.STOPPED
        assert "защитный резерв" in (test.last_error or "")
    finally:
        settings.ab_test_budget_guard_reserve_rub = old
