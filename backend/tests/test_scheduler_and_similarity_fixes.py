"""Regression tests for the 28.09.2026 review findings (1, 2, 4, 6).

pHash values are controlled explicitly (bytes -> hash table) so distances are
deterministic and independent of image content.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

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


async def test_verify_without_previous_keeps_old_behaviour(monkeypatch):
    hash_table(monkeypatch, {b"new": 7, b"cdn-old": 1})
    assert await verify(make_service(), b"cdn-old", b"new", None) is True


# ---------------------------------------------------------------- FIX 1 / 2
def running_test(**over):
    base = dict(
        id=1, user_id=1, connection_id=1, nm_id=1, wb_campaign_id=555,
        status=ABTestStatus.RUNNING, campaign_state="running",
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
    # A3: a failed test with a live campaign owes BOTH stop and card restore;
    # _finish_loaded(stopped=True) performs the confirmed stop and the restore.
    assert ("finish", True) in calls


async def test_fix1_running_but_abandoned_operation_is_still_handled():
    calls: list = []
    test = running_test(operation_state="in_progress")
    svc = service_with(test, calls)
    import asyncio
    await svc._scheduler_safety_one(test, asyncio.Semaphore(1))
    assert "stop" in calls


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
