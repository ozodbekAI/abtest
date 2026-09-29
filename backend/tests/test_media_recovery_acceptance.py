from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models.ab_test import ABTest, ABTestOperationStatus, ABTestStatus
from app.services.ab_test_service import ABTestReconciliationRequired, ABTestService


NOW = datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc)


class Content:
    async def get_card(self, nm_id):
        return {"nmID": nm_id, "photos": [{"c516x688": {"url": "https://cdn/original.jpg"}}]}


class Promotion:
    async def get_campaign_status(self, campaign_id, refresh=True):
        return 9


@pytest.mark.asyncio
async def test_pending_restore_is_replayed_after_worker_restart(monkeypatch):
    db = SimpleNamespace(commit=AsyncMock(), flush=AsyncMock())
    service = ABTestService(db)
    test = ABTest(
        id=1, user_id=1, connection_id=1, nm_id=123, title="restore",
        status=ABTestStatus.FAILED, wb_campaign_id=777,
        skip_current_photo=True, keep_winner_as_main=False,
        delete_test_media=False, views_per_variant=300, cpm_rub=300,
        budget_rub=1200, current_variant_order=1,
        campaign_state="running", operation_state=ABTestOperationStatus.RECONCILIATION_REQUIRED.value,
        media_status="restoring", stats_quality="preliminary", incident_id=None,
        media_state={
            "version": 1,
            "backups": {"1": {"url": "https://cdn/original.jpg", "path": "backup-1.jpg", "mime": "image/jpeg", "file_name": "original.jpg"}},
            "touched": [1],
            "pending": {"kind": "restore", "slots": [1], "targets": {"1": {"original_url": "https://cdn/original.jpg", "shadow_path": "backup-1.jpg"}}},
            "status": "restoring",
            "expected_slot_count": 1,
            "expected_snapshot": {"1": {"url": "https://cdn/test.jpg"}},
        },
    )
    monkeypatch.setattr(service, "_clients", AsyncMock(return_value=(Content(), Promotion())))
    monkeypatch.setattr(service, "_restore_original", AsyncMock(side_effect=lambda t, c: setattr(t, "media_status", "restored")))
    monkeypatch.setattr(service, "_stop_campaign_confirmed", AsyncMock())
    await service._sync_loaded(test)
    service._stop_campaign_confirmed.assert_awaited_once()
    service._restore_original.assert_awaited_once_with(test, service._clients.return_value[0])
    assert test.status == ABTestStatus.FINISHED
    assert test.operation_state == ABTestOperationStatus.SUCCEEDED.value
    assert test.media_status == "restored"


@pytest.mark.asyncio
async def test_restore_verification_failure_keeps_reconciliation_open(monkeypatch):
    db = SimpleNamespace(commit=AsyncMock(), flush=AsyncMock())
    service = ABTestService(db)
    test = ABTest(
        id=1, user_id=1, connection_id=1, nm_id=123, title="restore",
        status=ABTestStatus.FAILED, wb_campaign_id=777,
        skip_current_photo=True, keep_winner_as_main=False,
        delete_test_media=False, views_per_variant=300, cpm_rub=300,
        budget_rub=1200, current_variant_order=1,
        campaign_state="stopped", operation_state=ABTestOperationStatus.RECONCILIATION_REQUIRED.value,
        media_status="restoring", stats_quality="preliminary", incident_id=None,
        media_state={
            "version": 1,
            "backups": {"1": {"url": "https://cdn/original.jpg", "path": "backup-1.jpg", "mime": "image/jpeg", "file_name": "original.jpg"}},
            "touched": [1],
            "pending": {"kind": "restore", "slots": [1], "targets": {"1": {"original_url": "https://cdn/original.jpg", "shadow_path": "backup-1.jpg"}}},
            "status": "restoring",
        },
    )
    monkeypatch.setattr(service, "_clients", AsyncMock(return_value=(Content(), Promotion())))
    monkeypatch.setattr(service, "_restore_original", AsyncMock(side_effect=ABTestReconciliationRequired("restore not confirmed", incident_id="INC1")))
    with pytest.raises(ABTestReconciliationRequired):
        await service._sync_loaded(test)
    assert test.operation_state == ABTestOperationStatus.RECONCILIATION_REQUIRED.value
    assert test.incident_id.startswith("INC-")
    assert test.status == ABTestStatus.FAILED
