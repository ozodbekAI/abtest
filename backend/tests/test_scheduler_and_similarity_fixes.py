"""Regression tests for the 28.09.2026 review findings (1, 2, 4, 6).

pHash values are controlled explicitly (bytes -> hash table) so distances are
deterministic and independent of image content.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models.ab_test import ABTest, ABTestStatus
from app.services.ab_test_service import ABTestService
from app.utils.imagehash_compat import ImageHash

N = 255


def h(flips: int) -> ImageHash:
    """Hash at Hamming distance `flips` from the all-zero hash."""
    return ImageHash(tuple(1 if i < flips else 0 for i in range(N)))


def hash_table(monkeypatch, mapping: dict[bytes, int]):
    monkeypatch.setattr(ABTestService, "_perceptual_hash", staticmethod(lambda data: h(mapping[data])))


# ---------------------------------------------------------------- FIX 4
def test_relation_exact_recompressed_similar_distinct(monkeypatch):
    # A = 0 flips; a re-encoded copy: 2; another pose: 7; a different photo: 60
    hash_table(monkeypatch, {b"A": 0, b"A-recompressed": 2, b"pose": 7, b"other": 60})
    assert ABTestService._image_relation(b"A", b"A") == ("identical", 0)
    assert ABTestService._image_relation(b"A", b"A-recompressed") == ("indistinguishable", 2)
    assert ABTestService._image_relation(b"A", b"pose") == ("similar", 7)
    assert ABTestService._image_relation(b"A", b"other") == ("distinct", 60)


def test_similar_variant_warning_persists_on_model_state():
    test = SimpleNamespace(media_state={})
    ABTestService._mark_similar_variant_warning(test, 2)
    assert test.media_state["similar_variant_positions"] == [2]
    assert test.media_state["similar_variant_warning"] is True


@pytest.mark.asyncio
async def test_initialize_media_state_preserves_preflight_similar_warning(tmp_path, monkeypatch):
    # start() may detect a similar upload before the durable media state is
    # initialized. Initialization must not erase that explicit paid-start
    # confirmation signal.
    service = ABTestService.__new__(ABTestService)
    service.db = SimpleNamespace(flush=AsyncMock())
    monkeypatch.setattr(service, "_media_root", lambda: tmp_path)
    monkeypatch.setattr(service, "_prepare_image", lambda filename, mime, data: (filename, mime, data))

    class Content:
        async def download_image(self, url, cache_bust=False):
            return b"original", "image/jpeg"

        async def get_card(self, nm_id):
            return {"photos": [{"big": "http://cdn/x.jpg"}], "videos": []}

    test = SimpleNamespace(
        id=1,
        nm_id=1,
        skip_current_photo=True,
        media_state={"similar_variant_positions": [1], "similar_variant_warning": True},
        original_media=None,
        original_main_backup_path=None,
        media_status=None,
    )

    state = await service._initialize_media_state(
        test, ["http://cdn/x.jpg"], [], Content()
    )

    assert state["similar_variant_positions"] == [1]
    assert state["similar_variant_warning"] is True


class FakeContent:
    """CDN that keeps serving `serving` (a key of the hash table)."""

    def __init__(self, serving: bytes):
        self.serving = serving

    async def get_card(self, nm_id):
        return {"photos": [{"big": "http://cdn/x.jpg"}]}

    async def download_image(self, url, cache_bust=False):
        return self.serving, "image/jpeg"


def make_service():
    svc = ABTestService.__new__(ABTestService)
    return svc


async def verify(svc, cdn_serving, expected, previous):
    test = SimpleNamespace(id=1, nm_id=1)
    return await svc._verify_uploaded_image(
        test=test, slot=1, expected_data=expected, content=FakeContent(cdn_serving),
        attempts=1, delay_seconds=0, threshold=10, previous_data=previous,
    )


async def test_verify_similar_pose_rejects_stale_old_image(monkeypatch):
    # old=0 flips, new pose=7 flips away from old. CDN still serves the OLD image.
    hash_table(monkeypatch, {b"old": 0, b"new": 7, b"cdn-old": 1, b"cdn-new": 6})
    svc = make_service()
    # before the fix "cdn-old" (distance 6 to new) would be accepted as installed
    assert await verify(svc, b"cdn-old", b"new", b"old") is False
    assert await verify(svc, b"cdn-new", b"new", b"old") is True




async def test_verify_similar_photo_uses_exact_bytes_to_reject_old_and_accept_new(monkeypatch):
    # old/new are intentionally pHash-close. The CDN serving old bytes must be
    # rejected even when the pHash distance is within threshold; exact expected
    # bytes must be accepted.
    hash_table(monkeypatch, {b"old": 0, b"new": 6})
    svc = make_service()
    assert await verify(svc, b"old", b"new", b"old") is False
    assert await verify(svc, b"new", b"new", b"old") is True

async def test_verify_without_previous_keeps_old_behaviour(monkeypatch):
    hash_table(monkeypatch, {b"new": 7, b"cdn-old": 1})
    assert await verify(make_service(), b"cdn-old", b"new", None) is True


# ---------------------------------------------------------------- FIX 1 / 2
def running_test(**over):
    base = dict(
        id=1, user_id=1, connection_id=1, nm_id=1, wb_campaign_id=555,
        status=ABTestStatus.RUNNING, campaign_state="running", media_status="variant_applied",
        operation_state="succeeded", incident_id=None, lifecycle_lock=True, last_error=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def service_with(test, calls):
    svc = ABTestService.__new__(ABTestService)

    async def get_for_update(user_id, test_id):
        return test

    async def commit():
        calls.append("commit")

    @asynccontextmanager
    async def lock(*a, **k):
        yield True

    async def connection(*a, **k):
        calls.append("connection")
        return SimpleNamespace()

    promotion = SimpleNamespace()

    async def clients(conn):
        return SimpleNamespace(), promotion

    async def finish(t, content, promo, *, stopped=False):
        calls.append(("finish", stopped))
        t.status = ABTestStatus.STOPPED

    async def stop_confirmed(t, promo):
        calls.append("stop")
        t.campaign_state = "stopped"

    async def assert_cfg(t, promo):
        return None

    async def get_status(cid, refresh=False):
        return 9

    promotion.get_campaign_status = get_status
    svc.repository = SimpleNamespace(get_for_update=get_for_update)
    svc.db = SimpleNamespace(commit=commit)
    svc._operation_lock = lock
    svc._connection_or_404 = connection
    svc._clients = clients
    svc._finish_loaded = finish
    svc._stop_campaign_confirmed = stop_confirmed
    svc._assert_campaign_configuration = assert_cfg
    return svc


async def test_fix1_healthy_running_campaign_is_not_stopped():
    calls: list = []
    test = running_test()
    svc = service_with(test, calls)
    import asyncio
    await svc._scheduler_safety_one(test, asyncio.Semaphore(1))
    assert "stop" not in calls and "connection" not in calls  # returned before any WB action
    assert test.campaign_state == "running" and test.status == ABTestStatus.RUNNING


async def test_fix1_failed_test_with_active_campaign_is_still_stopped():
    calls: list = []
    test = running_test(status=ABTestStatus.FAILED, operation_state="reconciliation_required")
    svc = service_with(test, calls)
    import asyncio
    await svc._scheduler_safety_one(test, asyncio.Semaphore(1))
    assert ("finish", True) in calls  # safety finish stops and restores media


async def test_fix1_running_but_abandoned_operation_is_still_handled():
    calls: list = []
    test = running_test(operation_state="in_progress")
    svc = service_with(test, calls)
    import asyncio
    await svc._scheduler_safety_one(test, asyncio.Semaphore(1))
    assert ("finish", True) in calls


async def test_fix2_interrupted_swap_with_paused_campaign_gets_finish_and_restore():
    calls: list = []
    test = running_test(status=ABTestStatus.FAILED, campaign_state="paused",
                        operation_state="reconciliation_required")
    svc = service_with(test, calls)
    import asyncio
    await svc._scheduler_safety_one(test, asyncio.Semaphore(1))
    assert ("finish", True) in calls
    assert test.status == ABTestStatus.STOPPED


async def test_fix2_paused_recovery_failure_keeps_incident_message():
    calls: list = []
    test = running_test(status=ABTestStatus.FAILED, campaign_state="paused",
                        operation_state="reconciliation_required")
    svc = service_with(test, calls)

    async def failing_finish(t, content, promo, *, stopped=False):
        t.last_error = "Инцидент X: восстановление фото не подтверждено"
        raise RuntimeError("boom")

    svc._finish_loaded = failing_finish
    import asyncio
    await svc._scheduler_safety_one(test, asyncio.Semaphore(1))
    assert test.status == ABTestStatus.FAILED
    assert "Инцидент X" in test.last_error


def test_fix2_default_timeout_covers_a_two_slot_swap():
    from app.core.config import Settings
    worst_swap = 2 * (30 + (ABTestService.IMAGE_VERIFY_ATTEMPTS - 1) * ABTestService.IMAGE_VERIFY_DELAY_SECONDS)
    assert Settings().ab_test_scheduler_per_test_timeout_sec > worst_swap

@pytest.mark.asyncio
async def test_safety_sweep_does_not_wait_for_busy_card_lock():
    """Emergency sweep returns immediately when a normal worker owns the lock."""
    import asyncio
    calls: list[str] = []
    test = running_test(status=ABTestStatus.FAILED, operation_state="reconciliation_required")
    svc = service_with(test, calls)

    @asynccontextmanager
    async def busy_lock(*args, **kwargs):
        assert kwargs.get("wait") is False
        yield False

    svc._operation_lock = busy_lock
    started = asyncio.get_running_loop().time()
    await svc._scheduler_safety_one(test, asyncio.Semaphore(1))
    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < 0.1
    assert calls == []

@pytest.mark.asyncio
async def test_manual_stop_does_not_wait_behind_busy_worker_lock():
    """A user stop becomes a durable request instead of hanging behind reconciliation."""
    from types import SimpleNamespace

    test = running_test()
    calls: list[str] = []
    svc = ABTestService.__new__(ABTestService)

    class DB:
        async def commit(self):
            calls.append("commit")

    async def get_for_user(user_id, test_id):
        return test

    @asynccontextmanager
    async def busy_lock(*args, **kwargs):
        assert kwargs.get("wait") is False
        yield False

    svc.repository = SimpleNamespace(get_for_user=get_for_user)
    svc.db = DB()
    svc._operation_lock = busy_lock
    svc._incident_id = lambda: "INC-STOP"
    svc._now = lambda: NOW

    result = await svc.stop(1, 1)
    assert result is test
    assert test.operation_state == "stop_requested"
    assert test.campaign_state == "stop_requested"
    assert test.incident_id == "INC-STOP"
    assert "commit" in calls

@pytest.mark.asyncio
async def test_stop_checkpoint_reads_fresh_db_state_and_finishes_safely():
    """A stale ORM object must not hide a concurrent durable STOP request."""
    import asyncio
    test = running_test(operation_state="succeeded", campaign_state="running", stats_quality="preliminary", current_variant_order=1, winner_variant_order=None)
    calls = []

    class Result:
        def one_or_none(self):
            return ("stop_requested", "stop_requested")

    class DB:
        async def execute(self, stmt):
            return Result()

        async def commit(self):
            calls.append("commit")

        def scalar(self, stmt):
            return None

    svc = ABTestService.__new__(ABTestService)
    svc.db = DB()
    svc._finish_loaded = AsyncMock(side_effect=lambda *a, **k: calls.append(("finish", k.get("stopped"))))
    svc._audit = AsyncMock()

    assert await svc._stop_requested_checkpoint(
        test, SimpleNamespace(), SimpleNamespace(), reason="test-race"
    ) is True
    assert ("finish", True) in calls
    svc._audit.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_is_blocked_while_post_stop_reconciliation_is_pending():
    from fastapi import HTTPException

    test = running_test(
        status=ABTestStatus.STOPPED,
        operation_state="post_stop_reconciliation",
        campaign_state="stopped",
        lifecycle_lock=False,
        media_status="restored",
    )
    svc = ABTestService.__new__(ABTestService)

    async def get_for_user(user_id, test_id):
        return test

    async def get_for_update(user_id, test_id):
        return test

    async def get_connection(user_id, connection_id, **kwargs):
        return SimpleNamespace(seller_id="seller-1")

    @asynccontextmanager
    async def lock(*args, **kwargs):
        yield True

    svc.repository = SimpleNamespace(get_for_user=get_for_user, get_for_update=get_for_update, get_connection=get_connection)
    svc._operation_lock = lock
    with pytest.raises(HTTPException) as exc_info:
        await svc.delete(1, 1)
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_stop_checkpoint_after_campaign_start_handles_race_without_continuing():
    test = running_test(operation_state="succeeded", campaign_state="running")
    calls = []

    class Result:
        def one_or_none(self):
            return ("succeeded", "running")

    class DB:
        async def execute(self, stmt):
            return Result()

        async def commit(self):
            calls.append("commit")

    svc = ABTestService.__new__(ABTestService)
    svc.db = DB()
    svc._finish_loaded = AsyncMock(side_effect=lambda *a, **k: calls.append(("finish", k.get("stopped"))))
    svc._audit = AsyncMock()
    assert await svc._stop_requested_checkpoint(
        test, SimpleNamespace(), SimpleNamespace(), reason="no-race"
    ) is False
    assert calls == []

@pytest.mark.asyncio
async def test_manual_reconcile_post_stop_runs_without_holding_card_lock():
    test = running_test(
        status=ABTestStatus.STOPPED,
        operation_state="post_stop_reconciliation",
        campaign_state="stopped",
        media_status="restored",
        stats_quality="preliminary",
        current_variant_order=2,
        winner_variant_order=None,
    )
    svc = ABTestService.__new__(ABTestService)
    calls = []

    async def get_for_user(user_id, test_id):
        return test

    async def get_for_update(user_id, test_id):
        return test

    async def get_connection(user_id, connection_id, **kwargs):
        return SimpleNamespace(seller_id="seller-1")

    class Promotion:
        async def get_campaign_status(self, campaign_id, refresh=False):
            return 7

    async def clients(connection):
        return SimpleNamespace(), Promotion()

    @asynccontextmanager
    async def lock(*args, **kwargs):
        calls.append("lock")
        yield True

    async def post_stop(test_obj, promotion):
        calls.append("post_stop")
        test_obj.operation_state = "succeeded"

    class DB:
        async def commit(self):
            calls.append("commit")

    svc.repository = SimpleNamespace(
        get_for_user=get_for_user,
        get_for_update=get_for_update,
        get_connection=get_connection,
    )
    svc._operation_lock = lock
    svc._connection_or_404 = get_connection
    svc._clients = clients
    svc._post_stop_reconcile = post_stop
    svc._finish_loaded = AsyncMock()
    svc.db = DB()

    result = await svc.reconcile(1, 1)
    assert result is test
    assert calls.count("lock") == 1
    assert "post_stop" in calls
    svc._finish_loaded.assert_not_awaited()


def test_fullstats_lock_uses_dedicated_nullpool_session():
    from app.core import database
    import inspect
    from app.services import wb_rate_limiter

    assert database.StatsLockSessionLocal is not database.AsyncSessionLocal
    assert "StatsLockSessionLocal" in inspect.getsource(wb_rate_limiter.WBRateLimiter.stats_lock)
