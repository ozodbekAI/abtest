"""Regression tests for the af42ac audit defects A1-A3 (28.09.2026)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.models.ab_test import ABTestStatus
from app.services.ab_test_service import ABTestService
from tests.test_scheduler_and_similarity_fixes import (
    FakeContent, hash_table, make_service, running_test, service_with, h,
)


def bits(flips: int) -> str:
    return ABTestService._hash_to_bits(h(flips))


async def verify_j(svc, cdn, expected, *, previous_phash=None, require_previous=False):
    test = SimpleNamespace(id=1, nm_id=1)
    return await svc._verify_uploaded_image(
        test=test, slot=1, expected_data=expected, content=FakeContent(cdn),
        attempts=1, delay_seconds=0, threshold=10,
        previous_phash=previous_phash, require_previous=require_previous,
    )


# ---- A1: journaled fingerprint survives deletion of the old local file
async def test_a1_journaled_previous_rejects_stale_similar_photo(monkeypatch):
    hash_table(monkeypatch, {b"new": 7, b"cdn-old": 1, b"cdn-new": 6})
    svc = make_service()
    # old photo hash = 0 flips (journaled); CDN still serves the old one
    assert await verify_j(svc, b"cdn-old", b"new", previous_phash=bits(0), require_previous=True) is False
    assert await verify_j(svc, b"cdn-new", b"new", previous_phash=bits(0), require_previous=True) is True


async def test_a1_unknown_previous_is_not_confirmed_when_required(monkeypatch):
    hash_table(monkeypatch, {b"new": 7, b"cdn-old": 1})
    assert await verify_j(make_service(), b"cdn-old", b"new", previous_phash=None, require_previous=True) is False


async def test_a1_indistinguishable_old_and_new_is_not_confirmed(monkeypatch):
    hash_table(monkeypatch, {b"new": 2, b"cdn": 2})
    assert await verify_j(make_service(), b"cdn", b"new", previous_phash=bits(0), require_previous=True) is False


async def test_a1_same_picture_already_in_slot_is_confirmed(monkeypatch):
    # e.g. winner equals the last stage's photo: nothing to tell apart
    hash_table(monkeypatch, {b"new": 5, b"cdn": 5})
    assert await verify_j(make_service(), b"cdn", b"new", previous_phash=bits(5), require_previous=True) is True


def test_a1_slot_fingerprint_is_taken_from_state(monkeypatch, tmp_path):
    svc = make_service()
    hash_table(monkeypatch, {b"A": 3})
    svc._read_state_slot = lambda state, slot, shadow=True: (b"A", "image/jpeg", "a.jpg")
    assert svc._slot_phash_bits({}, 1) == bits(3)
    svc._read_state_slot = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("gone"))
    assert svc._slot_phash_bits({}, 1) is None


# ---- A2: restore must not accept the similar test photo left in the slot
async def test_a2_restore_rejects_similar_test_photo(monkeypatch):
    hash_table(monkeypatch, {b"ORIG": 0, b"cdn-upl": 6, b"cdn-orig": 1})
    svc = make_service()
    # test photo (journaled) = 6 flips; CDN still shows it, distance to ORIG=6<=10
    assert await verify_j(svc, b"cdn-upl", b"ORIG", previous_phash=bits(6)) is False
    assert await verify_j(svc, b"cdn-orig", b"ORIG", previous_phash=bits(6)) is True


# ---- A3: failed + running/stopped campaign still owes a card restore
async def test_a3_failed_running_campaign_gets_finish_and_restore():
    calls: list = []
    test = running_test(status=ABTestStatus.FAILED, campaign_state="running",
                        operation_state="reconciliation_required", media_status="variant_applied")
    svc = service_with(test, calls)
    await svc._scheduler_safety_one(test, asyncio.Semaphore(1))
    assert ("finish", True) in calls


async def test_a3_stopped_campaign_with_unrestored_card_is_still_recovered():
    calls: list = []
    test = running_test(status=ABTestStatus.FAILED, campaign_state="stopped",
                        operation_state="reconciliation_required", media_status="variant_applied")
    svc = service_with(test, calls)
    await svc._scheduler_safety_one(test, asyncio.Semaphore(1))
    assert ("finish", True) in calls


async def test_a3_external_conflict_is_not_retried():
    calls: list = []
    test = running_test(status=ABTestStatus.FAILED, campaign_state="paused",
                        operation_state="reconciliation_required", media_status="external_conflict")
    svc = service_with(test, calls)
    await svc._scheduler_safety_one(test, asyncio.Semaphore(1))
    assert ("finish", True) not in calls
