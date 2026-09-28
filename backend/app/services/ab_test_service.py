from __future__ import annotations

from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import asyncio
from zoneinfo import ZoneInfo
import base64
import hashlib
import hmac
import io
import json
import re
from app.utils.imagehash_compat import ImageHash
from PIL import Image
import logging
import math
import mimetypes
import secrets
import struct
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from fastapi import HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.ab_test import ABTest, ABTestAuditEvent, ABTestOperation, ABTestOperationStatus, ABTestStatus, ABTestVariant
from app.repositories.ab_test_repository import ABTestRepository
from app.schemas.ab_test import ABTestCreateRequest, ABTestStartRequest
from app.services.ab_test_budget import DEFAULT_SAFETY_RESERVE_RUB, calculate_protected_budget, calculate_required_budget
from app.services.wb_content_client import WBApiError, WBContentClient
from app.services.wb_promotion_client import WBPromotionClient
from app.services.wb_rate_limiter import WBRateLimiter
from app.services.wb_token_service import WBTokenService
from app.services.email_service import EmailService


logger = logging.getLogger(__name__)


class ABTestReconciliationRequired(RuntimeError):
    """The last external effect may have happened, but cannot be proven yet."""

    def __init__(self, message: str, *, incident_id: str | None = None):
        super().__init__(message)
        self.incident_id = incident_id


class ABCampaignProductMismatch(ABTestReconciliationRequired):
    """A campaign was confirmed, but it contains another product."""


class ABTestService:
    MAX_IMAGE_BYTES = 32 * 1024 * 1024

    IMAGE_VERIFY_ATTEMPTS = 5
    IMAGE_VERIFY_DELAY_SECONDS = 60

    IMAGE_REUPLOAD_DELAY_SECONDS = 3600

    IMAGE_PHASH_THRESHOLD = 10

    IMAGE_VERIFY_MAX_REUPLOADS = 1
    # A/B experiments are intentionally limited to 2–5 tested images.  The
    # product-card API supports up to 30 media slots, but those are not 30
    # statistically comparable experiment variants. Keeping this limit in the
    # service also protects direct API callers that bypass the wizard.
    MAX_VARIANTS = 5
    ALLOWED_IMAGE_TYPES = {
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/bmp",
        "image/gif",
        "image/tiff",
        "image/tif",
        "image/x-tiff",
    }
    ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
    TIFF_TYPES = {"image/tiff", "image/tif", "image/x-tiff"}
    TIFF_SIGNATURES = (b"II*\x00", b"MM\x00*")
    # Promotion API statuses which are known not to be serving impressions.
    # Status 7 is especially important: a campaign can finish by itself
    # before our cleanup request reaches WB. ``-1`` is a deleted/pending
    # deletion state: it is safe to regard it as non-serving, but it must
    # never be reused for a retry because WB cannot start it again.
    NON_ACTIVE_CAMPAIGN_STATUSES = {-1, 4, 7, 8, 11}
    RESUMABLE_CAMPAIGN_STATUSES = {4, 11}
    WINNER_MIN_VARIANTS = 2
    WINNER_MIN_IMPRESSIONS = 300
    WINNER_MIN_CTR_DELTA = 0.35
    CAMPAIGN_ACTIVE_CONFIRM_DELAYS = (0.0, 1.0, 2.0, 4.0, 8.0, 15.0)
    CAMPAIGN_STOP_CONFIRM_DELAYS = (0.0, 1.0, 2.0, 4.0, 8.0, 15.0)
    CAMPAIGN_PAUSE_CONFIRM_DELAYS = (0.0, 1.0, 2.0, 4.0, 8.0, 15.0)
    CAMPAIGN_BUDGET_CONFIRM_DELAYS = (0.0, 1.0, 2.0, 4.0, 8.0, 15.0)
    CAMPAIGN_DETAILS_CONFIRM_DELAYS = (0.0, 1.0, 2.0, 4.0, 8.0, 15.0, 30.0)
    MAX_STATS_STALE_SECONDS = 300
    MIN_NO_PROGRESS_TIMEOUT_SECONDS = 3600

    def __init__(self, db: AsyncSession):
        self.db = db
        self.repository = ABTestRepository(db)

    async def dispatch_incident_notifications(self) -> None:
        """Materialize and deliver all durable safety incident alerts."""
        rows = await self.db.scalars(
            select(ABTest)
            .options(selectinload(ABTest.connection))
            .where(ABTest.incident_id.is_not(None))
            .order_by(ABTest.id.asc())
        )
        for test in rows.all():
            try:
                await self.repository.enqueue_incident_notification(test)
            except IntegrityError:
                await self.db.rollback()
        await self.db.commit()
        pending = await self.repository.list_pending_incident_notifications(limit=20)
        email = EmailService()
        for notification in pending:
            notification.attempts = int(notification.attempts or 0) + 1
            try:
                await email.send_incident(
                    recipient=notification.recipient,
                    incident_id=notification.incident_id,
                    subject=notification.subject,
                    body=notification.body,
                )
            except Exception as exc:
                notification.status = "failed"
                notification.last_error = self._safe_error(exc)[:2000]
                notification.next_attempt_at = self._now() + timedelta(seconds=min(3600, 30 * (2 ** min(notification.attempts, 7))))
            else:
                notification.status = "sent"
                notification.sent_at = self._now()
                notification.last_error = None
            await self.db.commit()

    @staticmethod
    def _perceptual_hash(data: bytes) -> ImageHash:
        """Calculate a deterministic perceptual hash for an image."""
        from app.utils.imagehash_compat import phash
        return phash(data)

    @staticmethod
    def _image_hash_distance(hash1: ImageHash, hash2: ImageHash) -> int:
        """Return Hamming distance between two perceptual hashes."""
        return hash1 - hash2

    @classmethod
    def _images_similar(
        cls,
        data1: bytes,
        data2: bytes,
        *,
        threshold: int = 10,
    ) -> tuple[bool, int]:
        """
        Compare two images using perceptual hash.

        Returns:
            (is_similar, hamming_distance)
        """
        hash1 = cls._perceptual_hash(data1)
        hash2 = cls._perceptual_hash(data2)

        distance = cls._image_hash_distance(hash1, hash2)

        return distance <= threshold, distance


    @staticmethod
    def _required_budget_for_test(test: ABTest) -> int:
        """Calculate the same protected budget shown by the UI.

        A newly-created draft can briefly have no variants while the wizard is
        uploading them. In that case keep its stored preview value until the
        real variant list is available.
        """

        tested_variant_count = sum(variant.source_type != "control" for variant in test.variants)
        tested_variant_count += 0 if test.skip_current_photo else 1
        if tested_variant_count <= 0:
            return int(test.budget_rub or 0)
        return calculate_protected_budget(
            tested_variant_count,
            test.views_per_variant,
            test.cpm_rub,
            getattr(settings, "ab_test_budget_guard_reserve_rub", DEFAULT_SAFETY_RESERVE_RUB),
        )

    @classmethod
    def _validate_test_variant_count(cls, variant_count: int, *, skip_current_photo: bool) -> int:
        """Validate the final number of images compared by an experiment."""
        tested_variant_count = int(variant_count) + (0 if skip_current_photo else 1)
        if tested_variant_count < cls.WINNER_MIN_VARIANTS:
            raise HTTPException(status_code=400, detail="Добавьте минимум 2 изображения для сравнения")
        if tested_variant_count > cls.MAX_VARIANTS:
            raise HTTPException(status_code=400, detail="В одном тесте может быть от 2 до 5 изображений")
        return tested_variant_count

    @staticmethod
    def _incident_id() -> str:
        return f"INC-{secrets.token_hex(6).upper()}"

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        """Keep provider payloads and credentials out of persisted errors."""
        if isinstance(exc, HTTPException):
            detail = exc.detail
            if isinstance(detail, dict) and detail.get("message"):
                return str(detail["message"])[:500]
            if isinstance(detail, str) and detail.strip():
                return detail.strip()[:500]
            return f"HTTP {exc.status_code}"
        if isinstance(exc, WBApiError):
            if exc.status_code == 401:
                return "Wildberries отклонил запрос: токен недействителен или просрочен"
            if exc.status_code == 403:
                return "Wildberries отклонил запрос: у токена недостаточно доступа"
            if exc.status_code == 429:
                return "Wildberries временно ограничил частоту запросов"
            provider_message = WBPromotionClient._error_message(exc.payload)
            if provider_message and provider_message.lower() not in {
                "bad request",
                "неправильный запрос",
                "internal server error",
            }:
                return f"Wildberries: {provider_message}"[:500]
            if exc.status_code and 400 <= exc.status_code < 500:
                return f"Wildberries отклонил запрос (HTTP {exc.status_code})"
            return "Не удалось надёжно определить результат запроса к Wildberries"
        text = str(exc).strip()
        return text[:500] if text else "Неизвестная ошибка внешнего сервиса"

    @staticmethod
    def _draft_fingerprint(test: ABTest) -> str:
        """Fingerprint the exact draft state the user reviewed before launch.

        Request-only fields such as funding source are deliberately excluded: the
        confirmation binds to the test draft, while idempotency binds to the
        complete start request separately.
        """
        payload = {
            "test_id": test.id,
            "user_id": test.user_id,
            "connection_id": test.connection_id,
            "nm_id": test.nm_id,
            "title": test.title,
            "skip_current_photo": bool(test.skip_current_photo),
            "keep_winner_as_main": bool(test.keep_winner_as_main),
            "delete_test_media": bool(test.delete_test_media),
            "views_per_variant": int(test.views_per_variant),
            "cpm_rub": int(test.cpm_rub),
            "budget_rub": int(test.budget_rub),
            "bid_type": str(test.bid_type or "unified"),
            "placement": test.placement,
            "variants": [
                {
                    "id": variant.id,
                    "position": variant.position,
                    "source_type": variant.source_type,
                    "source_url": variant.source_url,
                    "file_path": variant.file_path,
                }
                for variant in sorted(test.variants, key=lambda item: item.position)
            ],
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()

    @staticmethod
    def _operation_fingerprint(test: ABTest, request: ABTestStartRequest) -> str:
        payload = {
            "test_id": test.id,
            "user_id": test.user_id,
            "connection_id": test.connection_id,
            "nm_id": test.nm_id,
            "title": test.title,
            "skip_current_photo": bool(test.skip_current_photo),
            "keep_winner_as_main": bool(test.keep_winner_as_main),
            "delete_test_media": bool(test.delete_test_media),
            "views_per_variant": int(test.views_per_variant),
            "cpm_rub": int(test.cpm_rub),
            "bid_type": str(test.bid_type or "unified"),
            "placement": test.placement,
            "variant_ids": [variant.id for variant in test.variants if variant.source_type != "control"],
            "variant_sources": [
                {
                    "position": variant.position,
                    "source_type": variant.source_type,
                    "source_url": variant.source_url,
                    "file_path": variant.file_path,
                }
                for variant in test.variants
                if variant.source_type != "control"
            ],
            "auto_deposit": bool(request.auto_deposit),
            "deposit_rub": request.deposit_rub,
            "funding_source": request.funding_source,
            "confirmed_cpm": request.confirmed_cpm,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    async def _prepare_start_operation(
        self,
        test: ABTest,
        request: ABTestStartRequest,
        idempotency_key: str | None,
    ) -> tuple[ABTestOperation, bool]:
        key = str(idempotency_key or f"start:test:{test.id}").strip()
        if not key or len(key) > 128:
            raise HTTPException(status_code=422, detail="Некорректный ключ операции запуска")
        fingerprint = self._operation_fingerprint(test, request)
        existing = await self.repository.get_operation(key)
        if existing:
            if existing.fingerprint != fingerprint:
                raise HTTPException(
                    status_code=409,
                    detail="Параметры запуска изменились. Требуется новое подтверждение пользователя.",
                )
            if existing.status == ABTestOperationStatus.SUCCEEDED:
                return existing, False
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "reconciliation_required",
                    "message": "Предыдущий запуск ещё не подтверждён. Повтор запрещён до сверки операции.",
                    "operation_id": existing.id,
                    "incident_id": test.incident_id,
                },
            )

        operation = await self.repository.create_operation(
            test_id=test.id,
            user_id=test.user_id,
            connection_id=test.connection_id,
            nm_id=test.nm_id,
            operation_key=key,
            kind="start",
            fingerprint=fingerprint,
            status=ABTestOperationStatus.PREPARED,
            request_snapshot={
                "test_id": test.id,
                "connection_id": test.connection_id,
                "nm_id": test.nm_id,
                "views_per_variant": test.views_per_variant,
                "cpm_rub": test.cpm_rub,
                "bid_type": str(test.bid_type or "unified"),
                "placement": test.placement,
                "deposit_rub": request.deposit_rub,
                "auto_deposit": request.auto_deposit,
                "funding_source": request.funding_source,
            },
        )
        # An incident belongs only to an unresolved external effect. Do not
        # allocate one for every ordinary start: successful tests must not
        # display an incident ID left over from the preparation phase.
        test.incident_id = None
        test.last_error = None
        test.operation_state = "preparing"
        test.campaign_state = "not_created"
        test.media_status = "original"
        test.stats_quality = "not_started"
        operation.attempts = int(operation.attempts or 0) + 1
        await self.db.commit()
        return operation, True

    async def _operation_state(
        self,
        test: ABTest,
        operation: ABTestOperation | None,
        status_value: ABTestOperationStatus,
        *,
        error: Exception | None = None,
    ) -> None:
        if operation:
            operation.status = status_value
            operation.last_error = self._safe_error(error) if error else None
        test.operation_state = status_value.value
        if error and not test.last_error:
            # Callers may have already composed a richer incident message
            # (including cleanup details and the incident ID). Preserve it.
            test.last_error = self._safe_error(error)
        await self.db.flush()

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _media_root() -> Path:
        root = Path(settings.media_root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root

    @staticmethod
    def _signed_media_url(file_path: str) -> str:
        expires = int(datetime.now(timezone.utc).timestamp()) + max(int(settings.media_url_expire_sec), 60)
        message = f"{file_path}:{expires}".encode()
        secret = (settings.media_signing_secret.strip() or settings.jwt_secret_key).encode()
        signature = hmac.new(secret, message, hashlib.sha256).hexdigest()
        return f"/media/{quote(file_path, safe='/')}?expires={expires}&signature={signature}"

    @staticmethod
    def _canonical_media_url(url: str) -> str:
        """Compare WB media URLs without cache-busting query parameters."""
        parsed = urlsplit(str(url).strip())
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))

    @staticmethod
    def _api_error(exc: Exception) -> HTTPException:
        if isinstance(exc, ABTestReconciliationRequired):
            return HTTPException(
                status_code=409,
                detail={
                    "code": "reconciliation_required",
                    "message": str(exc),
                    "incident_id": exc.incident_id,
                },
            )
        if isinstance(exc, WBApiError):
            if exc.status_code in {401, 403}:
                return HTTPException(status_code=403, detail="У токена нет нужного доступа к API Wildberries")
            if exc.status_code == 429:
                return HTTPException(status_code=429, detail="Wildberries временно ограничил частоту запросов. Повторите позже")
            return HTTPException(status_code=502, detail=ABTestService._safe_error(exc))
        return HTTPException(status_code=502, detail="Не удалось выполнить запрос к Wildberries. Проверьте состояние операции и повторите сверку позже")

    async def _connection_or_404(self, user_id: int, connection_id: int, *, require_ab_access: bool = True):
        connection = await self.repository.get_connection(user_id, connection_id)
        if not connection:
            raise HTTPException(status_code=404, detail="Магазин Wildberries не найден")
        if require_ab_access and (
            not connection.ready_for_ab_tests or not connection.content_access or not connection.promotion_access
        ):
            raise HTTPException(
                status_code=403,
                detail="Для A/B-тестов токен должен иметь доступ к категориям «Контент» и «Продвижение»",
            )
        return connection

    async def _clients(self, connection) -> tuple[WBContentClient, WBPromotionClient]:
        token = WBTokenService.decrypt_token(connection.token_encrypted)
        return WBContentClient(token), WBPromotionClient(token)

    async def _confirm_campaign_active(self, promotion: WBPromotionClient, campaign_id: int) -> int | None:
        """Confirm a start through WB's eventually-consistent read model.

        ``adv/v0/start`` acknowledges the mutation, while
        ``promotion/count`` can still expose the previous state for a few
        seconds. A single immediate read must not turn a successful start
        into a false reconciliation incident.
        """
        last_status: int | None = None
        last_error: Exception | None = None
        for attempt, delay in enumerate(self.CAMPAIGN_ACTIVE_CONFIRM_DELAYS):
            if delay:
                await asyncio.sleep(delay)
            try:
                last_status = await promotion.get_campaign_status(campaign_id, refresh=True)
                last_error = None
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "WB campaign status read failed after start campaign_id=%s attempt=%s/%s error=%s",
                    campaign_id,
                    attempt + 1,
                    len(self.CAMPAIGN_ACTIVE_CONFIRM_DELAYS),
                    self._safe_error(exc),
                )
                continue
            if last_status == 9:
                return last_status
        if last_error is not None and last_status is None:
            raise last_error
        return last_status

    async def _confirm_campaign_budget(
        self,
        promotion: WBPromotionClient,
        campaign_id: int,
        required_budget: int,
    ) -> int:
        """Wait for a deposited budget to become visible in WB.

        ``budget/deposit`` acknowledges the request before the campaign
        budget read model is updated. A single immediate GET was the reason
        valid deposits were reported as missing and retried. This helper only
        reads the numeric total; it does not repeat the deposit.
        """
        last_budget: int | None = None
        last_error: Exception | None = None
        for attempt, delay in enumerate(self.CAMPAIGN_BUDGET_CONFIRM_DELAYS):
            if delay:
                await asyncio.sleep(delay)
            try:
                last_budget = int(await promotion.get_budget_total(campaign_id))
                last_error = None
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "WB campaign budget read failed campaign_id=%s attempt=%s/%s error=%s",
                    campaign_id,
                    attempt + 1,
                    len(self.CAMPAIGN_BUDGET_CONFIRM_DELAYS),
                    self._safe_error(exc),
                )
                continue
            if last_budget >= int(required_budget):
                return last_budget
        if last_error is not None and last_budget is None:
            raise last_error
        return int(last_budget or 0)

    async def _confirm_campaign_product(
        self,
        promotion: WBPromotionClient,
        campaign_id: int,
        nm_id: int,
    ) -> None:
        """Prove that the external campaign contains the selected product.

        WB's campaign details are eventually consistent. We wait through the
        read-model delay, but never treat an empty response as a match. This
        guard runs before budget deposit and campaign start.
        """
        last_details: dict[str, Any] | None = None
        for delay in self.CAMPAIGN_DETAILS_CONFIRM_DELAYS:
            if delay:
                await asyncio.sleep(delay)
            details = await promotion.get_campaign_details(campaign_id)
            if details is None:
                continue
            last_details = details
            campaign_nm_ids = promotion.campaign_nm_ids(details)
            if int(nm_id) in campaign_nm_ids:
                return
            if campaign_nm_ids:
                raise ABCampaignProductMismatch(
                    f"Кампания WB #{int(campaign_id)} относится к другой карточке, а не к nmID {int(nm_id)}. "
                    "Кампания остановлена; требуется ручная сверка.",
                )
        if last_details is None:
            raise ABTestReconciliationRequired(
                f"Wildberries пока не подтвердил состав кампании WB #{int(campaign_id)}. "
                "Деньги не пополнялись; повторите сверку позже.",
            )
        raise ABTestReconciliationRequired(
            f"Wildberries не подтвердил nmID {int(nm_id)} в кампании WB #{int(campaign_id)}. "
            "Деньги не пополнялись; требуется ручная сверка.",
        )

    async def _assert_campaign_configuration(self, test: ABTest, promotion: WBPromotionClient) -> None:
        if not test.wb_campaign_id:
            return
        details = await promotion.get_campaign_details(test.wb_campaign_id)
        if details is None:
            test.campaign_state = "unknown"
            test.operation_state = ABTestOperationStatus.RECONCILIATION_REQUIRED.value
            test.incident_id = test.incident_id or self._incident_id()
            raise ABTestReconciliationRequired(
                f"Не удалось подтвердить конфигурацию кампании WB #{int(test.wb_campaign_id)}.",
                incident_id=test.incident_id,
            )
        nm_ids = promotion.campaign_nm_ids(details)
        if nm_ids and nm_ids != {int(test.nm_id)}:
            test.campaign_state = "unknown"
            test.operation_state = ABTestOperationStatus.RECONCILIATION_REQUIRED.value
            test.incident_id = test.incident_id or self._incident_id()
            raise ABTestReconciliationRequired(
                f"Кампания WB #{int(test.wb_campaign_id)} была изменена извне: в ней товар(ы) {sorted(nm_ids)} вместо nmID {int(test.nm_id)}.",
                incident_id=test.incident_id,
            )
        # Different WB API generations expose these keys under different names.
        # Only compare a field when the provider actually returns it.
        external_bid = details.get("bid_type") or details.get("bidType") or details.get("bidTypeId")
        if external_bid is not None and str(external_bid).strip().lower() not in {str(test.bid_type).lower(), "1" if test.bid_type == "unified" else "0"}:
            raise ABTestReconciliationRequired(
                f"Тип ставки кампании WB #{int(test.wb_campaign_id)} изменён извне.",
                incident_id=test.incident_id or self._incident_id(),
            )
        external_placement = details.get("placement") or details.get("placement_type") or details.get("placementTypes")
        if external_placement is not None and isinstance(external_placement, (str, list, tuple)):
            values = {str(v).lower() for v in (external_placement if isinstance(external_placement, (list, tuple)) else [external_placement])}
            if test.bid_type == "unified" and values and not ({"combined", "search", "recommendations"} & values):
                raise ABTestReconciliationRequired(
                    f"Размещение кампании WB #{int(test.wb_campaign_id)} изменено извне.",
                    incident_id=test.incident_id or self._incident_id(),
                )

    @asynccontextmanager
    async def _operation_lock(self, connection_id: int, nm_id: int, user_id: int | None = None, *, wait: bool = True):
        scope = None
        if user_id is not None:
            connection = await self.repository.get_connection(user_id, connection_id)
            scope = (WBTokenService.store_fingerprint(connection)
                     if connection and getattr(connection, "seller_id", None)
                     else getattr(connection, "token_fingerprint", None))
        async with WBRateLimiter.operation_lock(self.db, connection_id, nm_id, scope_key=scope or f"connection:{connection_id}", wait=wait) as acquired:
            yield acquired

    @asynccontextmanager
    async def _stats_operation_lock(self, connection_id: int):
        async with WBRateLimiter.stats_lock(self.db, connection_id):
            yield

    def _state_snapshot(self, test: ABTest) -> dict[str, object]:
        return {
            "status": str(test.status.value if isinstance(test.status, ABTestStatus) else test.status),
            "operation_state": test.operation_state,
            "campaign_state": test.campaign_state,
            "media_status": test.media_status,
            "stats_quality": test.stats_quality,
            "current_variant_order": int(test.current_variant_order or 0),
            "winner_variant_order": test.winner_variant_order,
            "campaign_id": int(test.wb_campaign_id) if test.wb_campaign_id else None,
        }

    async def _audit(self, test: ABTest, action: str, before: dict[str, object], *, details: dict[str, object] | None = None) -> None:
        await self.repository.add_audit_event(
            test_id=test.id,
            user_id=test.user_id,
            nm_id=int(test.nm_id),
            connection_id=int(test.connection_id),
            action=action,
            before_state=before,
            after_state=self._state_snapshot(test),
            details=details or {},
        )

    async def _assert_media_snapshot_current(self, test: ABTest, content: WBContentClient) -> None:
        """Fail closed when a human/another system changed the card behind our back."""
        state = self._media_state(test)
        expected = state.get("expected_snapshot") or {}
        if not expected and not state.get("expected_videos"):
            return
        card = await content.get_card(test.nm_id)
        snapshot = WBContentClient.media_snapshot(card)
        expected_videos = list(state.get("expected_videos") or [])
        actual_videos = list(snapshot.get("videos") or [])
        if expected_videos != actual_videos:
            test.operation_state = ABTestOperationStatus.RECONCILIATION_REQUIRED.value
            test.media_status = "external_conflict"
            test.incident_id = test.incident_id or self._incident_id()
            raise ABTestReconciliationRequired(
                "Видеоматериалы карточки WB были изменены извне. Перезапись заблокирована.",
                incident_id=test.incident_id,
            )
        actual_photos = snapshot.get("photos") or []
        expected_count = int(state.get("expected_slot_count") or len(expected))
        if len(actual_photos) != expected_count:
            test.operation_state = ABTestOperationStatus.RECONCILIATION_REQUIRED.value
            test.media_status = "external_conflict"
            test.incident_id = test.incident_id or self._incident_id()
            raise ABTestReconciliationRequired(
                f"Карточка WB была изменена извне: ожидалось {expected_count} фото, сейчас {len(actual_photos)}. Перезапись заблокирована.",
                incident_id=test.incident_id,
            )
        mismatches: list[str] = []
        backups = state.get("backups") or {}
        for slot in range(1, expected_count + 1):
            actual_url = str(actual_photos[slot - 1] or "")
            expected_entry = expected.get(str(slot)) or expected.get(slot) or {}
            expected_url = str(expected_entry.get("url") or "")
            if self._canonical_media_url(actual_url) != self._canonical_media_url(expected_url):
                mismatches.append(f"slot {slot}: URL")
                continue
            backup_entry = backups.get(str(slot)) or {}
            expected_sha = expected_entry.get("sha256") or backup_entry.get("sha256")
            if expected_sha:
                try:
                    data, _ = await content.download_image(actual_url, cache_bust=True)
                    actual_sha = hashlib.sha256(data).hexdigest()
                    if actual_sha != expected_sha:
                        mismatches.append(f"slot {slot}: bytes")
                except Exception as exc:
                    test.operation_state = ABTestOperationStatus.RECONCILIATION_REQUIRED.value
                    test.media_status = "external_conflict"
                    test.incident_id = test.incident_id or self._incident_id()
                    raise ABTestReconciliationRequired(
                        f"Не удалось подтвердить целостность текущего изображения WB в слоте {slot}. Перезапись заблокирована.",
                        incident_id=test.incident_id,
                    ) from exc
        if mismatches:
            test.operation_state = ABTestOperationStatus.RECONCILIATION_REQUIRED.value
            test.media_status = "external_conflict"
            test.incident_id = test.incident_id or self._incident_id()
            raise ABTestReconciliationRequired(
                "Карточка WB была изменена извне (" + ", ".join(mismatches) + "). Перезапись заблокирована.",
                incident_id=test.incident_id,
            )

    async def _assert_media_snapshot_safe_for_restore(self, test: ABTest, content: WBContentClient) -> None:
        """Allow restore only when WB still matches our expected state or the
        exact in-flight state recorded in the durable media journal.

        This closes the gap between safe conflict detection and crash recovery:
        a partially-applied operation is expected to differ from the pre-write
        snapshot, but a human/external writer must still be rejected.
        """
        state = self._media_state(test)
        expected = state.get("expected_snapshot") or {}
        pending = state.get("pending") or {}
        if not expected and not pending:
            return
        card = await content.get_card(test.nm_id)
        snapshot = WBContentClient.media_snapshot(card)
        actual_photos = list(snapshot.get("photos") or [])
        expected_count = int(state.get("expected_slot_count") or len(expected))
        if expected_count and len(actual_photos) != expected_count:
            raise ABTestReconciliationRequired(
                "Количество фото карточки WB изменилось извне; восстановление заблокировано.",
                incident_id=test.incident_id or self._incident_id(),
            )
        pending_targets = pending.get("targets") if isinstance(pending, dict) else {}
        for slot_index, actual_url in enumerate(actual_photos, start=1):
            expected_entry = expected.get(str(slot_index)) or expected.get(slot_index) or {}
            expected_url = str(expected_entry.get("url") or "")
            candidates: set[str] = set()
            if expected_url:
                candidates.add(self._canonical_media_url(expected_url))
            target = pending_targets.get(str(slot_index)) if isinstance(pending_targets, dict) else None
            if isinstance(target, dict):
                shadow_path = target.get("shadow_path")
                if shadow_path:
                    try:
                        shadow = self._state_file(str(shadow_path))
                        if shadow.is_file():
                            candidates.add("sha256:" + hashlib.sha256(shadow.read_bytes()).hexdigest())
                    except OSError:
                        pass
            actual_canonical = self._canonical_media_url(str(actual_url or ""))
            if actual_canonical in candidates:
                continue
            # When the pending write changed the URL, compare its actual bytes
            # against the durable shadow. If neither expected state matches,
            # another system changed the card and recovery must stop.
            matched_shadow = False
            if isinstance(target, dict) and target.get("shadow_path"):
                try:
                    shadow = self._state_file(str(target["shadow_path"]))
                    if shadow.is_file():
                        actual_bytes, _ = await content.download_image(str(actual_url), cache_bust=True)
                        matched_shadow = hashlib.sha256(actual_bytes).hexdigest() == hashlib.sha256(shadow.read_bytes()).hexdigest()
                except Exception:
                    matched_shadow = False
            if not matched_shadow:
                raise ABTestReconciliationRequired(
                    f"Слот {slot_index} был изменён вне журнала операции; восстановление заблокировано.",
                    incident_id=test.incident_id or self._incident_id(),
                )

    async def _refresh_expected_media_snapshot(self, test: ABTest, content: WBContentClient) -> None:
        card = await content.get_card(test.nm_id)
        snapshot = WBContentClient.media_snapshot(card)
        photos = list(snapshot.get("photos") or [])
        if not photos:
            raise ABTestReconciliationRequired(
                "После изменения media Wildberries не вернул изображения карточки.",
                incident_id=test.incident_id or self._incident_id(),
            )
        state = self._media_state(test)
        expected: dict[str, dict[str, object]] = {}
        for slot, url in enumerate(photos, start=1):
            entry: dict[str, object] = {"url": url}
            try:
                data, _ = await content.download_image(url, cache_bust=True)
                entry["sha256"] = hashlib.sha256(data).hexdigest()
            except Exception:
                # URL equality remains useful when CDN read-back is temporarily unavailable;
                # the next mutation will fail closed if a hash is available for the slot.
                pass
            expected[str(slot)] = entry
        state["expected_snapshot"] = expected
        state["expected_slot_count"] = len(photos)
        state["expected_videos"] = list(snapshot.get("videos") or [])
        await self._set_media_state(test, state)

    async def _settle_stage_stats(self, test: ABTest, promotion: WBPromotionClient, *, attempts: int | None = None) -> None:
        """Record a stable campaign boundary, without inventing photo attribution.

        Two equal observations establish an operational switching boundary.
        WB does not promise that they are final, or identify the photo that
        generated an event. Late events therefore remain unallocated and can
        never turn an ordinary fullstats response into a verified winner.
        """
        if not test.wb_campaign_id:
            return
        if test.campaign_state not in {"paused", "stopped"}:
            raise ABTestReconciliationRequired("Для сверки этапа требуется подтверждённая пауза или остановка кампании.")
        attempts = max(attempts or int(getattr(settings, "ab_test_stats_settle_attempts", 3) or 3), 2)
        delay = max(int(getattr(settings, "ab_test_stats_settle_delay_sec", 10) or 10), 1)
        stable = 0
        baseline_views = int(test.settled_total_views or 0)
        baseline_clicks = int(test.settled_total_clicks or 0)
        baseline_orders = int(test.settled_total_orders or 0)
        baseline_spend = float(test.settled_total_spend_rub or 0)
        previous = (
            int(test.last_total_views or baseline_views),
            int(test.last_total_clicks or baseline_clicks),
            int(test.last_total_orders or baseline_orders),
            float(test.last_total_spend_rub or baseline_spend),
        )
        final_totals = previous
        last_observation = None
        for attempt in range(attempts):
            async with self._stats_operation_lock(test.connection_id):
                totals = await self._fullstats_windowed(promotion, test.wb_campaign_id, test.started_at, refresh=True)
            quality, current = self._validated_stats(totals)
            await self._record_stats_observation(test, totals, quality=quality, purpose="stage_boundary")
            if quality != "complete" or current is None:
                test.stats_quality = "reconciliation_required"
                test.operation_state = ABTestOperationStatus.RECONCILIATION_REQUIRED.value
                test.incident_id = test.incident_id or self._incident_id()
                raise ABTestReconciliationRequired("WB не вернул полную корректную статистику для сверки этапа.", incident_id=test.incident_id)
            views, clicks, reported_orders, spend = current
            # Orders are optional and do not decide CTR. Preserve a previous
            # observation instead of manufacturing a decrease when omitted.
            orders = previous[2] if reported_orders is None else reported_orders
            current = (views, clicks, orders, spend)
            if any(
                current[i] < previous[i] - (1e-6 if i == 3 else 0)
                for i in range(4)
            ):
                test.stats_quality = "reconciliation_required"
                test.operation_state = ABTestOperationStatus.RECONCILIATION_REQUIRED.value
                test.incident_id = test.incident_id or self._incident_id()
                raise ABTestReconciliationRequired(
                    "WB статистика уменьшилась во время финальной сверки этапа.",
                    incident_id=test.incident_id,
                )
            final_totals = current
            if last_observation is not None and current == last_observation:
                stable += 1
            else:
                stable = 0
            previous = current
            last_observation = current
            test.last_total_views = views
            test.last_total_clicks = clicks
            test.last_total_orders = orders
            test.last_total_spend_rub = spend
            await self._retain_unallocated_statistics(test, views, clicks, spend)
            test.last_synced_at = self._now()
            if stable >= 1:
                break
            if attempt + 1 < attempts:
                await asyncio.sleep(delay)

        if stable < 1:
            test.stats_quality = "reconciliation_required"
            test.operation_state = ABTestOperationStatus.RECONCILIATION_REQUIRED.value
            test.incident_id = test.incident_id or self._incident_id()
            raise ABTestReconciliationRequired("Статистика этапа продолжает изменяться: смена фото заблокирована.", incident_id=test.incident_id)

        views, clicks, orders, spend = final_totals
        dv = views - baseline_views
        dc = clicks - baseline_clicks
        do = orders - baseline_orders
        ds = spend - baseline_spend
        if dv < 0 or dc < 0 or do < 0 or ds < -1e-6:
            test.stats_quality = "reconciliation_required"
            test.operation_state = ABTestOperationStatus.RECONCILIATION_REQUIRED.value
            test.incident_id = test.incident_id or self._incident_id()
            raise ABTestReconciliationRequired(
                "WB статистика вернулась ниже последней подтвержденной границы этапа.",
                incident_id=test.incident_id,
            )

        # A winner is allowed only when the provider gives an explicit,
        # complete variant breakdown.  Campaign totals alone remain
        # unallocated because late impressions can cross a photo boundary.
        variant_totals = totals.get("_variant_totals") if isinstance(totals, dict) else None
        attribution_complete = bool(
            isinstance(totals, dict)
            and totals.get("_variant_attribution_complete") is True
            and isinstance(variant_totals, dict)
        )
        candidates = [
            variant for variant in test.variants
            if variant.source_type != "control" or (not bool(test.skip_current_photo) and variant.position == 0)
        ]
        expected_positions = {str(variant.position) for variant in candidates}
        if attribution_complete and set(variant_totals) >= expected_positions:
            parsed: dict[int, tuple[int, int, int, float]] = {}
            for variant in candidates:
                raw = variant_totals.get(str(variant.position)) or {}
                try:
                    item = (int(raw.get("views", 0)), int(raw.get("clicks", 0)), int(raw.get("orders", 0)), round(float(raw.get("sum", 0)), 2))
                except (TypeError, ValueError, OverflowError):
                    parsed = {}
                    break
                if item[0] < 0 or item[1] < 0 or item[1] > item[0] or item[2] < 0 or item[3] < 0:
                    parsed = {}
                    break
                parsed[variant.position] = item
            if parsed and sum(item[0] for item in parsed.values()) <= views and sum(item[1] for item in parsed.values()) <= clicks and sum(item[3] for item in parsed.values()) <= spend + 0.01:
                for variant in candidates:
                    variant.views, variant.clicks, variant.orders, variant.spend_rub = parsed[variant.position]
                    variant.is_winner = False
                test.unallocated_views = max(views - sum(item[0] for item in parsed.values()), 0)
                test.unallocated_clicks = max(clicks - sum(item[1] for item in parsed.values()), 0)
                test.unallocated_spend_rub = round(max(spend - sum(item[3] for item in parsed.values()), 0.0), 2)
                test.stats_quality = "stage_attributed"
            else:
                attribution_complete = False
        if not attribution_complete or test.stats_quality != "stage_attributed":
            await self._retain_unallocated_statistics(test, views, clicks, spend)
            test.stats_quality = "aggregate_unverified"

        # Keep the observed interval for the audit, explicitly unverified.
        # It is unsuitable for CTR comparison: delayed events can cross this
        # boundary even after multiple equal observations while paused.
        await self._audit(test, "stats_boundary_observed", self._state_snapshot(test), details={
            "variant_position": test.current_variant_order,
            "views": dv, "clicks": dc, "orders": do, "spend_rub": round(ds, 2),
            "attribution": "stage_attributed" if test.stats_quality == "stage_attributed" else "aggregate_unverified",
        })
        test.stage_views = int(max(dv, 0))
        test.stage_clicks = int(max(dc, 0))
        test.stage_spend_rub = float(max(ds, 0.0))
        test.settled_total_views = int(views)
        test.settled_total_clicks = int(clicks)
        test.settled_total_orders = int(orders)
        test.settled_total_spend_rub = float(spend)
        if test.stats_quality != "stage_attributed":
            await self._retain_unallocated_statistics(test, views, clicks, spend)
            test.stats_quality = "aggregate_unverified"

    async def cards(
        self,
        user_id: int,
        connection_id: int,
        search: str = "",
        *,
        cursor_updated_at: str | None = None,
        cursor_nm_id: int | None = None,
    ) -> dict[str, Any]:
        connection = await self._connection_or_404(user_id, connection_id)
        content, _ = await self._clients(connection)
        try:
            cursor = None
            if cursor_updated_at and cursor_nm_id is not None:
                cursor = {"updatedAt": cursor_updated_at, "nmID": int(cursor_nm_id), "limit": 100}
            cards, next_cursor = await content.list_cards_page(search=search, limit=100, cursor=cursor)
            normalized_cursor = None
            total = None
            if next_cursor:
                total = int(next_cursor["total"]) if str(next_cursor.get("total", "")).isdigit() else None
                normalized_cursor = {
                    key: next_cursor[key]
                    for key in ("updatedAt", "nmID", "nmId", "limit")
                    if key in next_cursor
                }
            return {"items": cards, "next_cursor": normalized_cursor, "total": total}
        except Exception as exc:
            raise self._api_error(exc) from exc

    async def card(self, user_id: int, connection_id: int, nm_id: int) -> dict[str, Any]:
        connection = await self._connection_or_404(user_id, connection_id)
        content, _ = await self._clients(connection)
        try:
            result = await content.get_card(nm_id)
        except Exception as exc:
            raise self._api_error(exc) from exc
        if not result:
            raise HTTPException(status_code=404, detail="Карточка товара не найдена в Wildberries")
        return result

    async def create(self, user_id: int, request: ABTestCreateRequest) -> ABTest:
        connection = await self._connection_or_404(user_id, request.connection_id)
        card = await self.card(user_id, request.connection_id, request.nm_id)
        if not card.get("photos"):
            raise HTTPException(status_code=400, detail="У карточки нет изображений для безопасного запуска теста")
        store_fingerprint = WBTokenService.store_fingerprint(connection)
        existing_open = await self.repository.get_open_for_store_card(store_fingerprint, request.nm_id)
        if existing_open:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "card_test_already_open",
                    "message": "Для этой карточки уже существует незавершённый A/B-тест в этом WB-магазине.",
                    "test_id": existing_open.id,
                },
            )
        test = await self.repository.create(
            user_id=user_id,
            connection_id=connection.id,
            nm_id=request.nm_id,
            store_fingerprint=store_fingerprint,
            lifecycle_lock=True,
            title=request.title or card.get("title") or f"A/B-тест {request.nm_id}",
            status=ABTestStatus.DRAFT,
            skip_current_photo=request.skip_current_photo,
            keep_winner_as_main=bool(request.keep_winner_as_main),
            delete_test_media=request.delete_test_media,
            views_per_variant=request.views_per_variant,
            cpm_rub=request.cpm_rub,
            budget_rub=request.budget_rub,
            bid_type="unified",
            placement="combined",
        )
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise HTTPException(status_code=409, detail={"code": "card_test_already_open", "message": "Для этой карточки уже существует незавершённый A/B-тест в этом WB-магазине."}) from exc
        return await self.repository.get_for_user(user_id, test.id)  # type: ignore[return-value]

    @classmethod
    def _validate_image(cls, filename: str, content_type: str, data: bytes) -> str:
        extension = Path(filename or "").suffix.lower()
        content_type = (content_type or "").split(";", 1)[0].strip().lower()
        if content_type not in cls.ALLOWED_IMAGE_TYPES and extension not in cls.ALLOWED_EXTENSIONS:
            raise HTTPException(status_code=400, detail="Поддерживаются JPG, PNG, WEBP, BMP, GIF и TIFF")
        if len(data) > cls.MAX_IMAGE_BYTES:
            raise HTTPException(status_code=400, detail="Размер изображения не должен превышать 32 МБ")
        if not data:
            raise HTTPException(status_code=400, detail="Файл изображения пуст")
        signatures = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"BM", b"RIFF")
        if not data.startswith(signatures):
            raise HTTPException(status_code=400, detail="Файл не похож на корректное изображение")
        dimensions = cls._image_dimensions(data)
        if not dimensions:
            raise HTTPException(status_code=400, detail="Не удалось проверить размеры изображения")
        if dimensions[0] < 700 or dimensions[1] < 900:
            raise HTTPException(status_code=400, detail="Минимальный размер изображения — 700 × 900 пикселей")
        if extension not in cls.ALLOWED_EXTENSIONS:
            extension = mimetypes.guess_extension(content_type) or ".jpg"
        return extension if extension != ".jpe" else ".jpg"

    @classmethod
    def _prepare_image(
        cls,
        filename: str,
        content_type: str,
        data: bytes,
        *,
        verify_integrity: bool = True,
    ) -> tuple[str, str, bytes]:
        """Validate an upload and convert TIFF to a WB-compatible JPEG.

        Wildberries does not accept TIFF in the product media API. Conversion
        happens before the file is persisted, so the rest of the A/B engine
        always works with the same formats that WB accepts.
        """
        original_name = filename or "image.jpg"
        extension = Path(original_name).suffix.lower()
        normalized_type = (content_type or "").split(";", 1)[0].strip().lower()
        is_tiff = data.startswith(cls.TIFF_SIGNATURES)
        declared_tiff = extension in {".tif", ".tiff"} or normalized_type in cls.TIFF_TYPES

        if len(data) > cls.MAX_IMAGE_BYTES:
            raise HTTPException(status_code=400, detail="Размер изображения не должен превышать 32 МБ")
        if not data:
            raise HTTPException(status_code=400, detail="Файл изображения пуст")
        if declared_tiff and not is_tiff:
            raise HTTPException(status_code=400, detail="Файл с расширением TIFF не является корректным TIFF-изображением")

        if is_tiff:
            try:
                from PIL import Image, UnidentifiedImageError
            except ImportError as exc:
                raise HTTPException(status_code=503, detail="Конвертация TIFF временно недоступна на сервере") from exc
            try:
                with Image.open(io.BytesIO(data)) as source:
                    source.seek(0)  # A multi-page TIFF uses its first page as the variant.
                    frame = source.convert("RGB")
                    output = io.BytesIO()
                    frame.save(output, format="JPEG", quality=95, optimize=True)
                    converted = output.getvalue()
                    frame.close()
            except (UnidentifiedImageError, OSError, ValueError, EOFError) as exc:
                raise HTTPException(status_code=400, detail="Не удалось прочитать TIFF-изображение") from exc

            converted_name = f"{Path(original_name).stem or 'image'}.jpg"
            cls._validate_image(converted_name, "image/jpeg", converted)
            return converted_name, "image/jpeg", converted

        cls._validate_image(original_name, normalized_type, data)
        if verify_integrity:
            try:
                from PIL import Image, UnidentifiedImageError
            except ImportError as exc:
                raise HTTPException(status_code=503, detail="Проверка изображения временно недоступна на сервере") from exc
            try:
                # Header dimensions alone are not enough: a truncated JPEG/PNG
                # can still advertise valid dimensions. Verify the complete
                # uploaded file before it is persisted or offered to WB.
                from PIL import ImageFile
                ImageFile.LOAD_TRUNCATED_IMAGES = False
                with Image.open(io.BytesIO(data)) as image:
                    image.verify()
                with Image.open(io.BytesIO(data)) as image:
                    image.load()
            except (UnidentifiedImageError, OSError, ValueError, EOFError, Image.DecompressionBombError) as exc:
                raise HTTPException(status_code=400, detail="Файл изображения повреждён или не читается полностью") from exc
        return original_name, (
            normalized_type if normalized_type in cls.ALLOWED_IMAGE_TYPES else mimetypes.guess_type(original_name)[0] or "image/jpeg"
        ), data

    async def preview_image(self, user_id: int, file: UploadFile) -> dict[str, Any]:
        """Convert an uploaded image to a browser-previewable data URL."""
        del user_id  # Authentication is enforced by the router; previews are not persisted.
        data = await file.read(self.MAX_IMAGE_BYTES + 1)
        filename, content_type, prepared = self._prepare_image(
            file.filename or "image.tiff",
            file.content_type or "",
            data,
        )
        self._validate_image(filename, content_type, prepared)
        dimensions = self._image_dimensions(prepared)
        if not dimensions:
            raise HTTPException(status_code=400, detail="Не удалось проверить размеры изображения")
        return {
            "image_url": f"data:{content_type};base64,{base64.b64encode(prepared).decode('ascii')}",
            "file_name": filename,
            "width": dimensions[0],
            "height": dimensions[1],
        }

    @staticmethod
    def _image_dimensions(data: bytes) -> tuple[int, int] | None:
        """Read dimensions for common formats without trusting a client-side MIME type."""
        try:
            if data.startswith(b"\x89PNG") and len(data) >= 24:
                return struct.unpack(">II", data[16:24])
            if data[:6] in {b"GIF87a", b"GIF89a"} and len(data) >= 10:
                return struct.unpack("<HH", data[6:10])
            if data.startswith(b"BM") and len(data) >= 26:
                return abs(struct.unpack("<i", data[18:22])[0]), abs(struct.unpack("<i", data[22:26])[0])
            if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
                if data[12:16] == b"VP8X" and len(data) >= 30:
                    width = 1 + int.from_bytes(data[24:27], "little")
                    height = 1 + int.from_bytes(data[27:30], "little")
                    return width, height
                if data[12:16] == b"VP8 " and len(data) >= 30:
                    return struct.unpack("<HH", data[26:30])
            if data.startswith(b"\xff\xd8\xff"):
                offset = 2
                while offset + 9 < len(data):
                    while offset < len(data) and data[offset] != 0xFF:
                        offset += 1
                    while offset < len(data) and data[offset] == 0xFF:
                        offset += 1
                    if offset >= len(data):
                        break
                    marker = data[offset]
                    offset += 1
                    if marker in {0xD8, 0xD9}:
                        continue
                    if offset + 2 > len(data):
                        break
                    segment_length = int.from_bytes(data[offset:offset + 2], "big")
                    if marker in set(range(0xC0, 0xC4)) | set(range(0xC5, 0xC8)) | set(range(0xC9, 0xCC)) | set(range(0xCD, 0xD0)):
                        if offset + 7 <= len(data):
                            return int.from_bytes(data[offset + 5:offset + 7], "big"), int.from_bytes(data[offset + 3:offset + 5], "big")
                    offset += max(segment_length, 2)
        except (IndexError, struct.error, ValueError):
            return None
        # WB currently returns WEBP links for card photos. Pillow handles WEBP
        # variants such as VP8L that cannot be identified from the short header
        # alone, while the lightweight parsers above keep unit/test uploads
        # fast and dependency-independent for common formats.
        try:
            from PIL import Image

            with Image.open(io.BytesIO(data)) as image:
                return image.size
        except (ImportError, OSError, ValueError):
            pass
        return None

    async def upload_variant(self, user_id: int, test_id: int, position: int, file: UploadFile) -> ABTest:
        if position < 1 or position > self.MAX_VARIANTS:
            raise HTTPException(status_code=422, detail="Позиция варианта должна быть от 1 до 5")
        test = await self.repository.get_for_user(user_id, test_id)
        if not test:
            raise HTTPException(status_code=404, detail="A/B-тест не найден")
        async with self._operation_lock(test.connection_id, test.nm_id, user_id):
            test = await self.repository.get_for_update(user_id, test_id)
            if not test:
                raise HTTPException(status_code=404, detail="A/B-тест не найден")
            if test.status != ABTestStatus.DRAFT:
                raise HTTPException(status_code=409, detail="Изменять варианты можно только в черновике")
            data = await file.read(self.MAX_IMAGE_BYTES + 1)
            original_filename = file.filename or "image.jpg"
            filename, content_type, data = self._prepare_image(
                original_filename,
                file.content_type or "",
                data,
            )
            extension = Path(filename).suffix.lower()
            # Reject mathematically impossible experiment budgets before any
            # external WB operation can be created. PostgreSQL INTEGER and
            # WB integer fields must never be reached with an overflowing
            # views*CPM calculation.
            estimated_budget = calculate_required_budget(
                max(1, len([v for v in test.variants if v.source_type != "control"]) + 1),
                test.views_per_variant,
                test.cpm_rub,
            )
            if estimated_budget > 2_147_483_647:
                raise HTTPException(status_code=422, detail="Расчётный бюджет теста превышает технический предел 2 147 483 647 ₽")
            directory = self._media_root() / "ab_tests" / str(test.id)
            directory.mkdir(parents=True, exist_ok=True)
            old = await self.repository.get_variant(test.id, position)
            for existing in test.variants:
                if existing.position == position or existing.source_type != "upload" or not existing.file_path:
                    continue
                try:
                    same_file = (self._media_root() / existing.file_path).read_bytes() == data
                except OSError:
                    same_file = False
                if same_file:
                    raise HTTPException(status_code=409, detail="Один и тот же файл нельзя использовать дважды в тесте")
                # Exact bytes are not enough: re-encoded/resized copies of the
                # same creative must be rejected before the paid run starts.
                try:
                    existing_data = (self._media_root() / existing.file_path).read_bytes()
                    similar, distance = self._images_similar(existing_data, data)
                except (OSError, ValueError):
                    similar, distance = False, None
                if similar:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Вариант слишком похож на фото {existing.position}; выберите другое изображение (pHash distance={distance})",
                    )
            if old and old.file_path:
                (self._media_root() / old.file_path).unlink(missing_ok=True)
            file_path = Path("ab_tests") / str(test.id) / f"{position}_{secrets.token_hex(10)}{extension}"
            (self._media_root() / file_path).write_bytes(data)
            if old:
                old.source_type = "upload"
                old.file_path = str(file_path)
                old.source_url = None
                old.wb_url = None
                old.file_name = original_filename or f"Вариант {position}"
            else:
                await self.repository.add_variant(
                    test_id=test.id,
                    position=position,
                    source_type="upload",
                    file_path=str(file_path),
                    file_name=original_filename or f"Вариант {position}",
                )
            await self.db.commit()
        return await self.repository.get_for_user(user_id, test.id)  # type: ignore[return-value]

    async def set_variant_source(self, user_id: int, test_id: int, position: int, source_url: str) -> ABTest:
        """Attach one of the product's existing WB photos to a draft variant."""
        if position < 1 or position > self.MAX_VARIANTS:
            raise HTTPException(status_code=422, detail="Позиция варианта должна быть от 1 до 5")
        clean_url = str(source_url or "").strip()
        parsed = urlsplit(clean_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise HTTPException(status_code=400, detail="Выберите корректное изображение из карточки Wildberries")
        test = await self.repository.get_for_user(user_id, test_id)
        if not test:
            raise HTTPException(status_code=404, detail="A/B-тест не найден")
        async with self._operation_lock(test.connection_id, test.nm_id, user_id):
            test = await self.repository.get_for_update(user_id, test_id)
            if not test:
                raise HTTPException(status_code=404, detail="A/B-тест не найден")
            if test.status != ABTestStatus.DRAFT:
                raise HTTPException(status_code=409, detail="Изменять варианты можно только в черновике")
            content, _ = await self._clients(test.connection)
            try:
                card = await content.get_card(test.nm_id)
            except Exception as exc:
                raise self._api_error(exc) from exc
            photos = [str(url).strip() for url in ((card or {}).get("photos") or []) if str(url).strip()]
            selected_url = next(
                (url for url in photos if self._canonical_media_url(url) == self._canonical_media_url(clean_url)),
                None,
            )
            if not selected_url:
                raise HTTPException(status_code=400, detail="Изображение больше не найдено в карточке Wildberries")
            # The control image is already part of the card and must not be
            # added as a second tested variant. Enforce this server-side as
            # well as in the picker so direct API calls cannot create a
            # misleading test configuration.
            if photos and self._canonical_media_url(selected_url) == self._canonical_media_url(photos[0]):
                raise HTTPException(status_code=409, detail="Главное фото карточки нельзя добавить вторым вариантом")
            old = await self.repository.get_variant(test.id, position)
            duplicate_card_variant = next(
                (
                    variant
                    for variant in test.variants
                    if variant.position != position
                    and variant.source_type == "card"
                    and self._canonical_media_url(variant.source_url or "")
                    == self._canonical_media_url(selected_url)
                ),
                None,
            )
            if duplicate_card_variant:
                raise HTTPException(status_code=409, detail="Одно и то же фото карточки нельзя использовать дважды в тесте")
            if old and old.file_path:
                (self._media_root() / old.file_path).unlink(missing_ok=True)
            file_name = f"Фото карточки {photos.index(selected_url) + 1}"
            if old:
                old.source_type = "card"
                old.file_path = None
                old.source_url = selected_url
                old.wb_url = None
                old.file_name = file_name
            else:
                await self.repository.add_variant(
                    test_id=test.id,
                    position=position,
                    source_type="card",
                    source_url=selected_url,
                    file_name=file_name,
                )
            await self.db.commit()
        return await self.repository.get_for_user(user_id, test.id)  # type: ignore[return-value]

    async def delete_variant(self, user_id: int, test_id: int, position: int) -> ABTest:
        test = await self.repository.get_for_user(user_id, test_id)
        if not test:
            raise HTTPException(status_code=404, detail="A/B-тест не найден")
        async with self._operation_lock(test.connection_id, test.nm_id, user_id):
            test = await self.repository.get_for_update(user_id, test_id)
            if not test:
                raise HTTPException(status_code=404, detail="A/B-тест не найден")
            if test.status != ABTestStatus.DRAFT:
                raise HTTPException(status_code=409, detail="Удалять варианты можно только в черновике")
            variant = await self.repository.get_variant(test.id, position)
            if variant:
                if variant.file_path:
                    (self._media_root() / variant.file_path).unlink(missing_ok=True)
                await self.repository.delete_variant(variant)
                await self.db.commit()
        return await self.repository.get_for_user(user_id, test.id)  # type: ignore[return-value]

    async def delete(self, user_id: int, test_id: int) -> None:
        test = await self.repository.get_for_user(user_id, test_id)
        if not test:
            raise HTTPException(status_code=404, detail="A/B-тест не найден")
        async with self._operation_lock(test.connection_id, test.nm_id, user_id):
            test = await self.repository.get_for_update(user_id, test_id)
            if not test:
                raise HTTPException(status_code=404, detail="A/B-тест не найден")
            if (
                test.status == ABTestStatus.RUNNING
                or test.operation_state == ABTestOperationStatus.RECONCILIATION_REQUIRED.value
                or test.campaign_state in {"running", "starting", "stop_requested", "unknown"}
                or test.media_status in {"variant_applied", "swapping", "restoring"}
                or bool((test.media_state or {}).get("pending"))
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Нельзя удалить A/B-тест: сначала остановите кампанию и завершите сверку/восстановление фото",
                )
            file_paths = [variant.file_path for variant in test.variants if variant.file_path]
            if test.original_main_backup_path:
                file_paths.append(test.original_main_backup_path)
            file_paths.extend(self._media_state_paths(test.media_state))
            # Copy the immutable audit history before deleting the mutable
            # test aggregate. This makes deletion safe without erasing the
            # evidence required for financial/campaign investigations.
            await self.repository.archive_audit_events(test.id)
            await self.db.delete(test)
            await self.db.commit()
        for file_path in file_paths:
            try:
                (self._media_root() / str(file_path)).unlink(missing_ok=True)
            except OSError:
                # Database history is already removed; an orphaned local file
                # can be cleaned later without making the API return 500.
                continue

    @staticmethod
    def _media_state_paths(state: object) -> list[str]:
        paths: list[str] = []

        def walk(value: object) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == "path" and isinstance(item, str):
                        paths.append(item)
                    else:
                        walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(state)
        return list(dict.fromkeys(paths))

    @staticmethod
    def _variant_by_position(test: ABTest, position: int) -> ABTestVariant | None:
        return next((variant for variant in test.variants if variant.position == position), None)

    async def _variant_bytes(self, test: ABTest, variant: ABTestVariant, content: WBContentClient) -> tuple[bytes, str, str]:
        if variant.source_type == "control":
            path = self._media_root() / str(test.original_main_backup_path or "")
            if not path.is_file():
                raise RuntimeError("Не найдена резервная копия исходного изображения")
            return path.read_bytes(), mimetypes.guess_type(path.name)[0] or "image/jpeg", path.name
        if variant.file_path:
            path = self._media_root() / variant.file_path
            if not path.is_file():
                raise RuntimeError(f"Файл варианта {variant.position} не найден на сервере")
            mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
            return path.read_bytes(), mime, path.name
        if variant.source_url:
            data, mime = await content.download_image(variant.source_url)
            return data, mime, Path(variant.source_url.split("?", 1)[0]).name or f"variant_{variant.position}.jpg"
        raise RuntimeError(f"Вариант {variant.position} не содержит изображения")

    @staticmethod
    def _media_state(test: ABTest) -> dict[str, Any]:
        state = test.media_state
        return deepcopy(state) if isinstance(state, dict) else {}

    async def _set_media_state(self, test: ABTest, state: dict[str, Any]) -> None:
        # Assign a fresh object instead of mutating a JSON value in-place. This
        # makes SQLAlchemy detect every state transition on all supported DBs.
        test.media_state = deepcopy(state)
        if self.db is not None:
            await self.db.flush()

    @staticmethod
    def _image_extension(mime: str, url: str = "") -> str:
        extension = mimetypes.guess_extension((mime or "").split(";", 1)[0].strip().lower()) or ""
        if extension == ".jpe":
            extension = ".jpg"
        if not extension:
            extension = Path(urlsplit(url).path).suffix.lower()
        return extension if extension in ABTestService.ALLOWED_EXTENSIONS else ".jpg"

    def _state_file(self, relative_path: str) -> Path:
        target = (self._media_root() / str(relative_path)).resolve()
        try:
            target.relative_to(self._media_root())
        except ValueError as exc:
            raise RuntimeError("Некорректный путь резервной копии изображения") from exc
        return target

    async def _initialize_media_state(
        self,
        test: ABTest,
        original_media: list[str],
        variants: list[ABTestVariant],
        content: WBContentClient,
    ) -> dict[str, Any]:
        """Download every original slot before the first external mutation.

        The old implementation backed up only the main URL. A real swap must
        also preserve the image that currently occupies the source slot and
        every slot used as a parking slot. Backing up all slots (max 30) is
        deterministic and makes rollback safe even after several swaps.
        """
        base = Path("ab_tests") / str(test.id) / "original"
        backup_dir = self._media_root() / base
        backup_dir.mkdir(parents=True, exist_ok=True)
        backups: dict[str, dict[str, Any]] = {}
        for slot, url in enumerate(original_media, start=1):
            data, mime = await content.download_image(url, cache_bust=True)
            extension = self._image_extension(mime, url)
            filename = Path(urlsplit(url).path).name or f"original_{slot}{extension}"
            # A 200 response and an image/* MIME type are not enough for a
            # safe rollback: a truncated or HTML payload must be rejected
            # before the first card mutation. This is format validation only;
            # it deliberately does not compare media digests or read the card
            # back after an upload.
            filename, mime, data = self._prepare_image(
                filename,
                mime or "image/jpeg",
                data,
            )
            extension = Path(filename).suffix.lower() or extension
            relative = base / f"slot_{slot}{extension}"
            target = self._media_root() / relative
            target.write_bytes(data)
            backups[str(slot)] = {
                "path": str(relative),
                "url": url,
                "mime": mime or "image/jpeg",
                "file_name": filename,
                "sha256": hashlib.sha256(data).hexdigest(),
            }

        source_slots: set[int] = set()

        slot_state = {
            "backups": backups,
        }

        for variant in variants:
            if variant.source_type != "card":
                continue

            if not variant.source_url:
                continue

            source_slot = self._source_slot(
                slot_state,
                variant,
            )

            if source_slot is not None:
                source_slots.add(int(source_slot))
        # A custom upload is first copied to a slot which is not used as a
        # source variant. If all slots are selected, direct slot 1 replacement
        # is still safe because all originals are backed up above.
        parking_slot = next(
            (slot for slot in range(len(original_media), 1, -1) if slot not in source_slots),
            None,
        )
        state: dict[str, Any] = {
            "version": 1,
            "status": "original",
            "test_slot": 1,
            "parking_slot": parking_slot,
            "slot_count": len(original_media),
            "original_urls": list(original_media),
            "backups": backups,
            "shadows": {},
            "touched": [],
            "pending": None,
            "expected_slot_count": len(original_media),
            "expected_snapshot": {
                slot: {
                    "url": entry["url"],
                    "sha256": entry.get("sha256"),
                }
                for slot, entry in backups.items()
            },
            "expected_videos": WBContentClient.media_snapshot(await content.get_card(test.nm_id)).get("videos", []),
        }
        test.original_media = list(original_media)
        test.original_main_backup_path = backups["1"]["path"]
        test.media_status = "backed_up"
        await self._set_media_state(test, state)
        return state

    @staticmethod
    def _extract_photo_number(url: str) -> int | None:
        path = urlsplit(str(url or "").strip()).path

        # WB URL examples:
        # .../big/3.webp
        # .../c516x688/3.webp
        # .../c246x328/3.webp
        match = re.search(
            r"/(?:big|c\d+x\d+)/(\d+)\.[^/]+$",
            path,
        )

        if not match:
            return None

        try:
            return int(match.group(1))
        except (TypeError, ValueError):
            return None


    @classmethod
    def _source_slot(
        cls,
        state: dict[str, Any],
        variant: ABTestVariant,
    ) -> int | None:
        source_url = variant.source_url

        if variant.source_type != "card" or not source_url:
            return None

        # 1. Avval URL ichidan WB photo number'ni olamiz.
        photo_number = cls._extract_photo_number(source_url)

        if photo_number is not None:
            backups = state.get("backups") or {}

            if str(photo_number) in backups:
                return photo_number

        # 2. Fallback: eski URL comparison.
        source_canonical = cls._canonical_media_url(source_url)

        for raw_slot, entry in (state.get("backups") or {}).items():
            if not isinstance(entry, dict):
                continue

            backup_url = str(entry.get("url") or "")

            if (
                cls._canonical_media_url(backup_url)
                == source_canonical
            ):
                return int(raw_slot)

        return None

    def _slot_entry(self, state: dict[str, Any], slot: int, *, shadow: bool = True) -> dict[str, Any] | None:
        if shadow:
            entry = (state.get("shadows") or {}).get(str(slot))
            if isinstance(entry, dict):
                return entry
        entry = (state.get("backups") or {}).get(str(slot))
        return entry if isinstance(entry, dict) else None

    def _read_state_slot(
        self, state: dict[str, Any], slot: int, *, shadow: bool = True
    ) -> tuple[bytes, str, str]:
        entry = self._slot_entry(state, slot, shadow=shadow)
        if not entry or not entry.get("path"):
            raise RuntimeError(f"Не найдена резервная копия слота {slot}")
        path = self._state_file(str(entry["path"]))
        if not path.is_file():
            raise RuntimeError(f"Файл резервной копии слота {slot} отсутствует на сервере")
        return path.read_bytes(), str(entry.get("mime") or "image/jpeg"), str(entry.get("file_name") or path.name)

    def _write_shadow(
        self,
        test: ABTest,
        slot: int,
        data: bytes,
        mime: str,
        filename: str,
    ) -> dict[str, Any]:
        extension = self._image_extension(mime, filename)
        relative = (
            Path("ab_tests")
            / str(test.id)
            / "current"
            / f"slot_{slot}{extension}"
        )
        target = self._media_root() / relative
        target.parent.mkdir(parents=True, exist_ok=True)

        old_path = (
            self._media_state(test).get("shadows") or {}
        ).get(str(slot), {}).get("path")

        target.write_bytes(data)

        # Shadows may previously point to an original backup.
        # Never delete anything from original/.
        if old_path:
            old_path = str(old_path)
            current_prefix = f"ab_tests/{test.id}/current/"

            if old_path.startswith(current_prefix) and old_path != str(relative):
                self._state_file(old_path).unlink(missing_ok=True)

        return {
            "path": str(relative),
            "mime": mime or "image/jpeg",
            "file_name": filename or target.name,
        }

    async def _apply_variant(
            self,
            test: ABTest,
            variant: ABTestVariant,
            content: WBContentClient,
            promotion: WBPromotionClient,
        ) -> None:
            state = self._media_state(test)

            if state.get("version") == 1 and state.get("backups"):
                if promotion is None:
                    raise RuntimeError(
                        "Promotion client is required for the slot-based A/B media flow"
                    )

                await self._apply_variant_with_slot_swap(
                    test,
                    variant,
                    content,
                    state,
                    promotion,
                )
                return

            raise ABTestReconciliationRequired(
                "A/B test использует устаревшее состояние media_state "
                "и не может безопасно продолжить смену изображения.",
                incident_id=test.incident_id or self._incident_id(),
            )

        

    async def _verify_uploaded_image(
        self,
        test: ABTest,
        slot: int,
        expected_data: bytes,
        content: WBContentClient,
        *,
        attempts: int = 5,
        delay_seconds: int = 10,
        threshold: int = 10,
    ) -> bool:
        """
        Verify that WB CDN serves the exact image uploaded to the slot.

        Flow:
            upload
            -> wait for propagation
            -> fetch current card
            -> get current slot URL
            -> download CDN image
            -> compare pHash
        """

        expected_hash = self._perceptual_hash(expected_data)

        logger.info(
            "WB VERIFY EXPECTED "
            "test_id=%s nm_id=%s slot=%s "
            "bytes=%s sha256=%s phash=%s",
            test.id,
            test.nm_id,
            slot,
            len(expected_data),
            hashlib.sha256(expected_data).hexdigest(),
            expected_hash,
        )

        for attempt in range(1, attempts + 1):
            try:
                # -------------------------------------------------
                # 1. GET FRESH CARD
                # -------------------------------------------------

                card = await content.get_card(test.nm_id)

                if not card:
                    logger.warning(
                        "WB VERIFY CARD NOT FOUND "
                        "test_id=%s nm_id=%s slot=%s attempt=%s",
                        test.id,
                        test.nm_id,
                        slot,
                        attempt,
                    )

                else:
                    media = (
                        card.get("photos")
                        or card.get("media")
                        or []
                    )

                    if slot > len(media):
                        logger.warning(
                            "WB VERIFY SLOT NOT AVAILABLE "
                            "test_id=%s nm_id=%s slot=%s "
                            "photos=%s attempt=%s",
                            test.id,
                            test.nm_id,
                            slot,
                            len(media),
                            attempt,
                        )

                    else:
                        # -------------------------------------------------
                        # 2. GET CURRENT SLOT
                        # -------------------------------------------------

                        raw_media = media[slot - 1]

                        if isinstance(raw_media, dict):
                            url = (
                                raw_media.get("big")
                                or raw_media.get("hq")
                                or raw_media.get("c246x328")
                                or raw_media.get("c516x688")
                                or raw_media.get("url")
                            )
                        else:
                            url = str(raw_media)

                        url = str(url).strip() if url else ""

                        if not url:
                            logger.warning(
                                "WB VERIFY EMPTY URL "
                                "test_id=%s nm_id=%s slot=%s "
                                "attempt=%s",
                                test.id,
                                test.nm_id,
                                slot,
                                attempt,
                            )

                        else:
                            logger.info(
                                "WB VERIFY CDN URL "
                                "test_id=%s nm_id=%s slot=%s "
                                "attempt=%s url=%s",
                                test.id,
                                test.nm_id,
                                slot,
                                attempt,
                                url,
                            )

                            # -------------------------------------------------
                            # 3. DOWNLOAD CURRENT CDN IMAGE
                            # -------------------------------------------------

                            cdn_data, cdn_content_type = (
                                await content.download_image(
                                    url,
                                    cache_bust=True,
                                )
                            )

                            # -------------------------------------------------
                            # 4. VERIFY IMAGE
                            # -------------------------------------------------

                            actual_hash = self._perceptual_hash(
                                cdn_data
                            )

                            distance = (
                                expected_hash - actual_hash
                            )

                            logger.info(
                                "WB VERIFY RESULT "
                                "test_id=%s nm_id=%s slot=%s "
                                "attempt=%s "
                                "expected_phash=%s "
                                "actual_phash=%s "
                                "distance=%s "
                                "threshold=%s "
                                "expected_bytes=%s "
                                "actual_bytes=%s "
                                "actual_sha256=%s "
                                "content_type=%s",
                                test.id,
                                test.nm_id,
                                slot,
                                attempt,
                                expected_hash,
                                actual_hash,
                                distance,
                                threshold,
                                len(expected_data),
                                len(cdn_data),
                                hashlib.sha256(cdn_data).hexdigest(),
                                cdn_content_type,
                            )

                            if distance <= threshold:
                                logger.info(
                                    "WB VERIFY SUCCESS "
                                    "test_id=%s nm_id=%s slot=%s "
                                    "attempt=%s distance=%s",
                                    test.id,
                                    test.nm_id,
                                    slot,
                                    attempt,
                                    distance,
                                )

                                return True

                            logger.warning(
                                "WB VERIFY HASH MISMATCH "
                                "test_id=%s nm_id=%s slot=%s "
                                "attempt=%s distance=%s",
                                test.id,
                                test.nm_id,
                                slot,
                                attempt,
                                distance,
                            )

            except Exception as exc:
                logger.warning(
                    "WB VERIFY ATTEMPT FAILED "
                    "test_id=%s nm_id=%s slot=%s "
                    "attempt=%s error=%s",
                    test.id,
                    test.nm_id,
                    slot,
                    attempt,
                    self._safe_error(exc),
                )

            # -------------------------------------------------
            # WAIT BEFORE NEXT FRESH CARD/CDN CHECK
            # -------------------------------------------------

            if attempt < attempts:
                logger.info(
                    "WB VERIFY RETRY "
                    "test_id=%s nm_id=%s slot=%s "
                    "next_attempt=%s wait=%ss",
                    test.id,
                    test.nm_id,
                    slot,
                    attempt + 1,
                    delay_seconds,
                )

                await asyncio.sleep(delay_seconds)

        logger.error(
            "WB VERIFY FAILED "
            "test_id=%s nm_id=%s slot=%s "
            "attempts=%s",
            test.id,
            test.nm_id,
            slot,
            attempts,
        )

        return False

    async def _process_pending_image_retry(
        self,
        test: ABTest,
        content: WBContentClient,
        promotion: WBPromotionClient,
    ) -> bool:
        """
        Re-upload an image one hour after the first verification failure.

        The campaign must not serve impressions while the image is unresolved.
        After a successful second verification the existing campaign is started
        again and status 9 is explicitly confirmed.
        """

        state = self._media_state(test)
        pending = state.get("pending") or {}
        verification = pending.get("verification") or {}

        if verification.get("status") != "waiting_reupload":
            return False

        next_retry_at = verification.get("next_retry_at")

        if not next_retry_at:
            return False

        now = self._now().timestamp()

        if now < float(next_retry_at):
            return True

        reupload_count = int(
            verification.get("reupload_count") or 0
        )

        if reupload_count > self.IMAGE_VERIFY_MAX_REUPLOADS:
            test.status = ABTestStatus.FAILED
            test.operation_state = (
                ABTestOperationStatus.RECONCILIATION_REQUIRED.value
            )
            test.incident_id = test.incident_id or self._incident_id()

            raise ABTestReconciliationRequired(
                "Повторная загрузка изображения уже выполнялась, "
                "но Wildberries не подтвердил новое фото.",
                incident_id=test.incident_id,
            )

        # Before retry, prove that the campaign is not serving.
        campaign_status = await promotion.get_campaign_status(
            test.wb_campaign_id,
            refresh=True,
        )

        if campaign_status == 9:
            await self._pause_campaign_confirmed(
                test,
                promotion,
            )
        elif campaign_status not in {4, 7, 8, 11}:
            test.operation_state = (
                ABTestOperationStatus.RECONCILIATION_REQUIRED.value
            )
            test.incident_id = test.incident_id or self._incident_id()

            raise ABTestReconciliationRequired(
                f"Нельзя безопасно повторить загрузку изображения: "
                f"статус кампании WB = {campaign_status}.",
                incident_id=test.incident_id,
            )

        targets = pending.get("targets") or {}
        raw_slots = pending.get("slots") or []

        if not isinstance(targets, dict) or not targets:
            raise ABTestReconciliationRequired(
                "Повреждён журнал повторной загрузки изображения: targets отсутствуют.",
                incident_id=test.incident_id or self._incident_id(),
            )

        if not isinstance(raw_slots, list) or not raw_slots:
            raise ABTestReconciliationRequired(
                "Повреждён журнал повторной загрузки изображения: slots отсутствуют.",
                incident_id=test.incident_id or self._incident_id(),
            )

        try:
            pending_slots = {int(slot) for slot in raw_slots}
        except (TypeError, ValueError) as exc:
            raise ABTestReconciliationRequired(
                "Повреждён журнал повторной загрузки изображения: некорректные slots.",
                incident_id=test.incident_id or self._incident_id(),
            ) from exc

        target_slots = set()

        for raw_slot, target in targets.items():
            try:
                target_slot = int(raw_slot)
            except (TypeError, ValueError) as exc:
                raise ABTestReconciliationRequired(
                    "Повреждён журнал повторной загрузки изображения: "
                    f"некорректный target slot={raw_slot}.",
                    incident_id=test.incident_id or self._incident_id(),
                ) from exc

            if not isinstance(target, dict):
                raise ABTestReconciliationRequired(
                    f"Повреждён журнал изображения: target для слота {target_slot} "
                    "имеет некорректный формат.",
                    incident_id=test.incident_id or self._incident_id(),
                )

            target_slots.add(target_slot)

        if pending_slots != target_slots:
            raise ABTestReconciliationRequired(
                "Повреждён журнал повторной загрузки изображения: "
                f"slots={sorted(pending_slots)}, "
                f"targets={sorted(target_slots)}.",
                incident_id=test.incident_id or self._incident_id(),
            )

        for slot in sorted(pending_slots):
            target = targets.get(str(slot))
            if target is None:
                target = targets.get(slot)

            if not isinstance(target, dict):
                raise ABTestReconciliationRequired(
                    f"Не найден target для повторной загрузки слота {slot}.",
                    incident_id=test.incident_id or self._incident_id(),
                )

            shadow_path = target.get("shadow_path")

            if not shadow_path:
                raise ABTestReconciliationRequired(
                    f"Не найден файл для повторной загрузки слота {slot}.",
                    incident_id=test.incident_id or self._incident_id(),
                )

            path = self._state_file(str(shadow_path))

            if not path.is_file():
                raise ABTestReconciliationRequired(
                    f"Файл для повторной загрузки слота {slot} отсутствует.",
                    incident_id=test.incident_id or self._incident_id(),
                )

            data = path.read_bytes()

            await content.upload_media_file(
                nm_id=test.nm_id,
                photo_number=slot,
                content=data,
                filename=target.get("file_name") or path.name,
                content_type=target.get("mime") or "image/jpeg",
            )

            verified = await self._verify_uploaded_image(
                test=test,
                slot=slot,
                expected_data=data,
                content=content,
                attempts=self.IMAGE_VERIFY_ATTEMPTS,
                delay_seconds=self.IMAGE_VERIFY_DELAY_SECONDS,
                threshold=self.IMAGE_PHASH_THRESHOLD,
            )

            if not verified:
                verification["reupload_count"] = reupload_count + 1

                await self._stop_campaign_confirmed(
                    test,
                    promotion,
                )

                test.status = ABTestStatus.FAILED
                test.campaign_state = "stopped"
                test.operation_state = (
                    ABTestOperationStatus.RECONCILIATION_REQUIRED.value
                )
                test.media_status = "unknown"
                test.incident_id = (
                    test.incident_id or self._incident_id()
                )

                test.last_error = (
                    "Реклама остановлена: Wildberries не подтвердил "
                    "замену изображения после повторной загрузки. "
                    f"Инцидент {test.incident_id}."
                )[:2000]

                state["pending"] = pending
                state["status"] = "verification_failed"
                verification["status"] = "failed"
                verification["next_retry_at"] = None
                pending["verification"] = verification

                await self._set_media_state(test, state)
                await self.db.commit()

                raise ABTestReconciliationRequired(
                    test.last_error,
                    incident_id=test.incident_id,
                )

        # Image verification succeeded. Resume the SAME campaign.
        campaign_status = await promotion.get_campaign_status(
            test.wb_campaign_id,
            refresh=True,
        )

        if campaign_status in self.RESUMABLE_CAMPAIGN_STATUSES:
            test.campaign_state = "starting"
            await self.db.flush()

            await promotion.start_campaign(
                test.wb_campaign_id,
            )

            campaign_status = await self._confirm_campaign_active(
                promotion,
                test.wb_campaign_id,
            )

        if campaign_status != 9:
            test.operation_state = (
                ABTestOperationStatus.RECONCILIATION_REQUIRED.value
            )
            test.incident_id = test.incident_id or self._incident_id()

            raise ABTestReconciliationRequired(
                "Изображение успешно загружено и проверено, "
                "но существующая кампания WB не была повторно запущена.",
                incident_id=test.incident_id,
            )


        variant_position = int(
            pending.get("variant_position")
            or test.current_variant_order
            or 1
        )

        verification["status"] = "verified"
        verification["next_retry_at"] = None
        verification["reupload_count"] = reupload_count

        pending["verification"] = verification

        # MUHIM (Sergey hisobotidagi #3): oddiy foto almashtirish yo'lida
        # bajariladigan xuddi shu yangilanishlarni bu yerda ham bajarish
        # kerak — aks holda "faol variant" bazada eskicha qolib, keyingi
        # ko'rsatishlar (statistika) noto'g'ri fotoga yozilib ketaveradi
        # (masalan, 320/0 o'rniga to'g'risi 300/20 bo'lishi kerak edi).
        # Shadow-fayllar xaritasini ham yangilaymiz, aks holda keyingi
        # o'tishda eski (masalan .webp) fayl nomiga havola qolib, "fayl
        # topilmadi" xatosi chiqadi.
        state["shadows"] = {
            **(state.get("shadows") or {}),
            **{
                str(slot): {
                    "path": str(target.get("shadow_path")),
                    "mime": target.get("mime"),
                    "file_name": target.get("file_name"),
                }
                for slot, target in targets.items()
            },
        }
        state["pending"] = None
        state["current_variant_position"] = variant_position
        state["status"] = "variant_applied"

        test.status = ABTestStatus.RUNNING
        test.campaign_state = "running"
        test.operation_state = (
            ABTestOperationStatus.SUCCEEDED.value
        )
        test.current_variant_order = variant_position
        test.media_status = "variant_applied"
        test.last_error = None
        test.incident_id = None

        await self._set_media_state(test, state)
        # The retry writes a new slot arrangement just like the ordinary
        # variant path. Refresh the durable URL/hash snapshot before the
        # next scheduler tick or stop; otherwise our own successful writes
        # are mistaken for an external card edit during restore.
        await self._refresh_expected_media_snapshot(test, content)
        await self.db.commit()

        logger.info(
            "WB image reupload verified and campaign resumed "
            "test_id=%s nm_id=%s variant=%s",
            test.id,
            test.nm_id,
            variant_position,
        )

        return True


    async def _apply_variant_with_slot_swap(
        self,
        test: ABTest,
        variant: ABTestVariant,
        content: WBContentClient,
        state: dict[str, Any],
        promotion: WBPromotionClient,
    ) -> None:
        await self._assert_media_snapshot_current(test, content)
        source_slot = self._source_slot(
            state,
            variant,
        )

        # ---------------------------------------------------------
        # Card source bo‘lsa, source slot TOPILISHI SHART.
        # Topilmasa parking fallback qilinmasin.
        # ---------------------------------------------------------
        if variant.source_type == "card" and source_slot is None:
            test.incident_id = (
                test.incident_id or self._incident_id()
            )

            raise ABTestReconciliationRequired(
                (
                    "Не удалось определить исходный слот "
                    f"изображения WB: {variant.source_url}"
                ),
                incident_id=test.incident_id,
            )

        if source_slot:
            data, mime, filename = self._read_state_slot(
                state,
                source_slot,
            )
        else:
            data, mime, filename = await self._variant_bytes(
                test,
                variant,
                content,
            )

        filename, mime, data = self._prepare_image(
            filename,
            mime,
            data,
            verify_integrity=False,
        )

        self._validate_image(
            filename,
            mime,
            data,
        )

        changed_slots: set[int] = {1}

        current_main, current_main_mime, current_main_name = (
            self._read_state_slot(
                state,
                1,
            )
        )

        # ---------------------------------------------------------
        # OLD vs NEW safety check.
        # ---------------------------------------------------------
        if variant.source_type != "control":
            similar, distance = self._images_similar(
                current_main,
                data,
                threshold=self.IMAGE_PHASH_THRESHOLD,
            )

            logger.info(
                "A/B OLD NEW IMAGE CHECK "
                "test_id=%s nm_id=%s variant=%s "
                "distance=%s threshold=%s similar=%s",
                test.id,
                test.nm_id,
                variant.position,
                distance,
                self.IMAGE_PHASH_THRESHOLD,
                similar,
            )

            if similar:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Новый вариант изображения слишком похож "
                        "на текущее изображение"
                    ),
                )

        targets: dict[int, tuple[bytes, str, str]] = {
            1: (
                data,
                mime,
                filename,
            )
        }

        kind = "replace"

        # ---------------------------------------------------------
        # REAL TWO-WAY SWAP
        # ---------------------------------------------------------
        if (
            variant.source_type == "card"
            and source_slot
            and source_slot != 1
        ):
            # slot 1 <- source_slot image
            # source_slot <- old slot 1 image
            targets[source_slot] = (
                current_main,
                current_main_mime,
                current_main_name,
            )

            changed_slots.add(source_slot)

            kind = "swap"

        # ---------------------------------------------------------
        # CUSTOM UPLOAD
        # ---------------------------------------------------------
        elif variant.source_type == "upload":
            parking_slot = state.get("parking_slot")

            if (
                parking_slot
                and int(parking_slot) != 1
            ):
                parking_slot = int(parking_slot)

                targets[parking_slot] = (
                    current_main,
                    current_main_mime,
                    current_main_name,
                )

                changed_slots.add(parking_slot)

                kind = "replace_with_parking"
       

        target_meta: dict[int, dict[str, Any]] = {}
        shadow_entries: dict[str, dict[str, Any]] = {}
        for slot, (slot_data, slot_mime, slot_name) in targets.items():
            shadow_entries[str(slot)] = self._write_shadow(test, slot, slot_data, slot_mime, slot_name)
            target_meta[slot] = {
                # Keep the local shadow path in the journal so a worker crash
                # can replay the same slot writes without probing WB media.
                "shadow_path": shadow_entries[str(slot)]["path"],
                "mime": slot_mime,
                "file_name": slot_name,
                "original_url": (state.get("backups") or {}).get(str(slot), {}).get("url"),
            }

        state["touched"] = sorted({int(slot) for slot in state.get("touched") or []} | changed_slots)
        state["pending"] = {
            "kind": kind,
            "variant_position": variant.position,
            "slots": sorted(changed_slots),
            "targets": {
                str(slot): {
                    key: value
                    for key, value in meta.items()
                }
                for slot, meta in target_meta.items()
            },
            "verification": {
                "status": "pending",
                "attempt": 0,
                "reupload_count": 0,
                "next_retry_at": None,
            },
        }
        await self._set_media_state(test, state)
        await self.db.commit()

        # Write the non-main slot first. If the second request fails, the main
        # photo is still the old one and the full backup can restore both slots.
        upload_order = [slot for slot in sorted(targets, reverse=True) if slot != 1] + [1]
        # One logical operation gets one external write sequence.
        # Upload success is only the first step; every changed slot is
        # confirmed through CDN/pHash before the operation is committed.
        for slot in upload_order:
            slot_data, slot_mime, slot_name = targets[slot]

            logger.info(
                "WB UPLOAD START "
                "test_id=%s nm_id=%s slot=%s "
                "bytes=%s sha256=%s",
                test.id,
                test.nm_id,
                slot,
                len(slot_data),
                hashlib.sha256(slot_data).hexdigest(),
            )

            await content.upload_media_file(
                nm_id=test.nm_id,
                photo_number=slot,
                content=slot_data,
                filename=slot_name,
                content_type=slot_mime,
            )

            logger.info(
                "WB UPLOAD ACCEPTED "
                "test_id=%s nm_id=%s slot=%s",
                test.id,
                test.nm_id,
                slot,
            )

            # Muhim:
            # WB API upload 200 qaytargani bilan CDN darhol yangi
            # image'ni bermasligi mumkin.
            await asyncio.sleep(30)

            verified = await self._verify_uploaded_image(
                test=test,
                slot=slot,
                expected_data=slot_data,
                content=content,
                attempts=self.IMAGE_VERIFY_ATTEMPTS,
                delay_seconds=self.IMAGE_VERIFY_DELAY_SECONDS,
                threshold=self.IMAGE_PHASH_THRESHOLD,
            )

            if not verified:
                verification = (
                    state.get("pending", {}).get("verification") or {}
                )

                verification["status"] = "waiting_reupload"
                verification["reupload_count"] = 1
                verification["next_retry_at"] = (
                    self._now().timestamp()
                    + self.IMAGE_REUPLOAD_DELAY_SECONDS
                )

                state["pending"]["verification"] = verification
                state["status"] = "waiting_image_reupload"

                test.media_status = "waiting_image_reupload"
                test.campaign_state = "paused"
                test.operation_state = (
                    ABTestOperationStatus.IN_PROGRESS.value
                )

                await self._set_media_state(test, state)
                await self.db.commit()

                logger.warning(
                    "WB IMAGE VERIFY FAILED; reupload scheduled "
                    "test_id=%s nm_id=%s slot=%s retry_in=%ss",
                    test.id,
                    test.nm_id,
                    slot,
                    self.IMAGE_REUPLOAD_DELAY_SECONDS,
                )

                return

                logger.warning(
                    "WB IMAGE VERIFY FAILED; reupload scheduled "
                    "test_id=%s nm_id=%s slot=%s retry_in=%ss",
                    test.id,
                    test.nm_id,
                    slot,
                    self.IMAGE_REUPLOAD_DELAY_SECONDS,
                )

                return
            
        state["shadows"] = {**(state.get("shadows") or {}), **shadow_entries}
        state["pending"] = None
        state["current_variant_position"] = variant.position
        state["status"] = "variant_applied"
        state["expected_slot_count"] = int(state.get("slot_count") or len(state.get("backups") or {}))
        test.media_status = "variant_applied"
        await self._set_media_state(test, state)
        await self._refresh_expected_media_snapshot(test, content)
        state = self._media_state(test)
        variant.wb_url = None
        logger.info(
            "A/B variant applied with slot swap test_id=%s nm_id=%s position=%s kind=%s slots=%s",
            test.id,
            test.nm_id,
            variant.position,
            kind,
            sorted(changed_slots),
        )

    async def _restore_original(
        self,
        test: ABTest,
        content: WBContentClient,
    ) -> None:
        state = self._media_state(test)
        if state.get("version") == 1 and state.get("backups"):
            if state.get("pending"):
                await self._assert_media_snapshot_safe_for_restore(test, content)
            else:
                await self._assert_media_snapshot_current(test, content)
            touched = {int(slot) for slot in state.get("touched") or []}
            pending = state.get("pending") or {}
            touched.update(int(slot) for slot in pending.get("slots") or [])
            if not touched:
                test.media_status = "original"
                return

            state["status"] = "restoring"
            test.media_status = "restoring"
            await self._set_media_state(test, state)

            targets: dict[int, dict[str, Any]] = {}
            for slot in sorted(touched):
                entry = (state.get("backups") or {}).get(str(slot))
                if not entry:
                    raise RuntimeError(f"Не найдена резервная копия исходного слота {slot}")
                data, mime, filename = self._read_state_slot(state, slot, shadow=False)
                if not data:
                    raise RuntimeError(f"Резервная копия исходного слота {slot} пуста")
                targets[slot] = {
                    "data": data,
                    "mime": mime,
                    "filename": filename,
                    "original_url": entry.get("url"),
                }

            logger.info(
                "A/B restoring exact original slots test_id=%s nm_id=%s slots=%s",
                test.id,
                test.nm_id,
                sorted(touched),
            )
            # Persist the restore intent before the first external write. If
            # the worker stops between requests, reconciliation can continue
            # the same restore operation without creating a new campaign.
            state["pending"] = {
                "kind": "restore",
                "slots": sorted(touched),
                "targets": {
                    str(slot): {
                        "original_url": target.get("original_url"),
                        "shadow_path": (state.get("backups") or {}).get(str(slot), {}).get("path"),
                    }
                    for slot, target in targets.items()
                },
            }
            await self._set_media_state(test, state)
            await self.db.commit()
            # Restore secondary slots first and the main slot last so the card
            # never spends a long interval with a new main and an old source.
            restore_order = sorted(targets, reverse=True)
            # Restoration is also a single logical write sequence. Successful
            # upload responses are authoritative; no card/CDN read-back is
            # used to decide whether the restoration succeeded.
            for slot in restore_order:
                target = targets[slot]

                await content.upload_media_file(
                    nm_id=test.nm_id,
                    photo_number=slot,
                    content=target["data"],
                    filename=target["filename"],
                    content_type=target["mime"],
                )

                verified = await self._verify_uploaded_image(
                    test=test,
                    slot=slot,
                    expected_data=target["data"],
                    content=content,
                    attempts=self.IMAGE_VERIFY_ATTEMPTS,
                    delay_seconds=self.IMAGE_VERIFY_DELAY_SECONDS,
                    threshold=self.IMAGE_PHASH_THRESHOLD,
                )

                if not verified:
                    test.media_status = "unknown"
                    state["status"] = "restore_verification_failed"
                    await self._set_media_state(test, state)

                    raise ABTestReconciliationRequired(
                        f"Исходное изображение слота {slot} "
                        "не подтверждено после восстановления.",
                        incident_id=test.incident_id or self._incident_id(),
                    )
            state["shadows"] = {
                str(slot): {
                    "path": (state.get("backups") or {})[str(slot)]["path"],
                    "mime": (state.get("backups") or {})[str(slot)].get("mime") or "image/jpeg",
                    "file_name": (state.get("backups") or {})[str(slot)].get("file_name") or f"original_{slot}.jpg",
                }
                for slot in touched
            }
            state["touched"] = []
            state["pending"] = None
            state["current_variant_position"] = 0
            state["status"] = "restored"
            state["expected_slot_count"] = len((state.get("original_urls") or []))
            state["expected_snapshot"] = {
                slot: {
                    "url": entry.get("url") or "",
                }
                for slot, entry in (state.get("backups") or {}).items()
            }
            test.media_status = "restored"
            await self._set_media_state(test, state)
            await self._refresh_expected_media_snapshot(test, content)
            return

        # Compatibility path for tests and records created before 0007.
        original = [str(url).strip() for url in (test.original_media or []) if str(url).strip()]
        logger.info("A/B restore original media test_id=%s nm_id=%s photos=%s", test.id, test.nm_id, len(original))
        if original:
            await content.save_media(nm_id=test.nm_id, urls=original)
        elif test.original_main_backup_path:
            path = self._media_root() / test.original_main_backup_path
            if path.is_file():
                data = path.read_bytes()
                mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"

                await content.upload_media_file(
                    nm_id=test.nm_id,
                    photo_number=1,
                    content=data,
                    filename=path.name,
                    content_type=mime,
                )

                verified = await self._verify_uploaded_image(
                    test=test,
                    slot=1,
                    expected_data=data,
                    content=content,
                    attempts=self.IMAGE_VERIFY_ATTEMPTS,
                    delay_seconds=self.IMAGE_VERIFY_DELAY_SECONDS,
                    threshold=self.IMAGE_PHASH_THRESHOLD,
                )

                if not verified:
                    raise ABTestReconciliationRequired(
                        "Исходное главное изображение не подтверждено после восстановления.",
                        incident_id=test.incident_id or self._incident_id(),
                    )
        else:
            raise RuntimeError("У теста отсутствует резервная копия исходных изображений")

        logger.info(
            "A/B original media restore accepted test_id=%s nm_id=%s photos=%s",
            test.id,
            test.nm_id,
            len(original),
        )

    async def _pause_campaign_confirmed(
        self,
        test: ABTest,
        promotion: WBPromotionClient,
    ) -> int | None:
        """
        Pause an active WB campaign and confirm status 11.

        HTTP 200 from /adv/v0/pause only means that WB accepted the request.
        The campaign is considered paused only after promotion/count reports 11.
        """
        campaign_id = test.wb_campaign_id

        if not campaign_id:
            test.campaign_state = "not_created"
            return None

        try:
            initial_status = await promotion.get_campaign_status(
                campaign_id,
                refresh=True,
            )
        except Exception as exc:
            test.campaign_state = "unknown"
            test.operation_state = (
                ABTestOperationStatus.RECONCILIATION_REQUIRED.value
            )
            test.incident_id = test.incident_id or self._incident_id()

            raise ABTestReconciliationRequired(
                "Не удалось определить текущий статус рекламной кампании "
                "перед изменением изображения. Операция остановлена.",
                incident_id=test.incident_id,
            ) from exc

        # Already paused.
        if initial_status == 11:
            test.campaign_state = "paused"
            return 11

        # Campaign is not serving. It is already safe to modify media.
        if initial_status in {4, 7, 8, -1}:
            test.campaign_state = "stopped"
            return initial_status

        # We only send pause when WB confirms that campaign is active.
        if initial_status != 9:
            test.campaign_state = "unknown"
            test.operation_state = (
                ABTestOperationStatus.RECONCILIATION_REQUIRED.value
            )
            test.incident_id = test.incident_id or self._incident_id()

            raise ABTestReconciliationRequired(
                f"Нельзя безопасно изменить изображение: "
                f"Wildberries вернул неожиданный статус кампании "
                f"{initial_status}.",
                incident_id=test.incident_id,
            )

        before_pause = self._state_snapshot(test)
        test.campaign_state = "pause_requested"
        await self._audit(test, "campaign_pause_requested", before_pause, details={"campaign_id": int(campaign_id)})
        await self.db.flush()

        pause_error: Exception | None = None

        try:
            await promotion.pause_campaign(campaign_id)
        except Exception as exc:
            pause_error = exc

        last_status: int | None = None
        last_error: Exception | None = None

        for attempt, delay in enumerate(
            self.CAMPAIGN_PAUSE_CONFIRM_DELAYS
        ):
            if delay:
                await asyncio.sleep(delay)

            try:
                last_status = await promotion.get_campaign_status(
                    campaign_id,
                    refresh=True,
                )
                last_error = None
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "WB campaign status read failed after pause "
                    "campaign_id=%s attempt=%s/%s error=%s",
                    campaign_id,
                    attempt + 1,
                    len(self.CAMPAIGN_PAUSE_CONFIRM_DELAYS),
                    self._safe_error(exc),
                )
                continue

            if last_status == 11:
                before_confirm = self._state_snapshot(test)
                test.campaign_state = "paused"
                await self._audit(test, "campaign_pause_confirmed", before_confirm, details={"campaign_id": int(campaign_id), "status": 11})
                return 11

            # If WB moved it into another known non-serving state,
            # it is still safe to modify the media.
            if last_status in {4, 7, 8, -1}:
                test.campaign_state = "paused"
                return last_status

        test.campaign_state = (
            "running" if last_status == 9 else "unknown"
        )
        test.operation_state = (
            ABTestOperationStatus.RECONCILIATION_REQUIRED.value
        )
        test.incident_id = test.incident_id or self._incident_id()

        message = (
            "Wildberries не подтвердил остановку показов перед "
            "изменением изображения."
        )

        if pause_error:
            message += (
                f" Причина запроса: {self._safe_error(pause_error)}."
            )

        if last_error and last_status is None:
            message += (
                f" Причина проверки: {self._safe_error(last_error)}."
            )

        raise ABTestReconciliationRequired(
            message,
            incident_id=test.incident_id,
        )

    async def _stop_campaign_confirmed(self, test: ABTest, promotion: WBPromotionClient) -> None:
        """Request a stop and prove that the campaign is no longer active.

        A successful HTTP response from ``adv/v0/stop`` is only an accepted
        request. The campaign list is the read model we use to decide whether
        it is safe to finish the experiment or retry cleanup.
        """
        campaign_id = test.wb_campaign_id
        if not campaign_id:
            test.campaign_state = "not_created"
            return

        # A campaign created during preflight may already be in a known
        # non-serving state. Calling /adv/v0/stop for such a campaign makes
        # WB return HTTP 400 even though there is nothing left to stop. Read
        # the state first and only send the mutation when the campaign can
        # actually be active.
        try:
            initial_status = await promotion.get_campaign_status(campaign_id, refresh=True)
        except Exception:
            # If the read model is temporarily unavailable, keep the old
            # conservative path: send stop and require a read-back below.
            initial_status = None
        if initial_status in self.NON_ACTIVE_CAMPAIGN_STATUSES:
            test.campaign_state = "stopped"
            return

        before_stop = self._state_snapshot(test)
        test.campaign_state = "stop_requested"
        await self._audit(test, "campaign_stop_requested", before_stop, details={"campaign_id": int(campaign_id)})
        await self.db.flush()
        stop_error: Exception | None = None
        try:
            await promotion.stop_campaign(campaign_id)
        except Exception as exc:
            # The stop request may have reached WB even when the client did
            # not receive the response. Always reconcile before deciding.
            stop_error = exc

        campaign_status: int | None = None
        last_error: Exception | None = None
        for attempt, delay in enumerate(self.CAMPAIGN_STOP_CONFIRM_DELAYS):
            if delay:
                await asyncio.sleep(delay)
            try:
                campaign_status = await promotion.get_campaign_status(campaign_id, refresh=True)
                last_error = None
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "WB campaign status read failed after stop campaign_id=%s attempt=%s/%s error=%s",
                    campaign_id,
                    attempt + 1,
                    len(self.CAMPAIGN_STOP_CONFIRM_DELAYS),
                    self._safe_error(exc),
                )
                continue

            # 9 is the active state used by the Promotion API. The other
            # known states are non-serving. Unknown/empty state remains an
            # unresolved incident instead of being called "stopped".
            if campaign_status in self.NON_ACTIVE_CAMPAIGN_STATUSES:
                before_confirm = self._state_snapshot(test)
                test.campaign_state = "stopped"
                await self._audit(test, "campaign_stop_confirmed", before_confirm, details={"campaign_id": int(campaign_id), "status": int(campaign_status)})
                return
            if campaign_status == 9:
                test.campaign_state = "running"

        if campaign_status == 9:
            test.campaign_state = "running"
        else:
            test.campaign_state = "unknown"
        test.operation_state = ABTestOperationStatus.RECONCILIATION_REQUIRED.value
        test.incident_id = test.incident_id or self._incident_id()
        message = "Wildberries не подтвердил остановку рекламной кампании. Повторный запуск заблокирован."
        if stop_error:
            message = f"{message} Причина запроса: {self._safe_error(stop_error)}"
        if last_error and campaign_status is None:
            message = f"{message} Причина проверки: {self._safe_error(last_error)}"
        raise ABTestReconciliationRequired(message, incident_id=test.incident_id)

    @staticmethod
    def _can_resume_existing_campaign(test: ABTest, operation: ABTestOperation | None) -> bool:
        """Allow retrying a pre-start failure without creating another campaign."""
        if not operation or not test.wb_campaign_id or test.status != ABTestStatus.DRAFT:
            return False
        # "starting" qo'shildi: WB start_campaign so'rovi yuborilgan, lekin
        # hali tasdiqlanmagan/rad etilgan holatda kampaniya shu holatda qoladi
        # (qarang: start() dagi campaign_start_sent tarmog'i).
        if test.started_at or test.campaign_state not in {"created", "stopped", "starting"}:
            return False
        phase = (operation.response_snapshot or {}).get("phase")
        operation_status = operation.status.value if isinstance(operation.status, ABTestOperationStatus) else str(operation.status)
        if phase == "paused_minimum_bid":
            return True
        return phase in {
            "campaign_created",
            "campaign_reused",
            "campaign_reused_minimum_bid",
            "budget_deposit_sent",
            "budget_deposited",
            "budget_deposit_pending",
            "budget_deposit_rejected",
            # WB start_campaign so'rovi yuborilgan, lekin tasdiqlanmagan
            # (masalan, WB rad etgan yoki status 9 tasdiqlanmagan) holat.
            # Bu bo'lmasa, xuddi shu ssenariyda ikkinchi marta pullangan
            # yangi kampaniya yaratilib ketaveradi (Sergey hisobotidagi #4).
            "campaign_start_sent",
        } and operation_status != ABTestOperationStatus.SUCCEEDED.value

    @staticmethod
    def _validate_card_eligibility(card: dict[str, Any] | None) -> None:
        if not card:
            raise HTTPException(status_code=409, detail="Карточка Wildberries недоступна")
        raw = card
        # Provider schemas differ; only act on fields when WB actually sends them.
        for key in ("isDeleted", "deleted", "is_deleted", "blocked", "isBlocked", "blockedBySeller"):
            if raw.get(key) is True:
                raise HTTPException(status_code=409, detail="Карточка WB заблокирована/удалена. Запуск A/B-теста запрещён.")
        status = str(raw.get("status") or raw.get("state") or "").strip().lower()
        if status in {"blocked", "deleted", "archived", "unavailable", "disabled"}:
            raise HTTPException(status_code=409, detail=f"Карточка WB недоступна для рекламы: {status}.")
        if raw.get("advertisingAvailable") is False or raw.get("advertising_available") is False:
            raise HTTPException(status_code=409, detail="Карточка WB сейчас недоступна для рекламы.")
        # If explicit stock totals are present, a zero-stock item is unsafe to advertise.
        stock_values: list[float] = []

        def collect_stock(value: object) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    if str(key).lower() in {"stock", "totalstock", "total_stock", "qty", "quantity", "amount"} and isinstance(child, (int, float)) and not isinstance(child, bool):
                        stock_values.append(float(child))
                    elif isinstance(child, (dict, list)):
                        collect_stock(child)
            elif isinstance(value, list):
                for child in value:
                    collect_stock(child)

        collect_stock(raw)
        if stock_values and max(stock_values) <= 0:
            raise HTTPException(status_code=409, detail="У карточки WB нет доступного остатка. Запуск рекламы запрещён.")

    async def _assert_card_advertisable(self, card: dict[str, Any], promotion: WBPromotionClient, nm_id: int) -> None:
        checker = getattr(promotion, "is_card_advertisable", None)
        if not callable(checker):
            raise HTTPException(status_code=503, detail="Невозможно подтвердить доступность карточки для рекламы. Запуск заблокирован.")
        subject_id = None
        for key in ("subjectID", "subjectId", "subject_id"):
            try:
                if card.get(key) is not None:
                    subject_id = int(card[key])
                    break
            except (TypeError, ValueError):
                raise HTTPException(status_code=409, detail="WB вернул некорректную категорию карточки")
        try:
            eligible = await checker(int(nm_id), subject_id=subject_id)
        except Exception as exc:
            raise self._api_error(exc) from exc
        if eligible is not True:
            raise HTTPException(status_code=409, detail="Карточка WB сейчас недоступна для рекламы; запуск заблокирован.")

    async def start(
        self,
        user_id: int,
        test_id: int,
        request: ABTestStartRequest,
        *,
        idempotency_key: str | None = None,
    ) -> ABTest:
        test = await self.repository.get_for_user(user_id, test_id)
        if not test:
            raise HTTPException(status_code=404, detail="A/B-тест не найден")
        async with self._operation_lock(test.connection_id, test.nm_id, user_id):
            test = await self.repository.get_for_update(user_id, test_id)
            if not test:
                raise HTTPException(status_code=404, detail="A/B-тест не найден")
            operation_key = str(idempotency_key or f"start:test:{test.id}").strip()
            if not operation_key or len(operation_key) > 128:
                raise HTTPException(status_code=422, detail="Некорректный ключ операции запуска")

            # If the response to a successful start was lost, the client is
            # allowed to repeat the exact same logical request. Return the
            # current durable state instead of rejecting it merely because
            # the test is no longer a draft. A different key must never turn
            # a running test into a second external campaign.
            existing_operation = await self.repository.get_operation(operation_key)
            if existing_operation:
                fingerprint = self._operation_fingerprint(test, request)
                if existing_operation.fingerprint != fingerprint:
                    raise HTTPException(
                        status_code=409,
                        detail="Параметры запуска изменились. Требуется новое подтверждение пользователя.",
                    )
                if existing_operation.status == ABTestOperationStatus.SUCCEEDED:
                    return test
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "reconciliation_required",
                        "message": "Предыдущий запуск ещё не подтверждён. Сначала выполните сверку операции.",
                        "operation_id": existing_operation.id,
                        "incident_id": test.incident_id,
                    },
                )
            # A minimum-bid pause is a resumable running lifecycle, not a new
            # draft. Require an explicit CPM confirmation and continue the
            # same campaign without a second deposit or counter reset.
            pause_state = self._media_state(test).get("minimum_bid_pause") or {}
            if (
                test.status == ABTestStatus.RUNNING
                and test.operation_state == "awaiting_confirmation"
                and test.campaign_state == "paused"
                and pause_state
            ):
                expected_cpm = int(pause_state.get("minimum_cpm") or 0)
                if request.confirmed_cpm != expected_cpm:
                    raise HTTPException(status_code=409, detail={
                        "code": "minimum_cpm_confirmation_required",
                        "message": f"Подтвердите новый минимальный CPM {expected_cpm} ₽.",
                        "minimum_cpm": expected_cpm,
                    })
                if request.draft_fingerprint != self._draft_fingerprint(test):
                    raise HTTPException(status_code=409, detail={
                        "code": "confirmation_outdated",
                        "message": "Параметры теста изменились. Повторите подтверждение.",
                        "draft_fingerprint": self._draft_fingerprint(test),
                    })
                connection = await self._connection_or_404(user_id, test.connection_id)
                content, promotion = await self._clients(connection)
                campaign_id = int(test.wb_campaign_id or 0)
                if not campaign_id:
                    raise HTTPException(status_code=409, detail="У приостановленного теста отсутствует кампания WB; требуется сверка.")
                current_status = await promotion.get_campaign_status(campaign_id, refresh=True)
                if current_status not in self.RESUMABLE_CAMPAIGN_STATUSES:
                    raise HTTPException(status_code=409, detail={"code": "reconciliation_required", "message": "Кампания WB уже изменилась; сначала выполните сверку."})
                available = await promotion.get_budget_total(campaign_id)
                if available < int(test.budget_rub or 0):
                    raise HTTPException(status_code=400, detail={"code": "insufficient_campaign_budget", "message": "Существующего бюджета кампании недостаточно; дополнительное пополнение запрещено.", "available_budget": int(available), "required_budget": int(test.budget_rub or 0)})
                before = self._state_snapshot(test)
                test.cpm_rub = expected_cpm
                test.campaign_state = "starting"
                await promotion.set_bid(campaign_id=campaign_id, nm_id=test.nm_id, cpm_rub=expected_cpm, placement=test.placement, bid_type=test.bid_type)
                await promotion.start_campaign(campaign_id)
                if await self._confirm_campaign_active(promotion, campaign_id) != 9:
                    test.campaign_state = "paused"
                    await self.db.commit()
                    raise HTTPException(status_code=409, detail={"code": "reconciliation_required", "message": "Повторный запуск кампании WB не подтверждён."})
                pause_at = pause_state.get("paused_at")
                if pause_at and test.stage_started_at:
                    try:
                        paused_for = max((self._now() - datetime.fromisoformat(str(pause_at))).total_seconds(), 0)
                        test.stage_started_at = test.stage_started_at + timedelta(seconds=paused_for)
                    except (TypeError, ValueError):
                        pass
                state = self._media_state(test)
                state.pop("minimum_bid_pause", None)
                await self._set_media_state(test, state)
                test.campaign_state = "running"
                test.operation_state = ABTestOperationStatus.SUCCEEDED.value
                test.last_error = None
                await self._audit(test, "minimum_bid_resume_confirmed", before, details={"minimum_cpm": expected_cpm, "campaign_id": campaign_id})
                await self.db.commit()
                return await self.repository.get_for_user(user_id, test_id)  # type: ignore[return-value]
            if test.status != ABTestStatus.DRAFT:
                raise HTTPException(status_code=409, detail="Запустить можно только черновик A/B-теста")

            current_draft_fingerprint = self._draft_fingerprint(test)
            if request.draft_fingerprint != current_draft_fingerprint:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "confirmation_outdated",
                        "message": "Параметры теста изменились. Откройте черновик и подтвердите запуск заново.",
                        "draft_fingerprint": current_draft_fingerprint,
                    },
                )
            connection = await self._connection_or_404(user_id, test.connection_id)
            # Refresh the token permission/readiness state immediately before
            # paid or media mutations. A stale cached permission flag must not
            # be treated as proof that a revoked token can still write.
            try:
                await WBTokenService(self.db).validate_saved(user_id, test.connection_id)
                connection = await self.repository.get_connection(user_id, test.connection_id)
            except Exception as exc:
                raise self._api_error(exc) from exc
            if not connection or not connection.ready_for_ab_tests:
                raise HTTPException(status_code=403, detail="Токен WB больше не имеет подтверждённого доступа к «Контент» и «Продвижение».")
            store_fingerprint = WBTokenService.store_fingerprint(connection)
            other_open = await self.repository.get_open_for_store_card(store_fingerprint, test.nm_id, exclude_test_id=test.id)
            if other_open:
                raise HTTPException(status_code=409, detail="В этом магазине уже есть незакрытая серия для карточки.")
            test.store_fingerprint = store_fingerprint
            test.lifecycle_lock = True
            await self.db.flush()

            # Do'kon (WB token) darajasida bloklash: egasi va menejer bir xil
            # WB kabinetini ikki alohida foydalanuvchi sifatida ulasa ham,
            # bitta nmID uchun ikkinchi parallel test ishga tushmasligi kerak.
            # token_fingerprint — foydalanuvchidan qat'i nazar bir xil
            # tokenni aniqlaydi (qarang: wb_token_service.py).
            if connection.token_fingerprint:
                unresolved_test = await self.repository.get_unresolved_for_seller_card(
                    connection.token_fingerprint, test.nm_id, exclude_test_id=test.id
                )
                if unresolved_test:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Для этой карточки уже есть незавершённый A/B-тест "
                            "в этом магазине WB (возможно, запущен другим пользователем)."
                        ),
                    )
            elif not getattr(connection, "seller_id", None):
                token = WBTokenService.decrypt_token(connection.token_encrypted)
                connection.token_fingerprint = hashlib.sha256(token.strip().encode()).hexdigest()
                test.store_fingerprint = connection.token_fingerprint
                unresolved_test = await self.repository.get_open_for_store_card(
                    connection.token_fingerprint, test.nm_id, exclude_test_id=test.id
                )
                if unresolved_test:
                    raise HTTPException(status_code=409, detail={"code": "card_test_already_open", "message": "Для этой карточки уже есть незавершённый A/B-тест в этом WB-магазине.", "test_id": unresolved_test.id})

            content, promotion = await self._clients(connection)

            # Discover an external campaign for the same card before any new
            # campaign or deposit is created. Serving, paused and ready
            # campaigns are never silently adopted or overwritten.
            try:
                external_campaigns = await promotion.find_campaigns_for_nm(test.nm_id, refresh=True)
            except Exception as exc:
                raise self._api_error(exc) from exc
            known_campaign_id = int(test.wb_campaign_id or 0)
            unknown_campaigns = [
                item for item in external_campaigns
                if int(item.get("id") or 0) != known_campaign_id and int(item.get("status") or 0) in {4, 9, 11}
            ]
            if unknown_campaigns:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "external_campaign_for_card",
                        "message": "Для этой карточки уже найдена внешняя рекламная кампания. Проверьте её в кабинете WB перед запуском.",
                        "campaigns": [{"id": int(item["id"]), "status": int(item["status"])} for item in unknown_campaigns],
                    },
                )

            # Do the read-only card/access preflight before creating a campaign
            # or depositing money. A category ping alone is not enough to know
            # that the selected card is still available.
            try:
                preflight_card = await content.get_card(test.nm_id)
            except Exception as exc:
                raise self._api_error(exc) from exc
            if not preflight_card or not WBContentClient.photo_urls(preflight_card):
                raise HTTPException(
                    status_code=409,
                    detail="Карточка Wildberries недоступна или в ней нет фотографий. Запуск заблокирован.",
                )
            self._validate_card_eligibility(preflight_card)
            await self._assert_card_advertisable(preflight_card, promotion, test.nm_id)
            media_snapshot = WBContentClient.media_snapshot(preflight_card)
            current_card_photos = [str(url).strip() for url in media_snapshot.get("photos", []) if str(url).strip()]
            current_card_photo_keys = {self._canonical_media_url(url) for url in current_card_photos}
            # A card-selected variant must still point at a photo that is
            # actually present in the current WB card. This closes the gap
            # where a seller changes/reorders the card between wizard steps.
            for candidate in test.variants:
                if candidate.source_type == "card" and self._canonical_media_url(candidate.source_url or "") not in current_card_photo_keys:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Фото варианта {candidate.position} больше не соответствует текущему составу карточки WB. Откройте черновик и выберите фото заново.",
                    )
            if media_snapshot.get("videos"):
                # The current WB Content integration can mutate photo slots but
                # cannot atomically snapshot/restore seller video media. A/B
                # mutation is therefore blocked rather than risking silent
                # video loss on restore.
                raise HTTPException(
                    status_code=409,
                    detail="В карточке WB есть видео. A/B-тест заблокирован, пока интеграция не может гарантировать безопасное восстановление видео.",
                )
            if request.auto_deposit:
                try:
                    await promotion.get_balance()
                except Exception as exc:
                    raise self._api_error(exc) from exc

            variants = [variant for variant in test.variants if variant.source_type != "control"]
            tested_variant_count = self._validate_test_variant_count(
                len(variants), skip_current_photo=bool(test.skip_current_photo)
            )
            # Validate every candidate creative before campaign creation or
            # funding. Card URLs are downloaded and fully decoded here; local
            # uploads are re-read from durable storage so a missing/corrupt
            # file cannot surface after money has moved.
            for candidate in variants:
                try:
                    data, mime, filename = await self._variant_bytes(test, candidate, content)
                    self._prepare_image(filename, mime, data)
                except Exception as exc:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Вариант {candidate.position} не прошёл проверку изображения до запуска: {self._safe_error(exc)}",
                    ) from exc
            # The draft confirmation must cover the exact protected budget the
            # server is going to authorize. Never silently replace an API-
            # supplied lower amount after the confirmation fingerprint was
            # accepted; refresh the draft and require a new confirmation first.
            expected_protected_budget = calculate_protected_budget(
                tested_variant_count,
                test.views_per_variant,
                test.cpm_rub,
                getattr(settings, "ab_test_budget_guard_reserve_rub", DEFAULT_SAFETY_RESERVE_RUB),
            )
            if int(test.budget_rub or 0) != int(expected_protected_budget):
                test.budget_rub = int(expected_protected_budget)
                await self.db.commit()
                current_draft_fingerprint = self._draft_fingerprint(test)
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "confirmation_outdated",
                        "message": "Безопасный бюджет теста пересчитан. Подтвердите запуск с новой суммой.",
                        "draft_fingerprint": current_draft_fingerprint,
                        "recalculated_budget": int(expected_protected_budget),
                    },
                )
            positions = sorted(variant.position for variant in variants)
            if positions != list(range(1, len(positions) + 1)):
                raise HTTPException(status_code=400, detail="Позиции изображений должны идти последовательно, начиная с 1")
            max_budget = int(getattr(settings, "ab_test_max_budget_rub", 1_000_000_000) or 1_000_000_000)
            preflight_budget = calculate_protected_budget(
                tested_variant_count,
                test.views_per_variant,
                test.cpm_rub,
                getattr(settings, "ab_test_budget_guard_reserve_rub", DEFAULT_SAFETY_RESERVE_RUB),
            )
            if preflight_budget > max_budget or preflight_budget > 2_147_483_647:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Расчётный бюджет {preflight_budget} ₽ превышает безопасный лимит "
                        f"{max_budget} ₽. Уменьшите CPM или число показов."
                    ),
                )
            # Reject duplicate source images. Hash local uploads so two files
            # with different names/extensions cannot become duplicate stages.
            seen_variant_keys: set[str] = set()
            for candidate in variants:
                if candidate.file_path:
                    path = self._media_root() / candidate.file_path
                    if path.is_file():
                        key = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
                    else:
                        raise HTTPException(status_code=409, detail=f"Файл варианта {candidate.position} отсутствует на сервере")
                else:
                    key = "url:" + self._canonical_media_url(candidate.source_url or "")
                if not key or key.endswith(":"):
                    raise HTTPException(status_code=400, detail=f"Вариант {candidate.position} не содержит изображения")
                if key in seen_variant_keys:
                    raise HTTPException(status_code=409, detail="Один и тот же файл нельзя использовать в нескольких вариантах")
                seen_variant_keys.add(key)

            # Compare uploaded creatives with the current WB card before any
            # paid mutation. Exact/pHash duplicates must be rejected at the
            # draft boundary, not after an experiment has already spent money.
            upload_variants = [variant for variant in variants if variant.source_type == "upload" and variant.file_path]
            if upload_variants and current_card_photos:
                remote_images: list[bytes] = []
                for card_url in current_card_photos:
                    try:
                        remote_data, _ = await content.download_image(card_url, cache_bust=True)
                        remote_images.append(remote_data)
                    except Exception:
                        # A temporary CDN read error does not manufacture a
                        # duplicate; the media verification layer will still
                        # fail closed before any external photo mutation.
                        continue
                for candidate in upload_variants:
                    local_path = self._media_root() / str(candidate.file_path)
                    try:
                        local_data = local_path.read_bytes()
                    except OSError as exc:
                        raise HTTPException(status_code=409, detail=f"Файл варианта {candidate.position} отсутствует на сервере") from exc
                    local_sha = hashlib.sha256(local_data).hexdigest()
                    for remote_data in remote_images:
                        if hashlib.sha256(remote_data).hexdigest() == local_sha:
                            raise HTTPException(status_code=409, detail=f"Вариант {candidate.position} совпадает с уже существующим фото карточки WB")
                        try:
                            similar, distance = self._images_similar(local_data, remote_data)
                        except (ValueError, OSError):
                            similar, distance = False, None
                        if similar:
                            raise HTTPException(
                                status_code=409,
                                detail=f"Вариант {candidate.position} слишком похож на уже существующее фото карточки WB (pHash distance={distance})",
                            )

            # A previous attempt may have created a campaign and then stopped
            # before the first impression (for example because WB raised the
            # minimum CPM). Verify that exact campaign before allowing a
            # resume. Never create a second campaign for this situation.
            latest_operation = await self.repository.get_latest_operation(test.id)

            selected_funding_source = request.funding_source

            if request.auto_deposit and selected_funding_source == "auto":
                operations = await self.repository.list_operations(test.id)

                for previous_operation in operations:
                    saved_funding_source = (
                        previous_operation.request_snapshot or {}
                    ).get("funding_source")

                    if saved_funding_source in {"account", "mutual", "bonus"}:
                        selected_funding_source = saved_funding_source
                        break

            if request.auto_deposit and selected_funding_source == "auto":
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "funding_source_required",
                        "message": (
                            "Для автоматического пополнения выберите источник средств: "
                            "счёт Продвижения, баланс взаиморасчётов или промо-бонусы WB."
                        ),
                        "campaign_id": int(test.wb_campaign_id)
                        if test.wb_campaign_id
                        else None,
                        "reuse_existing_campaign": bool(test.wb_campaign_id),
                    },
                )
            resume_campaign_id: int | None = None
            if self._can_resume_existing_campaign(test, latest_operation):
                try:
                    existing_campaign_status = await promotion.get_campaign_status(test.wb_campaign_id, refresh=True)
                except Exception as exc:
                    test.campaign_state = "unknown"
                    test.operation_state = ABTestOperationStatus.RECONCILIATION_REQUIRED.value
                    test.incident_id = test.incident_id or self._incident_id()
                    test.last_error = (
                        f"Не удалось проверить существующую рекламную кампанию. Инцидент {test.incident_id}. "
                        f"{self._safe_error(exc)}"
                    )[:2000]
                    await self.db.commit()
                    raise self._api_error(exc) from exc
                if existing_campaign_status == 9:
                    test.campaign_state = "running"
                    test.operation_state = ABTestOperationStatus.RECONCILIATION_REQUIRED.value
                    test.incident_id = test.incident_id or self._incident_id()
                    message = (
                        "Существующая рекламная кампания уже активна. Новый запуск заблокирован, "
                        f"чтобы не создать дубликат. Инцидент {test.incident_id}."
                    )
                    test.last_error = message[:2000]
                    await self.db.commit()
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "existing_campaign_active",
                            "message": message,
                            "campaign_id": int(test.wb_campaign_id),
                            "incident_id": test.incident_id,
                        },
                    )
                if existing_campaign_status not in self.RESUMABLE_CAMPAIGN_STATUSES:
                    test.campaign_state = "unknown"
                    test.operation_state = ABTestOperationStatus.RECONCILIATION_REQUIRED.value
                    test.incident_id = test.incident_id or self._incident_id()
                    message = (
                        "Статус существующей рекламной кампании не подтверждён. "
                        f"Инцидент {test.incident_id}. Сначала выполните сверку."
                    )
                    test.last_error = message[:2000]
                    await self.db.commit()
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "reconciliation_required",
                            "message": message,
                            "campaign_id": int(test.wb_campaign_id),
                            "incident_id": test.incident_id,
                        },
                    )
                resume_campaign_id = int(test.wb_campaign_id)

            operation, is_new_operation = await self._prepare_start_operation(test, request, idempotency_key)
            if not is_new_operation:
                existing_test = await self.repository.get_for_user(user_id, test_id)
                if existing_test:
                    return existing_test
                raise HTTPException(status_code=404, detail="A/B-тест не найден")

            campaign_id: int | None = resume_campaign_id
            campaign_creation_attempted = False
            media_changed = False
            original_media: list[str] = []
            logger.info(
                "A/B start requested test_id=%s connection_id=%s nm_id=%s variants=%s skip_current=%s",
                test.id,
                test.connection_id,
                test.nm_id,
                len(variants),
                test.skip_current_photo,
            )
            try:
                await self._operation_state(test, operation, ABTestOperationStatus.IN_PROGRESS)
                operation.response_snapshot = {"phase": "preparing_card"}
                await self.db.commit()
                card = preflight_card
                original_media = WBContentClient.photo_urls(card or {})
                if not original_media:
                    raise RuntimeError("Карточка товара не найдена или в ней нет исходного изображения")
                if resume_campaign_id and (test.media_state or {}).get("backups"):
                    # A failed start may already have applied its first photo.
                    # Never replace original backups with that test image.
                    await self._assert_media_snapshot_safe_for_restore(test, content)
                    await self._restore_original(test, content)
                    original_media = list(test.original_media or [])
                else:
                    await self._initialize_media_state(test, original_media, variants, content)
                logger.info(
                    "A/B original media backed up test_id=%s nm_id=%s slots=%s parking_slot=%s",
                    test.id,
                    test.nm_id,
                    len(original_media),
                    (test.media_state or {}).get("parking_slot"),
                )

                if resume_campaign_id:
                    # Reuse the already-created WB campaign. The campaign was
                    # verified as inactive above, so changing its bid and
                    # starting it does not create a duplicate or reset its
                    # identity.
                    operation.external_id = resume_campaign_id
                    operation.response_snapshot = {"phase": "campaign_reused", "campaign_id": resume_campaign_id}
                    test.wb_campaign_id = resume_campaign_id
                    test.campaign_state = "created"
                    await self._operation_state(test, operation, ABTestOperationStatus.IN_PROGRESS)
                    await self.db.commit()
                    logger.info("A/B campaign reused test_id=%s nm_id=%s campaign_id=%s", test.id, test.nm_id, resume_campaign_id)
                else:
                    operation.response_snapshot = {"phase": "campaign_create_sent"}
                    await self.db.commit()
                    campaign_creation_attempted = True
                    campaign_id = await promotion.create_campaign(
                        name=test.title,
                        nm_id=test.nm_id,
                        placement=test.placement,
                        bid_type=test.bid_type,
                    )
                    test.wb_campaign_id = campaign_id
                    operation.external_id = campaign_id
                    operation.response_snapshot = {"phase": "campaign_created", "campaign_id": campaign_id}
                    test.campaign_state = "created"
                    await self._operation_state(test, operation, ABTestOperationStatus.IN_PROGRESS)
                    await self.db.commit()
                    logger.info("A/B campaign created test_id=%s nm_id=%s campaign_id=%s", test.id, test.nm_id, campaign_id)
                await self.db.flush()
                await self._confirm_campaign_product(promotion, campaign_id, test.nm_id)
                await self._assert_campaign_configuration(test, promotion)
                minimum_bid = await promotion.get_min_bid(
                    campaign_id=campaign_id,
                    nm_id=test.nm_id,
                    placement=test.placement,
                    bid_type=test.bid_type,
                )
                if minimum_bid is None or minimum_bid < 1:
                    raise RuntimeError("Wildberries не вернул актуальную минимальную ставку. Запуск заблокирован.")
                tested_variant_count = len(variants) + (0 if test.skip_current_photo else 1)
                if test.cpm_rub < minimum_bid:
                    previous_cpm = int(test.cpm_rub)
                    test.cpm_rub = int(minimum_bid)
                    test.budget_rub = calculate_protected_budget(
                        tested_variant_count,
                        test.views_per_variant,
                        minimum_bid,
                        getattr(settings, "ab_test_budget_guard_reserve_rub", DEFAULT_SAFETY_RESERVE_RUB),
                    )
                    await self.db.flush()
                    # A previous resume click is not a blanket permission to
                    # spend a newly recalculated amount. Every bid increase
                    # invalidates the old confirmation, including on the same
                    # existing campaign.
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "minimum_cpm_increased",
                            "message": (
                                f"Минимальная ставка Wildberries сейчас {minimum_bid} ₽. "
                                "CPM и необходимый бюджет пересчитаны. Подтвердите повторный запуск."
                            ),
                            "minimum_cpm": int(minimum_bid),
                            "previous_cpm": previous_cpm,
                            "recalculated_budget": int(test.budget_rub),
                            "tested_variant_count": int(tested_variant_count),
                            "campaign_id": int(campaign_id),
                            "reuse_existing_campaign": bool(resume_campaign_id),
                        },
                    )
                try:
                    await promotion.set_bid(
                        campaign_id=campaign_id,
                        nm_id=test.nm_id,
                        cpm_rub=test.cpm_rub,
                        placement=test.placement,
                        bid_type=test.bid_type,
                    )
                except WBApiError as bid_error:
                    # WB can raise the minimum between the preflight read
                    # and the bid mutation. Re-read once and keep the same
                    # campaign. A first start asks for a new confirmation;
                    # an already-confirmed resume retries the same bid once.
                    refreshed_minimum = await promotion.get_min_bid(
                        campaign_id=campaign_id,
                        nm_id=test.nm_id,
                        placement=test.placement,
                        bid_type=test.bid_type,
                    )
                    if refreshed_minimum and refreshed_minimum > int(test.cpm_rub):
                        previous_cpm = int(test.cpm_rub)
                        test.cpm_rub = int(refreshed_minimum)
                        test.budget_rub = calculate_protected_budget(
                            tested_variant_count,
                            test.views_per_variant,
                            refreshed_minimum,
                            getattr(settings, "ab_test_budget_guard_reserve_rub", DEFAULT_SAFETY_RESERVE_RUB),
                        )
                        raise HTTPException(
                            status_code=409,
                            detail={
                                "code": "minimum_cpm_increased",
                                "message": (
                                    f"Минимальная ставка Wildberries сейчас {refreshed_minimum} ₽. "
                                    "CPM и необходимый бюджет пересчитаны. Подтвердите повторный запуск."
                                ),
                                "minimum_cpm": int(refreshed_minimum),
                                "previous_cpm": previous_cpm,
                                "recalculated_budget": int(test.budget_rub),
                                "tested_variant_count": int(tested_variant_count),
                                "campaign_id": int(campaign_id),
                                "reuse_existing_campaign": bool(resume_campaign_id),
                            },
                        ) from bid_error
                    raise

                estimated_budget = calculate_protected_budget(
                    tested_variant_count,
                    test.views_per_variant,
                    test.cpm_rub,
                    getattr(settings, "ab_test_budget_guard_reserve_rub", DEFAULT_SAFETY_RESERVE_RUB),
                )
                # The required campaign balance is deterministic: every test
                # stage receives the requested impressions at the effective
                # CPM. The frontend shows the same formula; the backend is the
                # final source of truth after WB minimum-bid correction.
                if estimated_budget > max_budget or estimated_budget > 2_147_483_647:
                    raise HTTPException(status_code=422, detail=f"Рассчитанный бюджет {estimated_budget} ₽ превышает безопасный серверный лимит {max_budget} ₽.")
                test.budget_rub = estimated_budget
                requested_deposit = int(request.deposit_rub or 0)
                if requested_deposit > max_budget:
                    raise HTTPException(status_code=422, detail="Сумма автоматического пополнения превышает безопасный серверный лимит.")
                required_budget = max(estimated_budget, requested_deposit)
                funding_source_label = {
                    "account": "счёта Продвижения",
                    "mutual": "баланса взаиморасчётов",
                    "bonus": "промо-бонусов WB",
                }.get(selected_funding_source, "выбранного источника")
                current_budget = await promotion.get_budget_total(campaign_id)
                if current_budget < required_budget:
                    shortfall = max(1, int(math.ceil(required_budget - current_budget)))
                    previous_phase = (latest_operation.response_snapshot or {}).get("phase") if latest_operation else None
                    deposit_already_attempted = previous_phase in {
                        "budget_deposit_sent",
                        "budget_deposited",
                        "budget_deposit_pending",
                    }
                    if not request.auto_deposit:
                        raise HTTPException(
                            status_code=400,
                            detail={
                                "code": "insufficient_campaign_budget",
                                "message": (
                                    f"Для запуска нужен бюджет кампании не менее {required_budget} ₽, "
                                    f"доступно {int(current_budget)} ₽. Кампания WB #{int(campaign_id)}. "
                                    "Пополните её в кабинете WB и повторите запуск."
                                ),
                                "required_budget": int(required_budget),
                                "available_budget": int(current_budget),
                                "shortfall": shortfall,
                                "campaign_id": int(campaign_id),
                                "reuse_existing_campaign": bool(resume_campaign_id),
                            },
                        )
                    if deposit_already_attempted:
                        operation.response_snapshot = {
                            "phase": "budget_deposit_pending",
                            "campaign_id": int(campaign_id),
                            "amount_rub": shortfall,
                            "required_budget": int(required_budget),
                            "available_budget": int(current_budget),
                            "funding_source": selected_funding_source,
                        }
                        await self.db.flush()
                        raise HTTPException(
                            status_code=409,
                            detail={
                                "code": "budget_deposit_pending",
                                "message": (
                                    f"Пополнение кампании WB #{int(campaign_id)} уже отправлено, но новый бюджет ещё не виден. "
                                    "Подождите немного и подтвердите запуск повторно. Повторное списание заблокировано."
                                ),
                                "required_budget": int(required_budget),
                                "available_budget": int(current_budget),
                                "campaign_id": int(campaign_id),
                            },
                        )
                    operation.response_snapshot = {
                        "phase": "budget_deposit_sent",
                        "campaign_id": int(campaign_id),
                        "amount_rub": shortfall,
                        "required_budget": int(required_budget),
                        "available_budget": int(current_budget),
                        "funding_source": selected_funding_source,
                    }
                    await self.repository.add_budget_entry(
                        test_id=test.id,
                        operation_id=operation.id,
                        kind="deposit_intent",
                        amount_rub=shortfall,
                        source=selected_funding_source,
                        metadata_json={"required_budget": int(required_budget), "available_budget": int(current_budget)},
                    )
                    await self.db.commit()
                    logger.info(
                        "A/B campaign budget deposit requested test_id=%s campaign_id=%s amount_rub=%s",
                        test.id,
                        campaign_id,
                        shortfall,
                    )
                    try:
                        funding_source_type = {
                            "account": 0,
                            "mutual": 1,
                            "bonus": 3,
                        }.get(selected_funding_source)
                        await promotion.deposit_budget(
                            campaign_id=campaign_id,
                            amount_rub=shortfall,
                            source_type=funding_source_type,
                        )
                    except WBApiError as deposit_error:
                        # The campaign already exists and must remain reusable.
                        # A definitive validation/permission 4xx means WB
                        # rejected the money operation. A timeout, 429, or
                        # 5xx does not prove that the request was not accepted;
                        # classify it as pending so a retry cannot charge the
                        # same source twice.
                        definitive_rejection = deposit_error.status_code in {400, 401, 403, 404, 422}
                        deposit_phase = "budget_deposit_rejected" if definitive_rejection else "budget_deposit_pending"
                        operation.response_snapshot = {
                            "phase": deposit_phase,
                            "campaign_id": int(campaign_id),
                            "amount_rub": shortfall,
                            "required_budget": int(required_budget),
                            "available_budget": int(current_budget),
                            "provider_status": deposit_error.status_code,
                            "funding_source": selected_funding_source,
                        }
                        await self.db.commit()
                        provider_message = str(deposit_error).removeprefix("WB Продвижение:").strip()
                        if provider_message.lower() in {"bad request", "неправильный запрос"}:
                            provider_message = "WB не сообщил дополнительную причину"
                        if definitive_rejection:
                            message = (
                                f"Wildberries не принял автоматическое пополнение кампании WB #{int(campaign_id)} "
                                f"на {shortfall} ₽. {provider_message}. "
                                f"Проверьте доступность {funding_source_label} в кабинете WB и повторите запуск. "
                                "Будет продолжена эта же кампания, новая создана не будет."
                            )
                            code = "campaign_budget_deposit_failed"
                        else:
                            message = (
                                f"Результат пополнения кампании WB #{int(campaign_id)} на {shortfall} ₽ не подтверждён. "
                                "Повторное списание заблокировано. Проверьте бюджет кампании в кабинете WB и повторите сверку."
                            )
                            code = "campaign_budget_deposit_pending"
                        raise HTTPException(
                            status_code=409,
                            detail={
                                "code": code,
                                "message": message,
                                "campaign_id": int(campaign_id),
                                "required_budget": int(required_budget),
                                "available_budget": int(current_budget),
                                "shortfall": shortfall,
                                "reuse_existing_campaign": True,
                            },
                        ) from deposit_error
                    operation.response_snapshot = {
                        "phase": "budget_deposited",
                        "campaign_id": int(campaign_id),
                        "amount_rub": shortfall,
                        "required_budget": int(required_budget),
                        "funding_source": selected_funding_source,
                    }
                    await self.repository.add_budget_entry(
                        test_id=test.id,
                        operation_id=operation.id,
                        kind="deposit_confirmed",
                        amount_rub=shortfall,
                        provider_balance_rub=current_budget,
                        source=selected_funding_source,
                        metadata_json={"required_budget": int(required_budget)},
                    )
                    await self.db.commit()
                    current_budget = await self._confirm_campaign_budget(
                        promotion,
                        campaign_id,
                        required_budget,
                    )
                    if current_budget < required_budget:
                        operation.response_snapshot = {
                            "phase": "budget_deposit_pending",
                            "campaign_id": int(campaign_id),
                            "amount_rub": shortfall,
                            "required_budget": int(required_budget),
                            "available_budget": int(current_budget),
                            "funding_source": selected_funding_source,
                        }
                        await self.db.commit()
                        raise HTTPException(
                            status_code=409,
                            detail={
                                "code": "budget_deposit_pending",
                                "message": (
                                    f"Кампанию WB #{int(campaign_id)} пополнили на {shortfall} ₽, "
                                    "но Wildberries ещё не подтвердил новый бюджет. Подождите немного и повторите запуск."
                                ),
                                "required_budget": int(required_budget),
                                "available_budget": int(current_budget),
                                "campaign_id": int(campaign_id),
                            },
                        )

                if test.skip_current_photo:
                    first = self._variant_by_position(test, 1)
                    if not first:
                        raise RuntimeError("Не найден первый вариант изображения")
                    # Mark before the external write. If WB accepts the upload
                    # but the read-model poll fails, cleanup must restore the
                    # original card as well.
                    media_changed = True
                    await self._apply_variant(
                        test,
                        first,
                        content,
                        promotion,
                    )

                    if test.media_status == "waiting_image_reupload":
                        test.status = ABTestStatus.RUNNING
                        test.started_at = test.started_at or self._now()
                        test.stage_started_at = test.stage_started_at or self._now()
                        test.finished_at = None
                        test.campaign_state = "paused"
                        test.operation_state = (
                            ABTestOperationStatus.IN_PROGRESS.value
                        )

                        operation.response_snapshot = {
                            "phase": "image_reupload_waiting",
                            "campaign_id": int(campaign_id),
                            "variant_position": 1,
                        }

                        await self._operation_state(
                            test,
                            operation,
                            ABTestOperationStatus.IN_PROGRESS,
                        )

                        await self.db.commit()

                        return await self.repository.get_for_user(
                            user_id,
                            test_id,
                        )

                    test.current_variant_order = 1
                    test.stage_started_at = self._now()
                else:
                    control = self._variant_by_position(test, 0)
                    if not control:
                        control = await self.repository.add_variant(
                            test_id=test.id, position=0, source_type="control", source_url=original_media[0], file_name="Текущее фото"
                        )
                    test.current_variant_order = 0
                    test.stage_started_at = self._now()
                test.campaign_state = "starting"
                operation.response_snapshot = {"phase": "campaign_start_sent", "campaign_id": campaign_id}
                await self.db.commit()
                await promotion.start_campaign(campaign_id)
                campaign_status = await self._confirm_campaign_active(promotion, campaign_id)
                if campaign_status != 9:
                    raise ABTestReconciliationRequired(
                        "Запрос запуска отправлен, но Wildberries пока не подтвердил активный статус кампании.",
                        incident_id=test.incident_id,
                    )
                test.status = ABTestStatus.RUNNING
                test.started_at = self._now()
                test.stage_started_at = test.started_at
                test.finished_at = None
                test.last_synced_at = None
                test.last_total_views = 0
                test.last_total_clicks = 0
                test.last_total_orders = 0
                test.last_total_spend_rub = 0
                test.settled_total_views = 0
                test.settled_total_clicks = 0
                test.settled_total_orders = 0
                test.settled_total_spend_rub = 0
                test.winner_variant_order = None
                test.winner_decision = None
                test.last_error = None
                test.incident_id = None
                test.operation_state = ABTestOperationStatus.SUCCEEDED.value
                test.campaign_state = "running"
                test.media_status = "variant_applied" if test.skip_current_photo else "original"
                test.stats_quality = "preliminary"
                test.stage_views = 0
                test.stage_clicks = 0
                test.stage_spend_rub = 0
                test.funding_source = selected_funding_source
                operation.response_snapshot = {"phase": "campaign_running_confirmed", "campaign_id": campaign_id, "funding_source": selected_funding_source}
                await self._operation_state(test, operation, ABTestOperationStatus.SUCCEEDED)
                await self._audit(test, "campaign_started", {"status": "draft", "campaign_state": "starting"}, details={"campaign_id": int(campaign_id), "funding_source": selected_funding_source})
                for variant in test.variants:
                    variant.views = 0
                    variant.clicks = 0
                    variant.orders = 0
                    variant.spend_rub = 0
                    variant.is_winner = False
                await self.db.commit()
            except Exception as exc:
                if isinstance(exc, HTTPException):
                    detail_code = exc.detail.get("code") if isinstance(exc.detail, dict) else "http_error"
                    logger.warning(
                        "A/B start blocked test_id=%s nm_id=%s status=%s code=%s",
                        test.id,
                        test.nm_id,
                        exc.status_code,
                        detail_code,
                    )
                else:
                    logger.exception("A/B start failed test_id=%s nm_id=%s", test.id, test.nm_id)

                # Sergey hisobotidagi #4: agar WB start_campaign so'rovini
                # rad etgan bo'lsa (kampaniya allaqachon yaratilgan va
                # pullangan, ammo hali ishga tushmagan/tasdiqlanmagan holatda),
                # uni majburan "stopped" qilib qo'ymaymiz. Aks holda keyingi
                # tasdiqlangan urinish _can_resume_existing_campaign orqali
                # shu kampaniyani topolmaydi va yangi (ikkinchi pullangan)
                # kampaniya yaratadi. Bu yerda kampaniyani "created" holatda
                # saqlab, keyingi start() chaqiruvi uni davom ettirishiga
                # imkon beramiz.
                start_phase = (operation.response_snapshot or {}).get("phase") if operation else None
                if (
                    campaign_id
                    and start_phase == "campaign_start_sent"
                    and not isinstance(exc, ABCampaignProductMismatch)
                ):
                    test.status = ABTestStatus.FAILED
                    test.campaign_state = "starting"
                    test.operation_state = ABTestOperationStatus.RECONCILIATION_REQUIRED.value
                    safe_error = self._safe_error(exc)
                    test.last_error = (
                        f"Запуск рекламной кампании не подтверждён: {safe_error}. "
                        f"Кампания {campaign_id} сохранена и пополнена — "
                        "сначала выполните сверку. Будет использована та же кампания, а новая кампания создаваться не будет."
                    )[:2000]
                    await self._operation_state(test, operation, ABTestOperationStatus.RECONCILIATION_REQUIRED, error=exc)
                    await self.db.commit()
                    if isinstance(exc, HTTPException):
                        raise
                    raise self._api_error(exc) from exc

                cleanup_errors: list[str] = []
                campaign_stopped = not campaign_id and not campaign_creation_attempted
                if campaign_id:
                    try:
                        await self._stop_campaign_confirmed(test, promotion)
                        campaign_stopped = True
                    except Exception as cleanup_exc:
                        cleanup_errors.append(f"остановка кампании не подтверждена: {self._safe_error(cleanup_exc)}")
                elif campaign_creation_attempted:
                    test.campaign_state = "unknown"
                    test.operation_state = ABTestOperationStatus.RECONCILIATION_REQUIRED.value
                    test.incident_id = test.incident_id or self._incident_id()
                    cleanup_errors.append("ID кампании не получен; результат создания кампании нужно сверить в кабинете WB")

                # A confirmed product mismatch must never be offered for a
                # retry: reusing it would keep advertising the wrong card.
                # Keep the external campaign ID for manual handling, but
                # quarantine it from the normal same-campaign resume path.
                if isinstance(exc, ABCampaignProductMismatch):
                    test.campaign_state = "quarantined"
                    operation.response_snapshot = {
                        "phase": "campaign_mismatch",
                        "campaign_id": int(campaign_id) if campaign_id else None,
                        "nm_id": int(test.nm_id),
                    }

                state = self._media_state(test)
                media_needs_restore = media_changed or bool(state.get("touched") or state.get("pending"))
                media_restored = not media_needs_restore
                if media_needs_restore and original_media:
                    try:
                        await self._restore_original(test, content)
                        media_restored = True
                    except Exception as cleanup_exc:
                        cleanup_errors.append(f"восстановление фото не подтверждено: {self._safe_error(cleanup_exc)}")

                unresolved = (
                    bool(cleanup_errors)
                    or isinstance(exc, ABTestReconciliationRequired)
                    or (campaign_creation_attempted and not campaign_id)
                    or not campaign_stopped
                    or not media_restored
                )
                safe_error = self._safe_error(exc)
                if unresolved:
                    test.status = ABTestStatus.FAILED
                    test.operation_state = ABTestOperationStatus.RECONCILIATION_REQUIRED.value
                    test.incident_id = test.incident_id or self._incident_id()
                    message = f"Запуск не подтверждён: {safe_error}. Инцидент {test.incident_id}."
                    if cleanup_errors:
                        message += " " + "; ".join(cleanup_errors)
                    test.last_error = message[:2000]
                    await self._operation_state(test, operation, ABTestOperationStatus.RECONCILIATION_REQUIRED, error=exc)
                else:
                    # No external effect remains: the draft can be corrected
                    # and started again with a fresh explicit confirmation.
                    test.status = ABTestStatus.DRAFT
                    test.operation_state = ABTestOperationStatus.FAILED.value
                    test.campaign_state = "stopped" if campaign_id else "not_created"
                    test.lifecycle_lock = False
                    test.last_error = f"Запуск не выполнен: {safe_error}. Повторите с новым подтверждением."[:2000]
                    await self._operation_state(test, operation, ABTestOperationStatus.FAILED, error=exc)
                await self.db.commit()
                if isinstance(exc, HTTPException):
                    if unresolved:
                        reconciliation = ABTestReconciliationRequired(
                            test.last_error or "Результат внешней операции не подтверждён",
                            incident_id=test.incident_id,
                        )
                        raise self._api_error(reconciliation) from exc
                    raise
                raise self._api_error(exc) from exc
        return await self.repository.get_for_user(user_id, test_id)  # type: ignore[return-value]

    @classmethod
    def _winner_result(cls, test: ABTest) -> tuple[ABTestVariant | None, str]:
        """Select a winner from attributable, sufficiently large CTR samples.

        The product decision is deliberately CTR-only. Clicks and impressions
        are used to calculate CTR and to enforce the minimum sample, but they do
        not add a second hidden score. Campaign-level ``fullstats`` cannot prove
        which photo generated a click, so aggregate or incomplete data must
        never produce a winner.
        """
        stats_quality = getattr(test, "stats_quality", None)
        if stats_quality != "stage_attributed":
            return None, "statistics_not_attributable"
        candidates = [
            variant
            for variant in test.variants
            if variant.source_type != "control"
            or (not bool(test.skip_current_photo) and variant.position == 0)
        ]
        if len(candidates) < cls.WINNER_MIN_VARIANTS:
            return None, "insufficient_data"
        if min((variant.views for variant in candidates), default=0) < cls.WINNER_MIN_IMPRESSIONS:
            return None, "insufficient_data"
        if any(variant.clicks < 0 or variant.clicks > variant.views for variant in candidates):
            return None, "statistics_not_attributable"

        ranked = sorted(candidates, key=lambda variant: (variant.ctr, -variant.position), reverse=True)
        winner, runner_up = ranked[0], ranked[1]
        if winner.ctr - runner_up.ctr < cls.WINNER_MIN_CTR_DELTA:
            return None, "no_clear_winner"
        return winner, "winner_found"

    @classmethod
    def _select_winner(cls, test: ABTest) -> ABTestVariant | None:
        return cls._winner_result(test)[0]

    async def _finish_loaded(self, test: ABTest, content: WBContentClient, promotion: WBPromotionClient, *, stopped: bool = False) -> None:
        # Replaying a persisted completion must not require already-cleaned
        # rollback files or produce another external mutation.
        if (test.status in {ABTestStatus.STOPPED, ABTestStatus.FINISHED}
                and not test.lifecycle_lock and test.campaign_state == "stopped"
                and test.media_status in {"restored", "winner_applied"}):
            return
        stop_error: Exception | None = None
        stats_error: Exception | None = None
        if test.wb_campaign_id:
            try:
                await self._stop_campaign_confirmed(test, promotion)
            except Exception as exc:
                # Restore is a separate obligation. It is still attempted, but
                # the experiment cannot be reported as finished until both
                # campaign stop and media restoration are confirmed.
                stop_error = exc
        if not stopped and test.wb_campaign_id:
            try:
                await self._settle_stage_stats(test, promotion)
            except Exception as settle_exc:
                # Statistics cannot prevent either safety obligation.
                stats_error = settle_exc
                test.stats_quality = "reconciliation_required"
        winner, decision = ((None, "statistics_not_attributable") if stats_error else
                            (None, "test_interrupted") if stopped else self._winner_result(test))
        for variant in test.variants:
            variant.is_winner = False
        if winner:
            winner.is_winner = True
            test.winner_variant_order = winner.position
        else:
            test.winner_variant_order = None
        test.winner_decision = decision
        restore_error: Exception | None = None
        try:
            await self._assert_media_snapshot_safe_for_restore(test, content)
            await self._restore_original(test, content)
        except Exception as exc:
            logger.exception("A/B finish failed test_id=%s nm_id=%s", test.id, test.nm_id)
            restore_error = exc
        if stop_error:
            test.status = ABTestStatus.FAILED
            test.finished_at = self._now()
            test.operation_state = ABTestOperationStatus.RECONCILIATION_REQUIRED.value
            test.incident_id = test.incident_id or self._incident_id()
            details = f"Тест не завершён: остановка кампании не подтверждена. Инцидент {test.incident_id}."
            if restore_error:
                details += f" Восстановление фото также не подтверждено: {self._safe_error(restore_error)}."
            test.last_error = details[:2000]
            raise ABTestReconciliationRequired(details, incident_id=test.incident_id) from stop_error
        if restore_error:
            test.status = ABTestStatus.FAILED
            test.finished_at = self._now()
            test.operation_state = ABTestOperationStatus.RECONCILIATION_REQUIRED.value
            test.incident_id = test.incident_id or self._incident_id()
            details = f"Кампания остановлена, но восстановление фото не подтверждено. Инцидент {test.incident_id}."
            details += f" {self._safe_error(restore_error)}"
            test.last_error = details[:2000]
            raise ABTestReconciliationRequired(details, incident_id=test.incident_id) from restore_error

        winner_media_error: Exception | None = None
        winner_applied = False
        if winner and test.keep_winner_as_main:
            try:
                await self._apply_variant(
                    test,
                    winner,
                    content,
                    promotion,
                )

                if test.media_status == "waiting_image_reupload":
                    raise ABTestReconciliationRequired(
                        "Победившее изображение не подтверждено Wildberries. Финальная установка победителя не завершена.",
                        incident_id=test.incident_id or self._incident_id(),
                    )
                winner_applied = True

            except Exception as exc:
                winner_media_error = exc

                try:
                    await self._restore_original(test, content)
                except Exception as restore_after_winner_error:
                    winner_media_error = RuntimeError(
                        f"{self._safe_error(exc)}; "
                        "восстановление после установки победителя: "
                        f"{self._safe_error(restore_after_winner_error)}"
                    )
        if winner_media_error:
            test.status = ABTestStatus.FAILED
            test.finished_at = self._now()
            test.operation_state = ABTestOperationStatus.RECONCILIATION_REQUIRED.value
            test.incident_id = test.incident_id or self._incident_id()
            details = (
                f"Кампания остановлена, но победившее фото не удалось установить. Инцидент {test.incident_id}. "
                f"{self._safe_error(winner_media_error)}"
            )
            test.last_error = details[:2000]
            raise ABTestReconciliationRequired(details, incident_id=test.incident_id) from winner_media_error
        before = self._state_snapshot(test)
        test.status = ABTestStatus.STOPPED if stopped else ABTestStatus.FINISHED
        test.finished_at = self._now()
        test.operation_state = ABTestOperationStatus.SUCCEEDED.value
        test.media_status = "winner_applied" if winner_applied else "restored"
        test.lifecycle_lock = False
        test.last_error = (f"Кампания остановлена, фотографии восстановлены. Финальная статистика не подтверждена: {self._safe_error(stats_error)}"[:2000]
                           if stats_error else None)
        await self._audit(test, "test_finished", before, details={"decision": test.winner_decision or ""})
        # Save confirmed restoration before deleting any files. A failure of
        # this commit must leave the backups available for crash recovery.
        await self.db.commit()
        if test.delete_test_media:
            self._cleanup_media_artifacts(test)
            await self.db.commit()

    def _cleanup_media_artifacts(self, test: ABTest) -> None:
        """Remove rollback/shadow files after a safe external finish.

        Uploaded creatives remain available as result evidence in the UI. The
        files removed here are the original-card backups and swap shadows,
        which are no longer needed after campaign stop, restoration, and the
        optional winner application have all succeeded. Failed or
        reconciliation-required tests never reach this method, so rollback
        data remains available for recovery.
        """
        state = self._media_state(test)
        paths = set(self._media_state_paths(state))
        if test.original_main_backup_path:
            paths.add(test.original_main_backup_path)
        cleanup_errors: list[str] = []
        for relative_path in paths:
            try:
                self._state_file(str(relative_path)).unlink(missing_ok=True)
            except (OSError, RuntimeError) as exc:
                cleanup_errors.append(str(relative_path))
                logger.warning(
                    "A/B local media cleanup failed test_id=%s path=%s error=%s",
                    test.id,
                    relative_path,
                    exc,
                )
        state["cleanup"] = "completed" if not cleanup_errors else "partial"
        state["cleanup_errors"] = cleanup_errors
        state["backups"] = {}
        state["shadows"] = {}
        state["pending"] = None
        state["touched"] = []
        test.original_main_backup_path = None
        test.media_state = state

    async def stop(self, user_id: int, test_id: int) -> ABTest:
        test = await self.repository.get_for_user(user_id, test_id)
        if not test:
            raise HTTPException(status_code=404, detail="A/B-тест не найден")
        async with self._operation_lock(test.connection_id, test.nm_id, user_id):
            test = await self.repository.get_for_update(user_id, test_id)
            if not test:
                raise HTTPException(status_code=404, detail="A/B-тест не найден")
            active_campaign_states = {"running", "starting", "unknown", "pause_requested", "stop_requested", "created"}
            if test.status not in {ABTestStatus.RUNNING, ABTestStatus.DRAFT, ABTestStatus.FAILED, ABTestStatus.STOPPED}:
                raise HTTPException(status_code=409, detail="A/B-тест уже завершён")
            if test.status == ABTestStatus.DRAFT and test.campaign_state not in active_campaign_states:
                test.status = ABTestStatus.STOPPED
                test.winner_decision = "test_interrupted"
                test.finished_at = self._now()
                test.lifecycle_lock = False
                await self.db.commit()
                return await self.repository.get_for_user(user_id, test.id)  # type: ignore[return-value]
            # A token can lose access while a test is running. Stopping must
            # remain available so the service can attempt the safety rollback.
            connection = await self._connection_or_404(user_id, test.connection_id, require_ab_access=False)
            content, promotion = await self._clients(connection)
            try:
                await self._finish_loaded(test, content, promotion, stopped=True)
                await self.db.commit()
            except Exception as exc:
                if not isinstance(exc, ABTestReconciliationRequired):
                    test.status = ABTestStatus.FAILED
                    test.operation_state = ABTestOperationStatus.RECONCILIATION_REQUIRED.value
                    test.incident_id = test.incident_id or self._incident_id()
                test.last_error = test.last_error or self._safe_error(exc)
                await self.db.commit()
                raise self._api_error(exc) from exc
        return await self.repository.get_for_user(user_id, test_id)  # type: ignore[return-value]

    async def _recover_pending_variant_media(
        self,
        test: ABTest,
        content: WBContentClient,
        promotion: WBPromotionClient,
    ) -> bool:
        """Replay an interrupted variant write from local shadow files.

        A worker can stop after WB accepted one of several slot writes.
        The journal stores the exact local payload for every slot.

        Recovery is safety-sensitive:
        1. If campaign is active, pause it and confirm status 11.
        2. Upload every pending slot.
        3. Verify every uploaded slot through CDN/pHash.
        4. If verification fails, wait one hour for reupload.
        5. Only after all slots are verified is the media state committed.
        """
        state = self._media_state(test)
        pending = state.get("pending")

        if (
            not isinstance(pending, dict)
            or pending.get("kind")
            not in {"swap", "replace", "replace_with_parking"}
        ):
            return False

        raw_targets = pending.get("targets") or {}

        if not isinstance(raw_targets, dict) or not raw_targets:
            raise ABTestReconciliationRequired(
                "Повреждён журнал ожидающей операции со сменой изображения.",
                incident_id=test.incident_id or self._incident_id(),
            )

        targets = {
            int(raw_slot): dict(target)
            for raw_slot, target in raw_targets.items()
            if isinstance(target, dict)
        }

        if not targets:
            raise ABTestReconciliationRequired(
                "Журнал смены изображения не содержит ни одного слота для восстановления.",
                incident_id=test.incident_id or self._incident_id(),
            )
        pending_slots = {
            int(slot)
            for slot in (pending.get("slots") or [])
        }

        target_slots = set(targets)

        if not pending_slots or pending_slots != target_slots:
            raise ABTestReconciliationRequired(
                "Журнал смены изображения повреждён: "
                f"slots={sorted(pending_slots)}, "
                f"targets={sorted(target_slots)}.",
                incident_id=test.incident_id or self._incident_id(),
            )

        payloads: dict[int, tuple[bytes, str, str]] = {}

        for slot, target in targets.items():
            shadow_path = target.get("shadow_path")

            if not shadow_path:
                raise ABTestReconciliationRequired(
                    f"Не найден shadow-файл для слота {slot}.",
                    incident_id=test.incident_id or self._incident_id(),
                )

            path = self._state_file(str(shadow_path))

            if not path.is_file():
                raise ABTestReconciliationRequired(
                    f"Shadow-файл для слота {slot} отсутствует: {shadow_path}",
                    incident_id=test.incident_id or self._incident_id(),
                )

            payloads[slot] = (
                path.read_bytes(),
                str(
                    target.get("mime")
                    or mimetypes.guess_type(path.name)[0]
                    or "image/jpeg"
                ),
                str(
                    target.get("file_name")
                    or path.name
                ),
            )

        if not payloads:
            return False

        # ---------------------------------------------------------
        # IMPORTANT:
        # Never modify media while campaign is actively serving.
        # ---------------------------------------------------------

        campaign_status = await promotion.get_campaign_status(
            test.wb_campaign_id,
            refresh=True,
        )

        if campaign_status == 9:
            await self._pause_campaign_confirmed(
                test,
                promotion,
            )

        elif campaign_status not in {4, 7, 8, 11}:
            test.campaign_state = "unknown"
            test.operation_state = (
                ABTestOperationStatus.RECONCILIATION_REQUIRED.value
            )
            test.incident_id = (
                test.incident_id or self._incident_id()
            )

            raise ABTestReconciliationRequired(
                f"Recovery uchun campaign holati xavfsiz emas: "
                f"{campaign_status}",
                incident_id=test.incident_id,
            )

        # ---------------------------------------------------------
        # Preserve variant position before clearing pending state.
        # ---------------------------------------------------------

        variant_position = int(
            pending.get("variant_position")
            or test.current_variant_order
            or 1
        )

        upload_order = [
            slot
            for slot in sorted(
                payloads,
                reverse=True,
            )
            if slot != 1
        ]

        if 1 in payloads:
            upload_order.append(1)

        # ---------------------------------------------------------
        # Upload + verify EVERY slot separately.
        # ---------------------------------------------------------

        for slot in upload_order:
            data, mime, filename = payloads[slot]

            await content.upload_media_file(
                nm_id=test.nm_id,
                photo_number=slot,
                content=data,
                filename=filename,
                content_type=mime,
            )

            verified = await self._verify_uploaded_image(
                test=test,
                slot=slot,
                expected_data=data,
                content=content,
                attempts=self.IMAGE_VERIFY_ATTEMPTS,
                delay_seconds=self.IMAGE_VERIFY_DELAY_SECONDS,
                threshold=self.IMAGE_PHASH_THRESHOLD,
            )

            # -----------------------------------------------------
            # First verification failed.
            # Keep campaign paused and schedule one-hour retry.
            # -----------------------------------------------------

            if not verified:
                test.media_status = "waiting_image_reupload"

                verification = (
                    pending.get("verification") or {}
                )

                verification["status"] = "waiting_reupload"

                verification["reupload_count"] = max(
                    int(
                        verification.get(
                            "reupload_count"
                        ) or 0
                    ),
                    1,
                )

                verification["next_retry_at"] = (
                    self._now().timestamp()
                    + self.IMAGE_REUPLOAD_DELAY_SECONDS
                )

                pending["verification"] = verification

                state["pending"] = pending
                state["status"] = "waiting_image_reupload"

                await self._set_media_state(
                    test,
                    state,
                )

                await self.db.commit()

                return False

            # -----------------------------------------------------
            # Slot verified successfully.
            # Save shadow information.
            # -----------------------------------------------------

            state.setdefault(
                "shadows",
                {},
            )[str(slot)] = {
                "path": str(
                    targets[slot]["shadow_path"]
                ),
                "mime": mime,
                "file_name": filename,
            }

        # ---------------------------------------------------------
        # ALL slots verified successfully.
        # ---------------------------------------------------------

        state["pending"] = None
        state["current_variant_position"] = variant_position
        state["status"] = "variant_applied"

        state["expected_slot_count"] = int(
            state.get("slot_count")
            or len(
                state.get("backups") or {}
            )
        )

        state["expected_snapshot"] = {
            str(slot): {
                "url": (state.get("backups") or {}).get(str(slot), {}).get("url") or ""
            }
            for slot in range(1, int(state["expected_slot_count"]) + 1)
        }

        test.current_variant_order = variant_position
        test.stage_started_at = self._now()
        test.media_status = "variant_applied"

        await self._set_media_state(
            test,
            state,
        )

        return True

    async def _mark_recovered_running(
        self,
        test: ABTest,
        operation: ABTestOperation | None,
        campaign_id: int,
    ) -> None:
        test.status = ABTestStatus.RUNNING
        test.started_at = test.started_at or self._now()
        test.finished_at = None
        test.last_error = None
        test.incident_id = None
        test.operation_state = ABTestOperationStatus.SUCCEEDED.value
        test.campaign_state = "running"
        test.media_status = "variant_applied"
        if operation:
            operation.external_id = int(campaign_id)
            operation.response_snapshot = {
                "phase": "campaign_running_recovered",
                "campaign_id": int(campaign_id),
                "current_variant_order": int(test.current_variant_order or 0),
            }
            operation.status = ABTestOperationStatus.SUCCEEDED
            operation.last_error = None
        await self.db.flush()

    async def reconcile(self, user_id: int, test_id: int) -> ABTest:
        """Reconcile a persisted incident without starting a new campaign."""
        test = await self.repository.get_for_user(user_id, test_id)
        if not test:
            raise HTTPException(status_code=404, detail="A/B-тест не найден")
        async with self._operation_lock(test.connection_id, test.nm_id, user_id):
            test = await self.repository.get_for_update(user_id, test_id)
            if not test:
                raise HTTPException(status_code=404, detail="A/B-тест не найден")
            if test.operation_state not in {
                ABTestOperationStatus.PREPARED.value,
                ABTestOperationStatus.IN_PROGRESS.value,
                ABTestOperationStatus.RECONCILIATION_REQUIRED.value,
            } and test.status not in {ABTestStatus.FAILED}:
                return test
            connection = await self._connection_or_404(user_id, test.connection_id, require_ab_access=False)
            content, promotion = await self._clients(connection)
            try:
                latest = await self.repository.get_latest_operation(test.id)
                phase = (latest.response_snapshot or {}).get("phase") if latest else None
                if test.wb_campaign_id:
                    campaign_status = await promotion.get_campaign_status(test.wb_campaign_id, refresh=True)
                    if test.campaign_state == "starting":
                        if campaign_status == 9:
                            test.status = ABTestStatus.RUNNING
                            test.started_at = test.started_at or self._now()
                            test.stage_started_at = test.stage_started_at or test.started_at
                            test.campaign_state = "running"
                            test.operation_state = ABTestOperationStatus.SUCCEEDED.value
                            test.incident_id = None
                            test.last_error = None
                            await self.db.commit()
                            return await self.repository.get_for_user(user_id, test_id)  # type: ignore[return-value]
                        if campaign_status not in self.NON_ACTIVE_CAMPAIGN_STATUSES:
                            await self._stop_campaign_confirmed(test, promotion)
                        test.status = ABTestStatus.DRAFT
                        test.campaign_state = "stopped"
                        test.operation_state = ABTestOperationStatus.FAILED.value
                        test.last_error = "Запуск не подтверждён. Кампания остановлена; повтор запуска использует ту же кампанию."
                        await self.db.commit()
                        return await self.repository.get_for_user(user_id, test_id)  # type: ignore[return-value]
                    recovered_media = await self._recover_pending_variant_media(
                        test,
                        content,
                        promotion,
                    )

                    state = self._media_state(test)

                    # Recovery verification failed.
                    # Keep the campaign paused and let the scheduler
                    # perform the one-hour image reupload retry.
                    if state.get("status") == "waiting_image_reupload":
                        test.status = ABTestStatus.RUNNING
                        test.campaign_state = "paused"
                        test.operation_state = (
                            ABTestOperationStatus.IN_PROGRESS.value
                        )

                        await self.db.commit()

                        return await self.repository.get_for_user(
                            user_id,
                            test_id,
                        )  # type: ignore[return-value]

                    if recovered_media:
                        campaign_status = await promotion.get_campaign_status(
                            test.wb_campaign_id,
                            refresh=True,
                        )

                        if campaign_status in self.RESUMABLE_CAMPAIGN_STATUSES:
                            test.campaign_state = "starting"

                            await promotion.start_campaign(
                                test.wb_campaign_id,
                            )

                            campaign_status = (
                                await self._confirm_campaign_active(
                                    promotion,
                                    test.wb_campaign_id,
                                )
                            )

                        if campaign_status == 9:
                            await self._mark_recovered_running(
                                test,
                                latest,
                                test.wb_campaign_id,
                            )

                            await self.db.commit()

                            return await self.repository.get_for_user(
                                user_id,
                                test_id,
                            )  # type: ignore[return-value]

                        raise ABTestReconciliationRequired(
                            "Изображения восстановлены, "
                            "но активный статус существующей "
                            "кампании ещё не подтверждён.",
                            incident_id=(
                                test.incident_id
                                or self._incident_id()
                            ),
                        )

                    await self._stop_campaign_confirmed(
                        test,
                        promotion,
                    )
                elif test.campaign_state == "unknown" or phase in {
                    "campaign_create_sent",
                    "campaign_created",
                    "campaign_start_sent",
                }:
                    test.campaign_state = "unknown"
                    test.operation_state = ABTestOperationStatus.RECONCILIATION_REQUIRED.value
                    test.incident_id = test.incident_id or self._incident_id()
                    raise ABTestReconciliationRequired(
                        "Не удалось определить ID рекламной кампании после прерванного запроса. Проверьте кампанию в кабинете WB и обратитесь к оператору.",
                        incident_id=test.incident_id,
                    )
                else:
                    test.campaign_state = "not_created"
                state = self._media_state(test)
                if state.get("version") == 1 and (state.get("touched") or state.get("pending")):
                    await self._restore_original(test, content)
                elif state.get("version") == 1:
                    test.media_status = "original"
                test.status = ABTestStatus.DRAFT
                test.operation_state = "reconciled"
                test.last_error = None
                test.finished_at = None
                if latest and latest.status != ABTestOperationStatus.SUCCEEDED:
                    latest.status = ABTestOperationStatus.FAILED
                    latest.last_error = "Сверка выполнена, существующая операция продолжена"
                await self.db.commit()
            except Exception as exc:
                test.status = ABTestStatus.FAILED
                test.operation_state = ABTestOperationStatus.RECONCILIATION_REQUIRED.value
                test.incident_id = test.incident_id or self._incident_id()
                test.last_error = (
                    f"Сверка ещё не завершена. Инцидент {test.incident_id}. {self._safe_error(exc)}"
                )[:2000]
                await self.db.commit()
                raise self._api_error(exc) from exc
        return await self.repository.get_for_user(user_id, test_id)  # type: ignore[return-value]

    async def recover_unfinished_operations(self) -> None:
        """Recover only abandoned operations while holding the same card lock.

        Re-read the operation after acquiring the lock: the request that was
        live when the scan began may have completed in the meantime.
        """
        operations = await self.repository.list_reconciliation_operations()
        candidates = [(op.operation_key, op.user_id, op.test_id) for op in operations]
        await self.db.commit()
        for key, user_id, test_id in candidates:
            candidate = await self.repository.get_for_user(user_id, test_id)
            if not candidate:
                continue
            async with self._operation_lock(candidate.connection_id, candidate.nm_id, user_id, wait=False) as acquired:
                if not acquired:
                    continue
                test = await self.repository.get_for_update(user_id, test_id)
                self.db.expire_all()
                test = await self.repository.get_for_update(user_id, test_id)
                operation = await self.repository.get_operation(key)
                if not test or not operation or operation.status not in {
                    ABTestOperationStatus.PREPARED, ABTestOperationStatus.IN_PROGRESS,
                    ABTestOperationStatus.RECONCILIATION_REQUIRED,
                }:
                    continue
                state = self._media_state(test)
                pending = state.get("pending") or {}
                phase = (operation.response_snapshot or {}).get("phase")
                if test.status == ABTestStatus.RUNNING and (
                    pending.get("kind") in {"swap", "replace", "replace_with_parking"}
                    or test.media_status == "waiting_image_reupload"
                ):
                    continue
                if operation.status == ABTestOperationStatus.RECONCILIATION_REQUIRED:
                    # Preserve the original incident and let the safety sweep
                    # retry stop/restore instead of inventing a restart.
                    continue
                before = self._state_snapshot(test)
                if (operation.status == ABTestOperationStatus.PREPARED
                        and not test.wb_campaign_id and phase in {None, "preparing_card"}):
                    operation.status = ABTestOperationStatus.FAILED
                    test.status = ABTestStatus.DRAFT
                    test.operation_state = ABTestOperationStatus.FAILED.value
                    test.last_error = "Подготовка прервана. Внешних действий не было; подтвердите запуск заново."
                    test.lifecycle_lock = False
                else:
                    operation.status = ABTestOperationStatus.RECONCILIATION_REQUIRED
                    test.status = ABTestStatus.FAILED
                    test.operation_state = ABTestOperationStatus.RECONCILIATION_REQUIRED.value
                    test.incident_id = test.incident_id or self._incident_id()
                    test.last_error = f"Незавершённая операция требует сверки. Инцидент {test.incident_id}."
                operation.last_error = test.last_error
                await self._audit(test, "abandoned_operation_recovered", before, details={"operation_id": operation.id})
                await self.db.commit()

    async def sync(self, user_id: int, test_id: int) -> ABTest:
        test = await self.repository.get_for_user(user_id, test_id)
        if not test:
            raise HTTPException(status_code=404, detail="A/B-тест не найден")
        async with self._operation_lock(test.connection_id, test.nm_id, user_id):
            test = await self.repository.get_for_update(user_id, test_id)
            if not test:
                raise HTTPException(status_code=404, detail="A/B-тест не найден")
            if test.status != ABTestStatus.RUNNING:
                return test
            try:
                await self._sync_loaded(test)
            except Exception as exc:
                logger.exception("A/B sync failed test_id=%s nm_id=%s", test.id, test.nm_id)
                test.last_error = test.last_error or ABTestService._safe_error(exc)
                if isinstance(exc, ABTestReconciliationRequired):
                    test.operation_state = ABTestOperationStatus.RECONCILIATION_REQUIRED.value
                    test.incident_id = test.incident_id or self._incident_id()
                await self.db.commit()
                raise self._api_error(exc) from exc
        return await self.repository.get_for_user(user_id, test_id)  # type: ignore[return-value]

    @staticmethod
    def _stats_date(value):
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone(ZoneInfo("Europe/Moscow")).date()
        return value

    @staticmethod
    def _validated_stats(totals: Any) -> tuple[str, tuple[int, int, int | None, float] | None]:
        if not isinstance(totals, dict):
            return "incomplete", None
        available = totals.get("_available_metrics")
        if not isinstance(available, dict):
            available = {key: key in totals for key in ("views", "clicks", "orders", "sum")}
        if not totals.get("_has_data", any(available.values())):
            return "no_data", None
        if totals.get("_data_complete") is False or not all(available.get(key) for key in ("views", "clicks", "sum")):
            return "incomplete", None
        try:
            raw = [totals["views"], totals["clicks"], totals.get("orders") if available.get("orders") else None, totals["sum"]]
            for i, value in enumerate(raw):
                if i == 2 and value is None:
                    continue
                if isinstance(value, bool) or not math.isfinite(float(value)) or float(value) < 0:
                    return "invalid", None
                if i < 3 and float(value) != int(value):
                    return "invalid", None
            views, clicks = int(raw[0]), int(raw[1])
            if clicks > views:
                return "invalid", None
            return "complete", (views, clicks, int(raw[2]) if raw[2] is not None else None, round(float(raw[3]), 2))
        except (TypeError, ValueError, OverflowError, KeyError):
            return "invalid", None

    async def _record_stats_observation(self, test: ABTest, totals: Any, *, quality: str, purpose: str = "poll") -> None:
        # An append-only normalized source observation survives later WB
        # corrections. Non-finite/malformed input must still serialize safely.
        def safe(value):
            if isinstance(value, float) and not math.isfinite(value):
                return str(value)
            if isinstance(value, dict):
                return {str(key): safe(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [safe(item) for item in value]
            return value
        await self._audit(test, "stats_observed", self._state_snapshot(test), details={
            "purpose": purpose, "quality": quality, "observed_at": self._now().isoformat(),
            "campaign_totals": safe(totals),
        })
        if hasattr(self.db, "add") and isinstance(totals, dict) and "sum" in totals:
            try:
                await self.repository.add_budget_entry(
                    test_id=test.id,
                    operation_id=None,
                    kind="spend_snapshot",
                    amount_rub=round(float(totals.get("sum") or 0), 2),
                    provider_balance_rub=None,
                    source="provider",
                    metadata_json={"quality": quality, "views": int(totals.get("views") or 0), "clicks": int(totals.get("clicks") or 0)},
                )
            except Exception:
                # A stats observation must remain durable even if an optional
                # ledger write is unavailable during a legacy migration.
                logger.exception("Could not append provider spend snapshot test_id=%s", test.id)

    async def _retain_unallocated_statistics(self, test: ABTest, views: int, clicks: int, spend: float) -> None:
        # Older builds wrote campaign deltas to photos without an attributable
        # source. Preserve that observation in the journal, then remove the
        # unsupported attribution instead of silently keeping misleading CTRs.
        former = [{"position": variant.position, "views": variant.views, "clicks": variant.clicks,
                   "orders": variant.orders, "spend_rub": variant.spend_rub}
                  for variant in test.variants if variant.views or variant.clicks or variant.spend_rub]
        if former:
            await self._audit(test, "unsupported_photo_attribution_removed", self._state_snapshot(test), details={"variants": former})
            for variant in test.variants:
                variant.views = variant.clicks = variant.orders = 0
                variant.spend_rub = 0
                variant.is_winner = False
            test.winner_variant_order = None
        test.unallocated_views = views
        test.unallocated_clicks = clicks
        test.unallocated_spend_rub = spend

    async def _stats_safety_stop(self, test: ABTest, content, promotion, message: str) -> None:
        test.stats_quality = "reconciliation_required"
        test.operation_state = ABTestOperationStatus.RECONCILIATION_REQUIRED.value
        test.incident_id = test.incident_id or self._incident_id()
        try:
            await self._finish_loaded(test, content, promotion, stopped=True)
        finally:
            # _finish_loaded may report a safe lifecycle completion, but that
            # does not resolve the source-data incident or authorize a winner.
            test.stats_quality = "reconciliation_required"
            test.last_error = f"{message} Инцидент {test.incident_id}."[:2000]
            await self.db.commit()

    async def _fullstats_windowed(self, promotion, campaign_id: int, started_at, *, refresh: bool = False):
        """Fetch long-running campaign statistics in WB's <=31-day windows.

        WB fullstats has a bounded date range. Never silently truncate a test
        older than one month: aggregate complete windows instead.
        """
        end = self._stats_date(self._now())
        begin = self._stats_date(started_at) or end
        if begin > end:
            raise WBApiError("Начальная дата статистики позже текущей даты WB")
        totals = {"views": 0, "clicks": 0, "orders": 0, "sum": 0.0, "_has_data": False,
                  "_data_complete": True,
                  "_available_metrics": {"views": True, "clicks": True, "orders": True, "sum": True},
                  "_windows": [], "_variant_totals": {}, "_variant_attribution_complete": True}
        cursor = begin
        while cursor <= end:
            window_end = min(cursor + timedelta(days=30), end)
            kwargs = {"started_at": cursor, "end_at": window_end}
            if refresh:
                kwargs["refresh"] = True
            part = await promotion.fullstats(campaign_id, **kwargs)
            quality, _ = self._validated_stats(part)
            if quality == "invalid":
                raise WBApiError("WB вернул некорректные счётчики статистики")
            totals["views"] += int(part.get("views", 0) or 0)
            totals["clicks"] += int(part.get("clicks", 0) or 0)
            totals["orders"] += int(part.get("orders", 0) or 0)
            totals["sum"] += float(part.get("sum", 0) or 0)
            available = part.get("_available_metrics")
            if not isinstance(available, dict):
                available = {key: key in part for key in ("views", "clicks", "orders", "sum")}
            totals["_has_data"] = totals["_has_data"] or bool(part.get("_has_data", any(available.values())))
            totals["_data_complete"] = totals["_data_complete"] and quality == "complete"
            part_variant_totals = part.get("_variant_totals") if isinstance(part, dict) else None
            if not part.get("_variant_attribution_complete", False) or not isinstance(part_variant_totals, dict):
                totals["_variant_attribution_complete"] = False
            elif totals["_variant_attribution_complete"]:
                for position, metrics in part_variant_totals.items():
                    target = totals["_variant_totals"].setdefault(str(position), {"views": 0.0, "clicks": 0.0, "orders": 0.0, "sum": 0.0})
                    for metric in target:
                        target[metric] += float(metrics.get(metric, 0) or 0)
            totals["_windows"].append({"begin": cursor.isoformat(), "end": window_end.isoformat(),
                                        "observed_at": part.get("_observed_at"), "quality": quality})
            for key in totals["_available_metrics"]:
                totals["_available_metrics"][key] = totals["_available_metrics"][key] and bool(available.get(key, False))
            cursor = window_end + timedelta(days=1)
        totals["sum"] = round(totals["sum"], 2)
        if not totals["_variant_attribution_complete"]:
            totals.pop("_variant_totals", None)
        else:
            totals["_variant_totals"] = {
                position: {metric: round(value, 2) for metric, value in metrics.items()}
                for position, metrics in totals["_variant_totals"].items()
            }
        return totals

    async def _sync_loaded(self, test: ABTest) -> None:
        connection = test.connection
        content, promotion = await self._clients(connection)
        if self._media_state(test).get("minimum_bid_pause"):
            # Only a fresh explicit user confirmation can resume this stage.
            # A background tick must not raise CPM or discard its counters.
            await self.db.commit()
            return
        if test.media_status == "waiting_image_reupload":
            handled = await self._process_pending_image_retry(
                test,
                content,
                promotion,
            )

            if handled:
                await self.db.commit()
                return

        # Re-check stock, card availability and WB's advertisable-card list on
        # every scheduler cycle.  A card can become blocked or out of stock
        # after the paid campaign has started; in that case the normal safety
        # stop path must run before another stats poll.
        get_card = getattr(content, "get_card", None)
        if callable(get_card) and callable(getattr(promotion, "is_card_advertisable", None)):
            try:
                live_card = await get_card(test.nm_id)
                self._validate_card_eligibility(live_card)
                await self._assert_card_advertisable(live_card, promotion, test.nm_id)
            except Exception as exc:
                test.operation_state = ABTestOperationStatus.RECONCILIATION_REQUIRED.value
                test.incident_id = test.incident_id or self._incident_id()
                raise ABTestReconciliationRequired(
                    f"Карточка больше не подтверждена как доступная для рекламы. Инцидент {test.incident_id}.",
                    incident_id=test.incident_id,
                ) from exc

        state = self._media_state(test)
        pending = state.get("pending") or {}

        pending_kind = pending.get("kind")

        if (
            pending_kind
            in {
                "swap",
                "replace",
                "replace_with_parking",
            }
            and state.get("status")
            not in {
                "waiting_image_reupload",
                "variant_applied",
            }
        ):
            logger.warning(
                "A/B pending media operation detected; "
                "starting recovery "
                "test_id=%s nm_id=%s kind=%s slots=%s",
                test.id,
                test.nm_id,
                pending_kind,
                pending.get("slots"),
            )

            recovered = await self._recover_pending_variant_media(
                test,
                content,
                promotion,
            )

            state = self._media_state(test)

            # First verification failed during recovery.
            # Campaign stays paused for the 1-hour retry.
            if state.get("status") == "waiting_image_reupload":
                test.status = ABTestStatus.RUNNING
                test.campaign_state = "paused"
                test.operation_state = (
                    ABTestOperationStatus.IN_PROGRESS.value
                )

                await self.db.commit()
                return

            if recovered:
                campaign_status = await promotion.get_campaign_status(
                    test.wb_campaign_id,
                    refresh=True,
                )

                # Campaign can be resumed only from resumable states.
                if campaign_status in self.RESUMABLE_CAMPAIGN_STATUSES:
                    test.campaign_state = "starting"
                    await self.db.flush()

                    await promotion.start_campaign(
                        test.wb_campaign_id,
                    )

                    campaign_status = (
                        await self._confirm_campaign_active(
                            promotion,
                            test.wb_campaign_id,
                        )
                    )

                if campaign_status == 9:
                    test.status = ABTestStatus.RUNNING
                    test.campaign_state = "running"
                    test.operation_state = (
                        ABTestOperationStatus.SUCCEEDED.value
                    )
                    test.media_status = "variant_applied"

                    await self.db.commit()
                    return

                # Campaign ended / disappeared while recovering.
                test.operation_state = (
                    ABTestOperationStatus.RECONCILIATION_REQUIRED.value
                )
                test.incident_id = (
                    test.incident_id or self._incident_id()
                )

                raise ABTestReconciliationRequired(
                    "Изображения восстановлены, "
                    "но существующая кампания WB "
                    "не может быть безопасно продолжена.",
                    incident_id=test.incident_id,
                )
        if not test.wb_campaign_id:
            raise RuntimeError("У теста отсутствует ID рекламной кампании WB")
        started_at = self._stats_date(test.started_at)
        totals = None
        # Cache only the common <=31-day window. Long-running tests use the
        # explicit windowed reader so no historical data is silently dropped.
        if started_at and (self._stats_date(self._now()) - started_at).days <= 30:
            totals = promotion.cached_fullstats(test.wb_campaign_id, started_at=started_at)
        if totals is None:
            async with self._stats_operation_lock(test.connection_id):
                totals = await self._fullstats_windowed(promotion, test.wb_campaign_id, started_at)

        quality, values = self._validated_stats(totals)
        await self._record_stats_observation(test, totals, quality=quality)
        if quality in {"no_data", "incomplete"}:
            test.stats_quality = quality
            reference = test.last_synced_at or test.stage_started_at or test.started_at
            stale = reference is None or (self._now() - (reference if reference.tzinfo else reference.replace(tzinfo=timezone.utc))).total_seconds() >= self.MAX_STATS_STALE_SECONDS
            stage_start = test.stage_started_at or test.started_at
            no_progress_timeout = max(int(getattr(settings, "ab_test_no_progress_timeout_sec", 172800) or 172800), self.MIN_NO_PROGRESS_TIMEOUT_SECONDS)
            stage_expired = bool(stage_start and int(test.stage_views or 0) == 0 and (
                self._now() - (stage_start if stage_start.tzinfo else stage_start.replace(tzinfo=timezone.utc))
            ).total_seconds() >= no_progress_timeout)
            if stale or stage_expired:
                await self._stats_safety_stop(test, content, promotion, "Тест остановлен: свежая полная статистика WB недоступна более 5 минут.")
            await self.db.commit()
            return
        if quality == "invalid" or values is None:
            await self._stats_safety_stop(test, content, promotion, "Тест остановлен: WB вернул некорректные счётчики (отрицательные, нечисловые или кликов больше показов).")
            return
        raw_views, raw_clicks, raw_orders, raw_spend = values
        previous_views = int(test.last_total_views or 0)
        previous_clicks = int(test.last_total_clicks or 0)
        previous_orders = int(getattr(test, "last_total_orders", 0) or 0)
        previous_spend = float(test.last_total_spend_rub or 0)
        if (
            raw_views < 0
            or raw_clicks < 0
            or (raw_orders is not None and raw_orders < 0)
            or (raw_spend is not None and raw_spend < 0)
            or raw_views < previous_views
            or raw_clicks < previous_clicks
            or (raw_orders is not None and raw_orders < previous_orders)
            or (raw_spend is not None and raw_spend < previous_spend)
        ):
            await self._stats_safety_stop(test, content, promotion, "Статистика WB уменьшилась задним числом. Кампания остановлена, предыдущие данные сохранены для сверки.")
            return
        # A temporary WB/API read failure may have left an informational
        # error behind while the campaign continued serving. Once a complete
        # sync succeeds, remove only that stale runtime state. Real incidents
        # keep ``reconciliation_required`` and are never hidden here.
        if test.operation_state != ABTestOperationStatus.RECONCILIATION_REQUIRED.value:
            test.last_error = None
            test.incident_id = None
        views = raw_views
        clicks = raw_clicks
        spend = raw_spend if raw_spend is not None else previous_spend
        delta_views = views - previous_views
        delta_clicks = clicks - previous_clicks
        delta_orders = raw_orders - previous_orders if raw_orders is not None else 0
        delta_spend = spend - previous_spend if raw_spend is not None else 0.0
        current = self._variant_by_position(test, test.current_variant_order)
        if current:
            # Live fullstats is campaign-level and may contain delayed events
            # from a previous photo. Do NOT write deltas into the variant while
            # the stage is running. Keep only provisional stage exposure; the
            # a confirmed pause and stable read-back establish an observed
            # boundary, but do not prove attribution to a particular photo.
            provisional_views = max(0, views - int(test.settled_total_views or 0))
            provisional_clicks = max(0, clicks - int(test.settled_total_clicks or 0))
            provisional_spend = max(0.0, spend - float(test.settled_total_spend_rub or 0))
            test.stage_views = provisional_views
            test.stage_clicks = provisional_clicks
            test.stage_spend_rub = provisional_spend
            test.stats_quality = "preliminary"
        await self._retain_unallocated_statistics(test, views, clicks, spend)
        test.last_total_views = views
        test.last_total_clicks = clicks
        if raw_orders is not None:
            test.last_total_orders = raw_orders
        if raw_spend is not None:
            test.last_total_spend_rub = spend
        test.last_synced_at = self._now()

        # A successful but permanently empty stats response is different from
        # a temporary API error. Bound a test with no impressions so it cannot
        # keep a paid campaign alive forever without measurable progress.
        no_progress_timeout = max(
            int(getattr(settings, "ab_test_no_progress_timeout_sec", 172800) or 172800),
            self.MIN_NO_PROGRESS_TIMEOUT_SECONDS,
        )
        if current and int(test.stage_views or 0) <= 0 and (test.stage_started_at or test.started_at):
            started_at = test.stage_started_at or test.started_at
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=timezone.utc)
            if (self._now() - started_at).total_seconds() >= no_progress_timeout:
                await self._finish_loaded(test, content, promotion, stopped=True)
                test.last_error = (
                    f"Тест остановлен: за {no_progress_timeout // 3600} ч. не получено ни одного показа. "
                    "Проверьте доступность товара и настройки кампании перед повторным запуском."
                )[:2000]
                await self.db.commit()
                return

        # Treat the approved test budget as a hard safety ceiling. WB can
        # return delayed spend totals, so this is intentionally checked before
        # any next-photo mutation; it prevents the scheduler from knowingly
        # extending a test after the agreed amount is reached.
        spend_limit = float(test.budget_rub or 0)
        reserve = max(float(getattr(settings, "ab_test_budget_guard_reserve_rub", 300) or 300), 0.0)
        guard_limit = max(0.0, spend_limit - reserve)
        if current and spend_limit > 0 and spend >= guard_limit:
            await self._finish_loaded(test, content, promotion, stopped=True)
            test.last_error = (
                f"Тест остановлен до превышения согласованного лимита {int(spend_limit)} ₽: "
                f"защитный остаток {int(round(reserve))} ₽, фактический расход по данным WB — {int(round(spend))} ₽."
            )[:2000]
            await self.db.commit()
            return

        required_views = int(test.views_per_variant or 0)
        get_min_bid = getattr(promotion, "get_min_bid", None)
        if callable(get_min_bid):
            minimum_bid = await get_min_bid(
                campaign_id=test.wb_campaign_id, nm_id=test.nm_id,
                placement=test.placement, bid_type=test.bid_type,
            )
            if minimum_bid is None or minimum_bid < 1:
                await self._stats_safety_stop(test, content, promotion, "WB не подтвердил актуальную минимальную ставку; тест остановлен.")
                return
            if int(minimum_bid) > int(test.cpm_rub):
                before = self._state_snapshot(test)
                await self._pause_campaign_confirmed(test, promotion)
                state = self._media_state(test)
                state["minimum_bid_pause"] = {
                    "minimum_cpm": int(minimum_bid), "previous_cpm": int(test.cpm_rub),
                    "paused_at": self._now().isoformat(),
                    "stage_started_at": test.stage_started_at.isoformat() if test.stage_started_at else None,
                }
                await self._set_media_state(test, state)
                test.operation_state = "awaiting_confirmation"
                test.last_error = (
                    f"Кампания на паузе: минимальная ставка WB выросла до {int(minimum_bid)} ₽. "
                    "Согласованная ставка, фотографии и накопленные данные сохранены. "
                    "Для продолжения нужно новое подтверждение ставки и бюджета."
                )[:2000]
                await self._audit(test, "minimum_bid_pause", before, details=state["minimum_bid_pause"])
                await self.db.commit()
                return
        if current and required_views > 0 and int(test.stage_views or 0) >= required_views:
            next_position = next(
                (variant.position for variant in test.variants if variant.source_type != "control" and variant.position > test.current_variant_order),
                None,
            )
            if next_position is None:
                await self._finish_loaded(test, content, promotion)
            else:
                next_variant = self._variant_by_position(test, next_position)
                if not next_variant:
                    raise RuntimeError(f"Не найден вариант {next_position}")
                try:
                    # A running campaign must be paused before changing the
                    # product image. A successful pause request alone is not enough;
                    # status 11 must be confirmed through WB read-back.
                    await self._pause_campaign_confirmed(
                        test,
                        promotion,
                    )
                    await self._settle_stage_stats(test, promotion)
                    await self._audit(test, "stage_settled", {"current_variant_order": test.current_variant_order}, details={"views": test.stage_views, "clicks": test.stage_clicks})

                    await self._apply_variant(
                        test,
                        next_variant,
                        content,
                        promotion,
                    )

                    # _apply_variant confirms the image through CDN/pHash.
                    # If the image was not confirmed it schedules the one-hour
                    # retry and returns without resuming the campaign.
                    if test.media_status == "waiting_image_reupload":
                        return

                    test.campaign_state = "starting"

                    await promotion.start_campaign(
                        test.wb_campaign_id,
                    )

                    campaign_status = await self._confirm_campaign_active(
                        promotion,
                        test.wb_campaign_id,
                    )

                    if campaign_status != 9:
                        test.operation_state = (
                            ABTestOperationStatus.RECONCILIATION_REQUIRED.value
                        )
                        test.incident_id = test.incident_id or self._incident_id()

                        raise ABTestReconciliationRequired(
                            "Изображение обновлено и подтверждено, "
                            "но повторный запуск кампании WB не подтверждён.",
                            incident_id=test.incident_id,
                        )

                    test.campaign_state = "running"
                except Exception as exc:
                    logger.exception(
                        "A/B switch failed test_id=%s nm_id=%s next_position=%s",
                        test.id,
                        test.nm_id,
                        next_position,
                    )
                    # A rollback changes the displayed photo. Resuming the
                    # campaign here would count original-photo exposure as
                    # the interrupted variant. Finish the lifecycle safely;
                    # any unconfirmed stop remains an explicit obligation.
                    details = f"Тест остановлен при переключении варианта {next_position}: {self._safe_error(exc)}"
                    try:
                        await self._finish_loaded(test, content, promotion, stopped=True)
                    except Exception as cleanup_exc:
                        test.status = ABTestStatus.FAILED
                        test.operation_state = ABTestOperationStatus.RECONCILIATION_REQUIRED.value
                        test.incident_id = test.incident_id or self._incident_id()
                        test.lifecycle_lock = True
                        details += f" Безопасная остановка не завершена: {self._safe_error(cleanup_exc)}."
                    if test.incident_id:
                        details += f" | Инцидент {test.incident_id}"
                    test.last_error = details[:2000]
                    await self.db.commit()
                    raise RuntimeError(details) from exc
                test.current_variant_order = next_position
                test.stage_started_at = self._now()
                test.stage_views = 0
                test.stage_clicks = 0
                test.stage_spend_rub = 0
                test.stats_quality = "preliminary"
        await self.db.commit()

    async def get(self, user_id: int, test_id: int) -> ABTest:
        """Return a test and clear a stale non-blocking runtime warning.

        ``incident_id`` is allocated before an external start so that an
        interrupted request can be tracked. It is not itself proof of an
        unresolved incident. If a running test has a succeeded operation and
        is not in reconciliation, an old generic provider-read warning must
        not remain visible forever after the test has recovered.
        """
        test = await self.repository.get_for_user(user_id, test_id)
        if not test:
            raise HTTPException(status_code=404, detail="A/B-тест не найден")
        if (
            test.status == ABTestStatus.RUNNING
            and test.operation_state == ABTestOperationStatus.SUCCEEDED.value
            and test.stats_quality != "reconciliation_required"
            and test.last_error == "Не удалось надёжно определить результат запроса к Wildberries"
        ):
            test.last_error = None
            test.incident_id = None
            await self.db.commit()
        return test

    async def list(self, user_id: int, connection_id: int | None = None, status_value: ABTestStatus | None = None) -> list[ABTest]:
        return await self.repository.list_for_user(user_id, connection_id=connection_id, status=status_value)

    @staticmethod
    def response(test: ABTest) -> dict[str, Any]:
        variants: list[dict[str, Any]] = []
        for variant in test.variants:
            image_url = None
            if variant.file_path:
                image_url = ABTestService._signed_media_url(variant.file_path)
            elif variant.source_url:
                image_url = variant.source_url
            variants.append(
                {
                    "id": variant.id,
                    "position": variant.position,
                    "source_type": variant.source_type,
                    "file_name": variant.file_name,
                    "image_url": image_url,
                    "source_url": variant.source_url,
                    "wb_url": variant.wb_url,
                    "views": variant.views,
                    "clicks": variant.clicks,
                    "orders": int(variant.orders or 0),
                    "ctr": variant.ctr if variant.views and test.stats_quality == "stage_attributed" else None,
                    "cpo": round(variant.spend_rub / variant.orders, 2) if variant.orders else None,
                    "spend_rub": round(variant.spend_rub or 0, 2),
                    "is_winner": variant.is_winner,
                }
            )
        return {
            "id": test.id,
            "connection_id": test.connection_id,
            "store_name": test.connection.store_name,
            "nm_id": test.nm_id,
            "title": test.title,
            "status": test.status.value if isinstance(test.status, ABTestStatus) else str(test.status),
            "wb_campaign_id": test.wb_campaign_id,
            "skip_current_photo": test.skip_current_photo,
            "keep_winner_as_main": test.keep_winner_as_main,
            "delete_test_media": test.delete_test_media,
            "views_per_variant": test.views_per_variant,
            "cpm_rub": test.cpm_rub,
            "budget_rub": ABTestService._required_budget_for_test(test),
            "bid_type": test.bid_type or "unified",
            "placement": test.placement,
            "current_variant_order": test.current_variant_order,
            "winner_variant_order": test.winner_variant_order,
            "winner_decision": test.winner_decision,
            "operation_state": test.operation_state or "ready",
            "campaign_state": test.campaign_state or "not_created",
            "media_status": test.media_status or "original",
            "stats_quality": test.stats_quality or "not_started",
            "incident_id": test.incident_id,
            "unallocated_views": int(test.unallocated_views or 0),
            "unallocated_clicks": int(test.unallocated_clicks or 0),
            "unallocated_spend_rub": round(test.unallocated_spend_rub or 0, 2),
            "funding_source": test.funding_source or "auto",
            "stage_views": int(test.stage_views or 0),
            "stage_clicks": int(test.stage_clicks or 0),
            "stage_spend_rub": round(test.stage_spend_rub or 0, 2),
            "total_views": test.last_total_views,
            "total_clicks": test.last_total_clicks,
            "total_orders": int(getattr(test, "last_total_orders", 0) or 0),
            "total_spend_rub": round(test.last_total_spend_rub or 0, 2),
            "total_ctr": round(
                (int(test.last_total_clicks or 0) / int(test.last_total_views or 0)) * 100,
                4,
            ) if test.last_total_views else None,
            "total_cpo": round(
                float(test.last_total_spend_rub or 0) / int(getattr(test, "last_total_orders", 0) or 0),
                2,
            ) if getattr(test, "last_total_orders", 0) else None,
            "last_error": test.last_error,
            "started_at": test.started_at,
            "finished_at": test.finished_at,
            "last_synced_at": test.last_synced_at,
            "created_at": test.created_at,
            "draft_fingerprint": ABTestService._draft_fingerprint(test),
            "start_confirmation_fingerprint": ABTestService._draft_fingerprint(test),
            "stage_exposure_views": int(getattr(test, "stage_views", 0) or 0),
            "variants": variants,
        }

    async def _scheduler_sync_one(self, test: ABTest, semaphore: asyncio.Semaphore) -> None:
        async with semaphore:
            try:
                async with self._operation_lock(test.connection_id, test.nm_id, test.user_id):
                    locked_test = await self.repository.get_for_update(test.user_id, test.id)
                    if not locked_test or locked_test.status != ABTestStatus.RUNNING:
                        return
                    try:
                        await self._sync_loaded(locked_test)
                    except Exception as exc:
                        logger.exception("A/B scheduler sync failed test_id=%s nm_id=%s", locked_test.id, locked_test.nm_id)
                        locked_test.last_error = locked_test.last_error or ABTestService._safe_error(exc)
                        reference = locked_test.last_synced_at or locked_test.started_at
                        stale_seconds = None
                        if reference:
                            if reference.tzinfo is None:
                                reference = reference.replace(tzinfo=timezone.utc)
                            stale_seconds = max((self._now() - reference).total_seconds(), 0.0)
                        must_stop = (
                            isinstance(exc, ABTestReconciliationRequired)
                            or (isinstance(exc, WBApiError) and exc.status_code in {401, 403})
                            or stale_seconds is None
                            or stale_seconds >= self.MAX_STATS_STALE_SECONDS
                        )
                        if must_stop and locked_test.status == ABTestStatus.RUNNING:
                            locked_test.operation_state = ABTestOperationStatus.RECONCILIATION_REQUIRED.value
                            locked_test.stats_quality = "reconciliation_required"
                            locked_test.incident_id = locked_test.incident_id or self._incident_id()
                            try:
                                connection = await self._connection_or_404(locked_test.user_id, locked_test.connection_id, require_ab_access=False)
                                content, promotion = await self._clients(connection)
                                await self._finish_loaded(locked_test, content, promotion, stopped=True)
                                locked_test.last_error = (
                                    f"Синхронизация недоступна более {self.MAX_STATS_STALE_SECONDS // 60} мин. "
                                    "Кампания остановлена, исходные фото восстановлены. "
                                    f"Причина: {ABTestService._safe_error(exc)}"
                                )[:2000]
                            except Exception as safety_exc:
                                locked_test.status = ABTestStatus.FAILED
                                locked_test.operation_state = ABTestOperationStatus.RECONCILIATION_REQUIRED.value
                                locked_test.incident_id = locked_test.incident_id or self._incident_id()
                                locked_test.last_error = (
                                    "Синхронизация недоступна, безопасная остановка не завершена. "
                                    f"Инцидент {locked_test.incident_id}. {ABTestService._safe_error(safety_exc)}"
                                )[:2000]
                    await self.db.commit()
            except asyncio.TimeoutError:
                logger.error("A/B scheduler test timed out test_id=%s", test.id)
                try:
                    locked = await self.repository.get_for_user(test.user_id, test.id)
                    if locked:
                        locked.operation_state = ABTestOperationStatus.RECONCILIATION_REQUIRED.value
                        locked.incident_id = locked.incident_id or self._incident_id()
                        locked.last_error = f"Планировщик превысил timeout {settings.ab_test_scheduler_per_test_timeout_sec} сек. Инцидент {locked.incident_id}."
                        await self.db.commit()
                except Exception:
                    await self.db.rollback()

    async def _scheduler_safety_one(self, stuck_test: ABTest, semaphore: asyncio.Semaphore) -> None:
        async with semaphore:
            async with self._operation_lock(stuck_test.connection_id, stuck_test.nm_id, stuck_test.user_id):
                locked_stuck = await self.repository.get_for_update(stuck_test.user_id, stuck_test.id)
                if not locked_stuck or not locked_stuck.wb_campaign_id:
                    return
                if locked_stuck.campaign_state not in {
                    "running", "starting", "unknown", "pause_requested", "stop_requested", "created",
                }:
                    return
                try:
                    connection = await self._connection_or_404(locked_stuck.user_id, locked_stuck.connection_id, require_ab_access=False)
                    content, promotion = await self._clients(connection)
                    await self._assert_campaign_configuration(locked_stuck, promotion)
                    current_status = await promotion.get_campaign_status(locked_stuck.wb_campaign_id, refresh=True)
                    if locked_stuck.campaign_state == "starting" and current_status == 9:
                        locked_stuck.status = ABTestStatus.RUNNING
                        locked_stuck.started_at = locked_stuck.started_at or self._now()
                        locked_stuck.stage_started_at = locked_stuck.stage_started_at or locked_stuck.started_at
                        locked_stuck.campaign_state = "running"
                        locked_stuck.operation_state = ABTestOperationStatus.SUCCEEDED.value
                        locked_stuck.incident_id = None
                        locked_stuck.last_error = None
                    elif current_status in self.NON_ACTIVE_CAMPAIGN_STATUSES:
                        locked_stuck.campaign_state = "stopped"
                    else:
                        await self._stop_campaign_confirmed(locked_stuck, promotion)
                    locked_stuck.last_error = (
                        (locked_stuck.last_error or "").rstrip()
                        + " | Реклама остановлена автоматической проверкой планировщика."
                    )[:2000]
                except Exception as safety_exc:
                    logger.warning(
                        "A/B safety sweep campaign_id=%s test_id=%s: %s",
                        locked_stuck.wb_campaign_id,
                        locked_stuck.id,
                        ABTestService._safe_error(safety_exc),
                    )
                    locked_stuck.last_error = (
                        "Повторная попытка безопасной остановки не удалась: "
                        f"{ABTestService._safe_error(safety_exc)}"
                    )[:2000]
                await self.db.commit()

    async def scheduler_tick(self) -> None:
        await self.recover_unfinished_operations()
        concurrency = max(int(getattr(settings, "ab_test_scheduler_concurrency", 4) or 4), 1)
        timeout_seconds = max(int(getattr(settings, "ab_test_scheduler_per_test_timeout_sec", 600) or 600), 15)
        semaphore = asyncio.Semaphore(concurrency)

        async def run_one(user_id: int, test_id: int, safety: bool) -> None:
            async with semaphore:
                # AsyncSession is mutable transaction state and cannot be
                # shared between concurrent tasks.
                async with AsyncSessionLocal() as worker_db:
                    service = ABTestService(worker_db)
                    candidate = await service.repository.get_for_user(user_id, test_id)
                    if not candidate:
                        return
                    action = service._scheduler_safety_one if safety else service._scheduler_sync_one
                    try:
                        await asyncio.wait_for(action(candidate, asyncio.Semaphore(1)), timeout_seconds)
                    except Exception as exc:
                        await worker_db.rollback()
                        logger.error("A/B scheduler task failed test_id=%s: %s", test_id, self._safe_error(exc))
                        # A cancelled task is not a successful finish. Persist
                        # the safety obligation in a fresh transaction.
                        candidate = await service.repository.get_for_user(user_id, test_id)
                        if candidate:
                            async with service._operation_lock(candidate.connection_id, candidate.nm_id, user_id):
                                candidate = await service.repository.get_for_update(user_id, test_id)
                                if candidate and candidate.lifecycle_lock:
                                    candidate.status = ABTestStatus.FAILED
                                    candidate.operation_state = ABTestOperationStatus.RECONCILIATION_REQUIRED.value
                                    candidate.incident_id = candidate.incident_id or service._incident_id()
                                    candidate.last_error = f"Фоновая операция не завершена; требуется безопасная остановка. Инцидент {candidate.incident_id}."
                                    await worker_db.commit()

        for safety, loader in ((False, self.repository.list_running), (True, self.repository.list_unresolved_campaigns)):
            candidates = [(test.user_id, test.id) for test in await loader()]
            await self.db.commit()
            await asyncio.gather(*(run_one(user_id, test_id, safety) for user_id, test_id in candidates))
