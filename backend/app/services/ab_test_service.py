from __future__ import annotations

from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import asyncio
import base64
import hashlib
import hmac
import io
import json
import logging
import math
import mimetypes
import secrets
import struct
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from fastapi import HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.ab_test import ABTest, ABTestOperation, ABTestOperationStatus, ABTestStatus, ABTestVariant
from app.repositories.ab_test_repository import ABTestRepository
from app.schemas.ab_test import ABTestCreateRequest, ABTestStartRequest
from app.services.ab_test_budget import calculate_required_budget
from app.services.wb_content_client import WBApiError, WBContentClient
from app.services.wb_promotion_client import WBPromotionClient
from app.services.wb_rate_limiter import WBRateLimiter
from app.services.wb_token_service import WBTokenService


logger = logging.getLogger(__name__)


class ABTestReconciliationRequired(RuntimeError):
    """The last external effect may have happened, but cannot be proven yet."""

    def __init__(self, message: str, *, incident_id: str | None = None):
        super().__init__(message)
        self.incident_id = incident_id


class ABTestService:
    MAX_IMAGE_BYTES = 32 * 1024 * 1024
    MAX_VARIANTS = 30
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
    WINNER_MIN_SCORE_DELTA = 0.06
    CAMPAIGN_ACTIVE_CONFIRM_DELAYS = (0.0, 1.0, 2.0, 4.0, 8.0, 15.0)
    CAMPAIGN_STOP_CONFIRM_DELAYS = (0.0, 1.0, 2.0, 4.0, 8.0, 15.0)
    CAMPAIGN_BUDGET_CONFIRM_DELAYS = (0.0, 1.0, 2.0, 4.0, 8.0, 15.0)
    MAX_STATS_STALE_SECONDS = 300

    def __init__(self, db: AsyncSession):
        self.db = db
        self.repository = ABTestRepository(db)

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
        return calculate_required_budget(tested_variant_count, test.views_per_variant, test.cpm_rub)

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

    @asynccontextmanager
    async def _operation_lock(self, connection_id: int, nm_id: int):
        async with WBRateLimiter.operation_lock(self.db, connection_id, nm_id):
            yield

    @asynccontextmanager
    async def _stats_operation_lock(self, connection_id: int):
        async with WBRateLimiter.stats_lock(self.db, connection_id):
            yield

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
        test = await self.repository.create(
            user_id=user_id,
            connection_id=connection.id,
            nm_id=request.nm_id,
            title=request.title or card.get("title") or f"A/B-тест {request.nm_id}",
            status=ABTestStatus.DRAFT,
            skip_current_photo=request.skip_current_photo,
            keep_winner_as_main=request.keep_winner_as_main,
            delete_test_media=request.delete_test_media,
            views_per_variant=request.views_per_variant,
            cpm_rub=request.cpm_rub,
            budget_rub=request.budget_rub,
            bid_type="unified",
            placement="combined",
        )
        await self.db.commit()
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
    def _prepare_image(cls, filename: str, content_type: str, data: bytes) -> tuple[str, str, bytes]:
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
            return converted_name, "image/jpeg", converted

        cls._validate_image(original_name, normalized_type, data)
        return original_name, (
            normalized_type if normalized_type in cls.ALLOWED_IMAGE_TYPES else mimetypes.guess_type(original_name)[0] or "image/jpeg"
        ), data

    async def preview_image(self, user_id: int, file: UploadFile) -> dict[str, Any]:
        """Convert an uploaded image to a browser-previewable data URL."""
        del user_id  # Authentication is enforced by the router; previews are not persisted.
        data = await file.read(self.MAX_IMAGE_BYTES + 1)
        filename, content_type, prepared = await asyncio.to_thread(
            self._prepare_image,
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
        if position < 1 or position > 30:
            raise HTTPException(status_code=422, detail="Позиция варианта должна быть от 1 до 30")
        test = await self.repository.get_for_user(user_id, test_id)
        if not test:
            raise HTTPException(status_code=404, detail="A/B-тест не найден")
        async with self._operation_lock(test.connection_id, test.nm_id):
            test = await self.repository.get_for_update(user_id, test_id)
            if not test:
                raise HTTPException(status_code=404, detail="A/B-тест не найден")
            if test.status != ABTestStatus.DRAFT:
                raise HTTPException(status_code=409, detail="Изменять варианты можно только в черновике")
            data = await file.read(self.MAX_IMAGE_BYTES + 1)
            original_filename = file.filename or "image.jpg"
            filename, content_type, data = await asyncio.to_thread(
                self._prepare_image,
                original_filename,
                file.content_type or "",
                data,
            )
            extension = Path(filename).suffix.lower()
            directory = self._media_root() / "ab_tests" / str(test.id)
            directory.mkdir(parents=True, exist_ok=True)
            old = await self.repository.get_variant(test.id, position)
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
            raise HTTPException(status_code=422, detail="Позиция варианта должна быть от 1 до 30")
        clean_url = str(source_url or "").strip()
        parsed = urlsplit(clean_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise HTTPException(status_code=400, detail="Выберите корректное изображение из карточки Wildberries")
        test = await self.repository.get_for_user(user_id, test_id)
        if not test:
            raise HTTPException(status_code=404, detail="A/B-тест не найден")
        async with self._operation_lock(test.connection_id, test.nm_id):
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
            old = await self.repository.get_variant(test.id, position)
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
        async with self._operation_lock(test.connection_id, test.nm_id):
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
        async with self._operation_lock(test.connection_id, test.nm_id):
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
            relative = base / f"slot_{slot}{extension}"
            target = self._media_root() / relative
            target.write_bytes(data)
            backups[str(slot)] = {
                "path": str(relative),
                "url": url,
                "mime": mime or "image/jpeg",
                "file_name": Path(urlsplit(url).path).name or f"original_{slot}{extension}",
            }

        source_slots = {
            slot
            for variant in variants
            if variant.source_type == "card" and variant.source_url
            for slot, url in enumerate(original_media, start=1)
            if self._canonical_media_url(url) == self._canonical_media_url(variant.source_url or "")
        }
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
                }
                for slot, entry in backups.items()
            },
        }
        test.original_media = list(original_media)
        test.original_main_backup_path = backups["1"]["path"]
        test.media_status = "backed_up"
        await self._set_media_state(test, state)
        return state

    @classmethod
    def _source_slot(cls, state: dict[str, Any], variant: ABTestVariant) -> int | None:
        source_url = variant.source_url
        if variant.source_type != "card" or not source_url:
            return None
        for raw_slot, entry in (state.get("backups") or {}).items():
            if not isinstance(entry, dict):
                continue
            if cls._canonical_media_url(str(entry.get("url") or "")) == cls._canonical_media_url(source_url):
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

    def _write_shadow(self, test: ABTest, slot: int, data: bytes, mime: str, filename: str) -> dict[str, Any]:
        extension = self._image_extension(mime, filename)
        relative = Path("ab_tests") / str(test.id) / "current" / f"slot_{slot}{extension}"
        target = self._media_root() / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        old_path = (self._media_state(test).get("shadows") or {}).get(str(slot), {}).get("path")
        target.write_bytes(data)
        if old_path and str(old_path) != str(relative):
            self._state_file(str(old_path)).unlink(missing_ok=True)
        return {
            "path": str(relative),
            "mime": mime or "image/jpeg",
            "file_name": filename or target.name,
        }

    async def _apply_variant(self, test: ABTest, variant: ABTestVariant, content: WBContentClient) -> None:
        state = self._media_state(test)
        if state.get("version") == 1 and state.get("backups"):
            await self._apply_variant_with_slot_swap(test, variant, content, state)
            return

        # Compatibility path for tests and A/B runs created before the slot
        # state migration. New runs always use the exact slot engine above.
        data, mime, filename = await self._variant_bytes(test, variant, content)
        # Uploaded files are already prepared before persistence. Keep this
        # final guard synchronous so the polling worker remains deterministic
        # when a remote/card source also happens to be a TIFF.
        filename, mime, data = self._prepare_image(filename, mime, data)
        self._validate_image(filename, mime, data)
        logger.info(
            "A/B apply variant test_id=%s nm_id=%s position=%s source=%s bytes=%s",
            test.id,
            test.nm_id,
            variant.position,
            variant.source_type,
            len(data),
        )
        await content.upload_media_file(nm_id=test.nm_id, photo_number=1, content=data, filename=filename, content_type=mime)
        # The successful upload response is authoritative. The card is not
        # read back here: WB can expose a stale CDN list or renumber photos
        # after a successful write, which must not stop a running test.
        logger.info(
            "A/B variant upload accepted test_id=%s nm_id=%s position=%s",
            test.id,
            test.nm_id,
            variant.position,
        )

    async def _apply_variant_with_slot_swap(
        self,
        test: ABTest,
        variant: ABTestVariant,
        content: WBContentClient,
        state: dict[str, Any],
    ) -> None:
        source_slot = self._source_slot(state, variant)
        if source_slot:
            # Card sources are backed up locally at test start. This prevents a
            # later swap or a delayed WB CDN response from changing the source
            # bytes that are about to be copied.
            data, mime, filename = self._read_state_slot(state, source_slot)
        else:
            data, mime, filename = await self._variant_bytes(test, variant, content)
        filename, mime, data = self._prepare_image(filename, mime, data)
        self._validate_image(filename, mime, data)

        changed_slots: set[int] = {1}
        current_main, current_main_mime, current_main_name = self._read_state_slot(state, 1)
        targets: dict[int, tuple[bytes, str, str]] = {1: (data, mime, filename)}
        kind = "replace"

        if variant.source_type == "card" and source_slot and source_slot != 1:
            # True two-way swap: put the currently active main image into the
            # source slot first, then put the selected original into slot 1.
            # This is the crucial part missing from the previous implementation.
            targets[source_slot] = (current_main, current_main_mime, current_main_name)
            changed_slots.add(source_slot)
            kind = "swap"
        elif variant.source_type == "upload" or not source_slot:
            parking_slot = state.get("parking_slot")
            if parking_slot and int(parking_slot) != 1:
                parking_slot = int(parking_slot)
                # Parking the current main keeps it recoverable while a custom
                # upload is active and mirrors the legacy engine behaviour.
                targets[parking_slot] = (current_main, current_main_mime, current_main_name)
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
            "targets": {str(slot): {key: value for key, value in meta.items()} for slot, meta in target_meta.items()},
        }
        await self._set_media_state(test, state)

        # Write the non-main slot first. If the second request fails, the main
        # photo is still the old one and the full backup can restore both slots.
        upload_order = [slot for slot in sorted(targets, reverse=True) if slot != 1] + [1]
        # One logical operation gets one external write sequence. A successful
        # upload is enough; no CDN/card read-back is part of the operation.
        for slot in upload_order:
            slot_data, slot_mime, slot_name = targets[slot]
            await content.upload_media_file(
                nm_id=test.nm_id,
                photo_number=slot,
                content=slot_data,
                filename=slot_name,
                content_type=slot_mime,
            )
        state["shadows"] = {**(state.get("shadows") or {}), **shadow_entries}
        state["pending"] = None
        state["current_variant_position"] = variant.position
        state["status"] = "variant_applied"
        state["expected_slot_count"] = int(state.get("slot_count") or len(state.get("backups") or {}))
        state["expected_snapshot"] = {
            str(slot): {"url": (state.get("backups") or {}).get(str(slot), {}).get("url") or ""}
            for slot in range(1, int(state["expected_slot_count"]) + 1)
        }
        test.media_status = "variant_applied"
        await self._set_media_state(test, state)
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
        *,
        allow_pending: bool = False,
    ) -> None:
        state = self._media_state(test)
        if state.get("version") == 1 and state.get("backups"):
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
                    }
                    for slot, target in targets.items()
                },
            }
            await self._set_media_state(test, state)
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
            return

        # Compatibility path for tests and records created before 0007.
        original = [str(url).strip() for url in (test.original_media or []) if str(url).strip()]
        logger.info("A/B restore original media test_id=%s nm_id=%s photos=%s", test.id, test.nm_id, len(original))
        if original:
            await content.save_media(nm_id=test.nm_id, urls=original)
        elif test.original_main_backup_path:
            path = self._media_root() / test.original_main_backup_path
            if path.is_file():
                await content.upload_media_file(
                    nm_id=test.nm_id,
                    photo_number=1,
                    content=path.read_bytes(),
                    filename=path.name,
                    content_type=mimetypes.guess_type(path.name)[0] or "image/jpeg",
                )
        else:
            raise RuntimeError("У теста отсутствует резервная копия исходных изображений")

        logger.info(
            "A/B original media restore accepted test_id=%s nm_id=%s photos=%s",
            test.id,
            test.nm_id,
            len(original),
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

        test.campaign_state = "stop_requested"
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
                test.campaign_state = "stopped"
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
        if test.started_at or test.campaign_state not in {"created", "stopped"}:
            return False
        phase = (operation.response_snapshot or {}).get("phase")
        operation_status = operation.status.value if isinstance(operation.status, ABTestOperationStatus) else str(operation.status)
        return phase in {
            "campaign_created",
            "campaign_reused",
            "campaign_reused_minimum_bid",
            "budget_deposit_sent",
            "budget_deposited",
            "budget_deposit_pending",
            "budget_deposit_rejected",
        } and operation_status != ABTestOperationStatus.SUCCEEDED.value

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
        async with self._operation_lock(test.connection_id, test.nm_id):
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
            if test.status != ABTestStatus.DRAFT:
                raise HTTPException(status_code=409, detail="Запустить можно только черновик A/B-теста")
            running_test = await self.repository.get_running_for_card(test.connection_id, test.nm_id, exclude_test_id=test.id)
            if running_test:
                raise HTTPException(status_code=409, detail="Для этой карточки уже запущен другой A/B-тест")
            connection = await self._connection_or_404(user_id, test.connection_id)
            content, promotion = await self._clients(connection)
            variants = [variant for variant in test.variants if variant.source_type != "control"]
            if len(variants) < self.WINNER_MIN_VARIANTS:
                raise HTTPException(status_code=400, detail="Добавьте минимум 2 изображения для сравнения")
            if len(variants) > self.MAX_VARIANTS:
                raise HTTPException(status_code=400, detail="В одном тесте может быть не больше 30 изображений")
            positions = sorted(variant.position for variant in variants)
            if positions != list(range(1, len(positions) + 1)):
                raise HTTPException(status_code=400, detail="Позиции изображений должны идти последовательно, начиная с 1")

            # A previous attempt may have created a campaign and then stopped
            # before the first impression (for example because WB raised the
            # minimum CPM). Verify that exact campaign before allowing a
            # resume. Never create a second campaign for this situation.
            latest_operation = await self.repository.get_latest_operation(test.id)
            # A resume request from an older UI may omit the source and arrive
            # as ``auto``. Reuse the source that the user confirmed for the
            # existing operation instead of silently switching between the
            # Promotion account, mutual settlements, and promo bonus.
            selected_funding_source = request.funding_source
            saved_funding_source = (latest_operation.request_snapshot or {}).get("funding_source") if latest_operation else None
            if selected_funding_source == "auto" and saved_funding_source in {"account", "mutual", "bonus"}:
                selected_funding_source = saved_funding_source
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
                card = await content.get_card(test.nm_id)
                original_media = WBContentClient.photo_urls(card or {})
                if not original_media:
                    raise RuntimeError("Карточка товара не найдена или в ней нет исходного изображения")
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
                    test.budget_rub = calculate_required_budget(
                        tested_variant_count,
                        test.views_per_variant,
                        minimum_bid,
                    )
                    await self.db.flush()
                    if resume_campaign_id:
                        # The user already confirmed a resume action. Apply
                        # the current WB minimum to this same campaign and
                        # continue; the first-start flow still returns the
                        # structured 409 popup for an explicit review.
                        operation.response_snapshot = {
                            "phase": "campaign_reused_minimum_bid",
                            "campaign_id": resume_campaign_id,
                            "previous_cpm": previous_cpm,
                            "minimum_cpm": int(minimum_bid),
                            "recalculated_budget": int(test.budget_rub),
                        }
                        await self.db.commit()
                    else:
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
                                "reuse_existing_campaign": False,
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
                        test.budget_rub = calculate_required_budget(
                            tested_variant_count,
                            test.views_per_variant,
                            refreshed_minimum,
                        )
                        if resume_campaign_id:
                            operation.response_snapshot = {
                                "phase": "campaign_reused_minimum_bid",
                                "campaign_id": int(campaign_id),
                                "previous_cpm": previous_cpm,
                                "minimum_cpm": int(refreshed_minimum),
                                "recalculated_budget": int(test.budget_rub),
                            }
                            await self.db.commit()
                            await promotion.set_bid(
                                campaign_id=campaign_id,
                                nm_id=test.nm_id,
                                cpm_rub=test.cpm_rub,
                                placement=test.placement,
                                bid_type=test.bid_type,
                            )
                        else:
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
                                    "reuse_existing_campaign": False,
                                },
                            ) from bid_error
                    raise

                estimated_budget = calculate_required_budget(
                    tested_variant_count,
                    test.views_per_variant,
                    test.cpm_rub,
                )
                # The required campaign balance is deterministic: every test
                # stage receives the requested impressions at the effective
                # CPM. The frontend shows the same formula; the backend is the
                # final source of truth after WB minimum-bid correction.
                test.budget_rub = estimated_budget
                required_budget = max(estimated_budget, int(request.deposit_rub or 0))
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
                        # Persist the rejected phase before surfacing a clear
                        # user-facing error; retrying must never create a new
                        # campaign just because the funding source was empty.
                        operation.response_snapshot = {
                            "phase": "budget_deposit_rejected",
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
                        raise HTTPException(
                            status_code=409,
                            detail={
                                "code": "campaign_budget_deposit_failed",
                                "message": (
                                    f"Wildberries не принял автоматическое пополнение кампании WB #{int(campaign_id)} "
                                    f"на {shortfall} ₽. {provider_message}. "
                                    f"Проверьте доступность {funding_source_label} в кабинете WB и повторите запуск. "
                                    "Будет продолжена эта же кампания, новая создана не будет."
                                ),
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
                    await self._apply_variant(test, first, content)
                    test.current_variant_order = 1
                else:
                    control = self._variant_by_position(test, 0)
                    if not control:
                        control = await self.repository.add_variant(
                            test_id=test.id, position=0, source_type="control", source_url=original_media[0], file_name="Текущее фото"
                        )
                    test.current_variant_order = 0
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
                test.finished_at = None
                test.last_synced_at = None
                test.last_total_views = 0
                test.last_total_clicks = 0
                test.last_total_orders = 0
                test.last_total_spend_rub = 0
                test.winner_variant_order = None
                test.winner_decision = None
                test.last_error = None
                test.incident_id = None
                test.operation_state = ABTestOperationStatus.SUCCEEDED.value
                test.campaign_state = "running"
                test.media_status = "variant_applied" if test.skip_current_photo else "original"
                test.stats_quality = "preliminary"
                operation.response_snapshot = {"phase": "campaign_running_confirmed", "campaign_id": campaign_id}
                await self._operation_state(test, operation, ABTestOperationStatus.SUCCEEDED)
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

                state = self._media_state(test)
                media_needs_restore = media_changed or bool(state.get("touched") or state.get("pending"))
                media_restored = not media_needs_restore
                if media_needs_restore and original_media:
                    try:
                        await self._restore_original(test, content, allow_pending=True)
                        media_restored = True
                    except Exception as cleanup_exc:
                        cleanup_errors.append(f"восстановление фото не подтверждено: {self._safe_error(cleanup_exc)}")

                unresolved = bool(cleanup_errors) or (campaign_creation_attempted and not campaign_id) or not campaign_stopped or not media_restored
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
        """Apply the same conservative decision rules as the legacy engine.

        CTR alone is too noisy for small samples. The score combines normalized
        CTR, a clicks proxy and confidence, then requires both a minimum sample
        and a meaningful gap from the runner-up.
        """
        # Aggregate stats are collected sequentially: each completed stage
        # belongs to the photo that was active during that stage. They are not
        # a reason to hide the completed result. Only an unresolved
        # reconciliation incident must block winner selection.
        if getattr(test, "stats_quality", None) == "reconciliation_required":
            return None, "statistics_not_attributable"
        candidates = [variant for variant in test.variants if variant.source_type != "control"]
        if len(candidates) < cls.WINNER_MIN_VARIANTS:
            return None, "insufficient_data"
        if min((variant.views for variant in candidates), default=0) < cls.WINNER_MIN_IMPRESSIONS:
            return None, "insufficient_data"

        max_ctr = max((variant.ctr for variant in candidates), default=0.0)
        proxy_target = max(round(cls.WINNER_MIN_IMPRESSIONS * 0.03), 1)
        max_click_signal = max((min(variant.clicks / proxy_target, 1.0) for variant in candidates), default=0.0)

        def score(variant: ABTestVariant) -> float:
            ctr_score = variant.ctr / max_ctr if max_ctr else 0.0
            click_signal = min(variant.clicks / proxy_target, 1.0)
            click_score = click_signal / max_click_signal if max_click_signal else 0.0
            confidence = min(variant.views / cls.WINNER_MIN_IMPRESSIONS, 1.0)
            return 0.55 * ctr_score + 0.25 * click_score + 0.20 * confidence

        ranked = sorted(candidates, key=lambda variant: (score(variant), variant.ctr, variant.views, variant.clicks, -variant.position), reverse=True)
        winner, runner_up = ranked[0], ranked[1]
        ctr_delta = abs(winner.ctr - runner_up.ctr)
        score_delta = abs(score(winner) - score(runner_up))
        if ctr_delta < cls.WINNER_MIN_CTR_DELTA or score_delta < cls.WINNER_MIN_SCORE_DELTA:
            return None, "no_clear_winner"
        return winner, "winner_found"

    @classmethod
    def _select_winner(cls, test: ABTest) -> ABTestVariant | None:
        return cls._winner_result(test)[0]

    async def _finish_loaded(self, test: ABTest, content: WBContentClient, promotion: WBPromotionClient, *, stopped: bool = False) -> None:
        stop_error: Exception | None = None
        if test.wb_campaign_id:
            try:
                await self._stop_campaign_confirmed(test, promotion)
            except Exception as exc:
                # Restore is a separate obligation. It is still attempted, but
                # the experiment cannot be reported as finished until both
                # campaign stop and media restoration are confirmed.
                stop_error = exc
        winner, decision = (None, "test_interrupted") if stopped else self._winner_result(test)
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
            await self._restore_original(test, content, allow_pending=True)
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
        test.status = ABTestStatus.STOPPED if stopped else ABTestStatus.FINISHED
        test.finished_at = self._now()
        test.operation_state = ABTestOperationStatus.SUCCEEDED.value
        test.media_status = "restored"
        test.last_error = None

    async def stop(self, user_id: int, test_id: int) -> ABTest:
        test = await self.repository.get_for_user(user_id, test_id)
        if not test:
            raise HTTPException(status_code=404, detail="A/B-тест не найден")
        async with self._operation_lock(test.connection_id, test.nm_id):
            test = await self.repository.get_for_update(user_id, test_id)
            if not test:
                raise HTTPException(status_code=404, detail="A/B-тест не найден")
            if test.status not in {ABTestStatus.RUNNING, ABTestStatus.DRAFT}:
                raise HTTPException(status_code=409, detail="A/B-тест уже завершён")
            if test.status == ABTestStatus.DRAFT:
                test.status = ABTestStatus.STOPPED
                test.winner_decision = "test_interrupted"
                test.finished_at = self._now()
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
    ) -> bool:
        """Replay an interrupted variant write from local shadow files.

        A worker can stop after WB accepted one of several slot writes. The
        journal therefore stores the exact local payload for every slot. On
        recovery we replay those idempotent slot writes; no card read-back,
        URL comparison, ordinal comparison, digest, or fingerprint check is
        involved. The only fingerprint retained by this service is the
        start-request idempotency key; it is unrelated to image content.
        """
        state = self._media_state(test)
        pending = state.get("pending")
        if not isinstance(pending, dict) or pending.get("kind") not in {"swap", "replace", "replace_with_parking"}:
            return False
        raw_targets = pending.get("targets") or {}
        if not isinstance(raw_targets, dict) or not raw_targets:
            return False
        targets = {
            int(raw_slot): dict(target)
            for raw_slot, target in raw_targets.items()
            if isinstance(target, dict)
        }
        if not targets:
            return False

        payloads: dict[int, tuple[bytes, str, str]] = {}
        for slot, target in targets.items():
            shadow_path = target.get("shadow_path")
            if not shadow_path:
                # Pending records written by the pre-replay implementation do
                # not contain enough local data to safely repeat the upload.
                return False
            path = self._state_file(str(shadow_path))
            if not path.is_file():
                return False
            payloads[slot] = (
                path.read_bytes(),
                str(target.get("mime") or mimetypes.guess_type(path.name)[0] or "image/jpeg"),
                str(target.get("file_name") or path.name),
            )

        upload_order = [slot for slot in sorted(payloads, reverse=True) if slot != 1] + [1]
        for slot in upload_order:
            data, mime, filename = payloads[slot]
            await content.upload_media_file(
                nm_id=test.nm_id,
                photo_number=slot,
                content=data,
                filename=filename,
                content_type=mime,
            )

        state["shadows"] = {
            **(state.get("shadows") or {}),
            **{
                str(slot): {
                    "path": str(targets[slot]["shadow_path"]),
                    "mime": payloads[slot][1],
                    "file_name": payloads[slot][2],
                }
                for slot in payloads
            },
        }
        state["pending"] = None
        state["current_variant_position"] = int(pending.get("variant_position") or test.current_variant_order or 1)
        state["status"] = "variant_applied"
        state["expected_slot_count"] = int(state.get("slot_count") or len(state.get("backups") or {}))
        test.current_variant_order = int(state["current_variant_position"])
        test.media_status = "variant_applied"
        await self._set_media_state(test, state)
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
        async with self._operation_lock(test.connection_id, test.nm_id):
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
                    recovered_media = await self._recover_pending_variant_media(test, content)
                    if recovered_media:
                        campaign_status = await promotion.get_campaign_status(test.wb_campaign_id, refresh=True)
                        if campaign_status in self.RESUMABLE_CAMPAIGN_STATUSES:
                            await promotion.start_campaign(test.wb_campaign_id)
                            campaign_status = await self._confirm_campaign_active(promotion, test.wb_campaign_id)
                        if campaign_status == 9:
                            await self._mark_recovered_running(test, latest, test.wb_campaign_id)
                            await self.db.commit()
                            return await self.repository.get_for_user(user_id, test_id)  # type: ignore[return-value]
                        raise ABTestReconciliationRequired(
                            "Изображения восстановлены, но активный статус существующей кампании ещё не подтверждён.",
                            incident_id=test.incident_id or self._incident_id(),
                        )
                    await self._stop_campaign_confirmed(test, promotion)
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
                    await self._restore_original(test, content, allow_pending=True)
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

    async def sync(self, user_id: int, test_id: int) -> ABTest:
        test = await self.repository.get_for_user(user_id, test_id)
        if not test:
            raise HTTPException(status_code=404, detail="A/B-тест не найден")
        async with self._operation_lock(test.connection_id, test.nm_id):
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

    async def _sync_loaded(self, test: ABTest) -> None:
        connection = test.connection
        content, promotion = await self._clients(connection)
        if not test.wb_campaign_id:
            raise RuntimeError("У теста отсутствует ID рекламной кампании WB")
        started_at = test.started_at.date() if test.started_at else None
        totals = promotion.cached_fullstats(test.wb_campaign_id, started_at=started_at)
        if totals is None:
            async with self._stats_operation_lock(test.connection_id):
                totals = await promotion.fullstats(test.wb_campaign_id, started_at=started_at)
        raw_views = int(totals.get("views", 0))
        raw_clicks = int(totals.get("clicks", 0))
        raw_orders = int(totals.get("orders", 0))
        raw_spend = float(totals.get("sum", 0))
        previous_views = int(test.last_total_views or 0)
        previous_clicks = int(test.last_total_clicks or 0)
        previous_orders = int(getattr(test, "last_total_orders", 0) or 0)
        previous_spend = float(test.last_total_spend_rub or 0)
        if (
            raw_views < previous_views
            or raw_clicks < previous_clicks
            or raw_orders < previous_orders
            or raw_spend < previous_spend
        ):
            test.stats_quality = "reconciliation_required"
            test.operation_state = ABTestOperationStatus.RECONCILIATION_REQUIRED.value
            test.incident_id = test.incident_id or self._incident_id()
            test.last_error = (
                f"Статистика WB уменьшилась или изменилась задним числом. Данные сохранены до сверки. "
                f"Инцидент {test.incident_id}."
            )[:2000]
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
        spend = raw_spend
        delta_views = views - previous_views
        delta_clicks = max(clicks - previous_clicks, 0)
        delta_orders = max(raw_orders - previous_orders, 0)
        delta_spend = max(spend - previous_spend, 0)
        current = self._variant_by_position(test, test.current_variant_order)
        if current:
            # fullstats is campaign-level data, not photo-level attribution.
            # Keep the current progress for switching, but explicitly mark the
            # result as aggregate/unverified so a CTR is never presented as a
            # proven photo result.
            current.views = int(current.views or 0) + delta_views
            current.clicks = int(current.clicks or 0) + delta_clicks
            current.orders = int(current.orders or 0) + delta_orders
            current.spend_rub = float(current.spend_rub or 0) + delta_spend
            # These values are the auditable campaign delta. They are shown
            # separately from the current variant because WB fullstats does
            # not identify which photo generated each impression or click.
            test.unallocated_views = int(test.unallocated_views or 0) + delta_views
            test.unallocated_clicks = int(test.unallocated_clicks or 0) + delta_clicks
            test.unallocated_spend_rub = float(test.unallocated_spend_rub or 0) + delta_spend
            test.stats_quality = "aggregate_unverified"
        test.last_total_views = views
        test.last_total_clicks = clicks
        test.last_total_orders = raw_orders
        test.last_total_spend_rub = spend
        test.last_synced_at = self._now()

        required_views = int(test.views_per_variant or 0)
        if current and required_views > 0 and current.views >= required_views:
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
                    # Keep the existing WB campaign serving while the content
                    # API changes the main slot. This is the same sequence as
                    # the reference WB Optimizer project. Calling
                    # adv/v0/stop here is unsafe: WB rejects it when the
                    # campaign is already transitioning or not stoppable and
                    # the test then gets a false "stopped" incident.
                    await self._apply_variant(test, next_variant, content)
                    test.campaign_state = "running"
                except Exception as exc:
                    logger.exception(
                        "A/B switch failed test_id=%s nm_id=%s next_position=%s",
                        test.id,
                        test.nm_id,
                        next_position,
                    )
                    cleanup_errors: list[str] = []
                    media_restored = False
                    try:
                        await self._restore_original(test, content, allow_pending=True)
                        media_restored = True
                    except Exception as cleanup_exc:
                        cleanup_errors.append(f"восстановление фото не подтверждено: {self._safe_error(cleanup_exc)}")

                    # A failed write with a successful local rollback is a
                    # transient switch error, not a finished test. Leave the
                    # same campaign running and let the next scheduler tick
                    # retry the same variant. Only an unsuccessful rollback
                    # becomes a reconciliation incident.
                    if media_restored:
                        test.status = ABTestStatus.RUNNING
                        test.campaign_state = "running"
                        test.operation_state = ABTestOperationStatus.SUCCEEDED.value
                        test.finished_at = None
                        test.incident_id = None
                    else:
                        test.status = ABTestStatus.FAILED
                        test.operation_state = ABTestOperationStatus.RECONCILIATION_REQUIRED.value
                        test.incident_id = test.incident_id or self._incident_id()
                        test.winner_decision = "test_interrupted"
                        test.finished_at = self._now()
                    details = f"Тест остановлен при переключении варианта {next_position}: {self._safe_error(exc)}"
                    if media_restored:
                        details = (
                            f"Переключение варианта {next_position} не выполнено, исходное фото восстановлено. "
                            "Тест продолжит работу и повторит переключение автоматически: "
                            f"{self._safe_error(exc)}"
                        )
                    if cleanup_errors:
                        details += " | Очистка: " + "; ".join(cleanup_errors)
                    if test.incident_id:
                        details += f" | Инцидент {test.incident_id}"
                    test.last_error = details[:2000]
                    raise RuntimeError(details) from exc
                test.current_variant_order = next_position
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
                    "ctr": variant.ctr,
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
            "total_views": test.last_total_views,
            "total_clicks": test.last_total_clicks,
            "total_orders": int(getattr(test, "last_total_orders", 0) or 0),
            "total_spend_rub": round(test.last_total_spend_rub or 0, 2),
            "total_ctr": round(
                (int(test.last_total_clicks or 0) / int(test.last_total_views or 0)) * 100,
                4,
            ) if test.last_total_views else 0,
            "total_cpo": round(
                float(test.last_total_spend_rub or 0) / int(getattr(test, "last_total_orders", 0) or 0),
                2,
            ) if getattr(test, "last_total_orders", 0) else None,
            "last_error": test.last_error,
            "started_at": test.started_at,
            "finished_at": test.finished_at,
            "last_synced_at": test.last_synced_at,
            "created_at": test.created_at,
            "variants": variants,
        }

    async def scheduler_tick(self) -> None:
        tests = await self.repository.list_running()
        for test in tests:
            async with self._operation_lock(test.connection_id, test.nm_id):
                locked_test = await self.repository.get_for_update(test.user_id, test.id)
                if not locked_test or locked_test.status != ABTestStatus.RUNNING:
                    continue
                try:
                    await self._sync_loaded(locked_test)
                except Exception as exc:
                    logger.exception("A/B scheduler sync failed test_id=%s nm_id=%s", locked_test.id, locked_test.nm_id)
                    locked_test.last_error = locked_test.last_error or ABTestService._safe_error(exc)
                    # A short-lived stats read failure is shown to the user,
                    # but it must not stop a healthy campaign immediately. If
                    # the campaign has not produced a successful stats read
                    # for the bounded safety window, stop it and restore the
                    # card so an unavailable API cannot leave advertising
                    # running without supervision.
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
                            connection = await self._connection_or_404(
                                locked_test.user_id,
                                locked_test.connection_id,
                                require_ab_access=False,
                            )
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
                                f"Синхронизация недоступна, безопасная остановка не завершена. "
                                f"Инцидент {locked_test.incident_id}. "
                                f"{ABTestService._safe_error(safety_exc)}"
                            )[:2000]
                    await self.db.commit()
