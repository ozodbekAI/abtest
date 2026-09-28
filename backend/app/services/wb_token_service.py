from base64 import urlsafe_b64encode, urlsafe_b64decode
from datetime import datetime, timezone
import hashlib
import asyncio
import hmac
import json
from typing import Any

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.wb_connection import WBConnection
from app.models.ab_test import ABTestStatus
from app.services.wb_content_client import WBContentClient
from app.services.wb_promotion_client import WBPromotionClient
from app.repositories.wb_repository import WBRepository


class WBTokenService:
    # A/B-tests mutate card media and run promotion campaigns. Analytics and
    # statistics are optional seller permissions and must not block or slow
    # down token validation for this product.
    REQUIRED_CATEGORIES = ("content", "promotion")
    CATEGORIES = {
        "content": settings.wb_content_api_url,
        "promotion": settings.wb_advert_api_url,
        "analytics": settings.wb_analytics_api_url,
        "statistics": settings.wb_statistics_api_url,
    }

    def __init__(self, db: AsyncSession):
        self.db = db
        self.repository = WBRepository(db)

    @staticmethod
    def _cipher() -> Fernet:
        key = settings.fernet_key.strip()
        if not key:
            key = urlsafe_b64encode(hashlib.sha256(settings.jwt_secret_key.encode()).digest()).decode()
        try:
            return Fernet(key.encode())
        except (ValueError, TypeError) as exc:
            raise RuntimeError("FERNET_KEY must be a valid Fernet key") from exc

    @classmethod
    def encrypt_token(cls, token: str) -> str:
        return cls._cipher().encrypt(token.encode()).decode()

    @classmethod
    def decrypt_token(cls, encrypted: str) -> str:
        try:
            return cls._cipher().decrypt(encrypted.encode()).decode()
        except InvalidToken as exc:
            raise RuntimeError("Unable to decrypt WB token") from exc

    async def validate(self, token: str) -> dict[str, Any]:
        token = token.strip()
        # Wildberries uses the HeaderApiKey scheme.  The API key is sent as-is;
        # adding ``Bearer`` makes an otherwise valid seller token fail with 401.
        headers = {"Authorization": token, "Accept": "application/json"}
        timeout = httpx.Timeout(settings.wb_request_timeout)

        async def ping(client: httpx.AsyncClient, category: str, base_url: str) -> tuple[str, dict[str, Any]]:
            url = f"{base_url.rstrip('/')}/ping"
            try:
                response = await client.get(url, headers=headers)
                allowed = 200 <= response.status_code < 300
                if allowed:
                    return category, {"status": "allowed", "http_status": response.status_code, "message": "Доступ подтверждён"}
                if response.status_code == 403:
                    return category, {"status": "denied", "http_status": 403, "message": "У токена нет доступа к этой категории"}
                if response.status_code == 401:
                    return category, {"status": "invalid", "http_status": 401, "message": "Токен недействителен или просрочен"}
                return category, {"status": "unavailable", "http_status": response.status_code, "message": "WB вернул неожиданный ответ"}
            except httpx.TimeoutException:
                return category, {"status": "timeout", "http_status": None, "message": "WB не ответил вовремя"}
            except httpx.HTTPError:
                return category, {"status": "unavailable", "http_status": None, "message": "Не удалось связаться с WB"}

        results: dict[str, dict[str, Any]] = {}
        async with httpx.AsyncClient(timeout=timeout) as client:
            pairs = await asyncio.gather(
                *(
                    ping(client, category, self.CATEGORIES[category])
                    for category in self.REQUIRED_CATEGORIES
                )
            )
            results = dict(pairs)

            # A category ping does not prove write access. Decode documented
            # permission metadata only alongside remote token authentication.
            claims = self.token_claims(token)
            permissions = claims.get("s")
            write_access = (isinstance(permissions, int) and not isinstance(permissions, bool)
                            and permissions >= 0 and not bool(permissions & (1 << 30)))
            seller_id = None
            if any(item.get("status") == "allowed" for item in results.values()):
                try:
                    response = await client.get(f"{settings.wb_common_api_url.rstrip('/')}/api/v1/seller-info", headers=headers)
                    response.raise_for_status()
                    seller = response.json()
                    seller_id = str(seller.get("sid") or "").strip() if isinstance(seller, dict) else ""
                    if not seller_id or len(seller_id) > 128:
                        raise ValueError("missing seller identity")
                    if claims.get("sid") and str(claims["sid"]) != seller_id:
                        raise ValueError("seller identity mismatch")
                except (httpx.HTTPError, ValueError, TypeError) as exc:
                    raise HTTPException(status_code=503, detail="Не удалось подтвердить кабинет WB через seller-info; внешние изменения запрещены") from exc

        access = {category: result.get("status") == "allowed" for category, result in results.items()}
        reachable = any(result.get("status") in {"allowed", "denied", "invalid"} for result in results.values())
        if not reachable:
            raise HTTPException(status_code=503, detail={"message": "WB API временно недоступен", "pings": results})
        if not any(result.get("status") == "allowed" for result in results.values()) and all(
            result.get("status") == "invalid" for result in results.values()
        ):
            raise HTTPException(status_code=401, detail="Токен WB недействителен или просрочен")

        results["write"] = {
            "status": "allowed" if write_access else "denied", "http_status": None,
            "message": "Разрешена запись" if write_access else "Нужен токен WB с правами чтения и записи (не только чтения)",
        }
        ready = bool(access.get("content") and access.get("promotion") and write_access and seller_id)
        return {
            "access": access,
            "pings": results,
            "write_access": write_access,
            "seller_id": seller_id,
            "ready_for_ab_tests": ready,
            "status": "ready" if ready else "partial",
        }

    @staticmethod
    def token_claims(token: str) -> dict[str, Any]:
        """Decode permission metadata. This does not authenticate the token."""
        try:
            parts = token.strip().split(".")
            if len(parts) != 3:
                return {}
            payload = parts[1]
            claims = json.loads(urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            return claims if isinstance(claims, dict) else {}
        except (ValueError, UnicodeError, TypeError):
            return {}

    @staticmethod
    def store_fingerprint(connection: WBConnection) -> str:
        seller_id = str(getattr(connection, "seller_id", None) or "").strip()
        if not seller_id:
            raise HTTPException(status_code=409, detail="Кабинет WB ещё не подтверждён. Проверьте токен перед запуском.")
        return hashlib.sha256(f"wb-seller:{seller_id}".encode()).hexdigest()

    async def ensure_seller_identity(self, connection: WBConnection) -> str:
        """Verify legacy connections before acquiring a seller-scoped lock."""
        if not getattr(connection, "seller_id", None):
            result = await self.validate(self.decrypt_token(connection.token_encrypted))
            self._apply_validation(connection, result)
            await self.db.flush()
        return self.store_fingerprint(connection)

    @staticmethod
    def _apply_validation(connection: WBConnection, result: dict[str, Any]) -> None:
        old_seller_id = getattr(connection, "seller_id", None)
        if old_seller_id and result.get("seller_id") and old_seller_id != result["seller_id"]:
            raise HTTPException(status_code=409, detail="Токен принадлежит другому кабинету WB")
        connection.seller_id = result.get("seller_id")
        connection.write_access = bool(result.get("write_access"))
        connection.status = result["status"]
        for category in WBTokenService.CATEGORIES:
            setattr(connection, f"{category}_access", result["access"].get(category, False))
        connection.ready_for_ab_tests = result["ready_for_ab_tests"]
        connection.ping_results = result["pings"]
        connection.last_validated_at = datetime.now(timezone.utc)

    @staticmethod
    def response(connection: WBConnection | None) -> dict[str, Any]:
        if not connection:
            return {
                "id": 0,
                "store_name": "",
                "connected": False,
                "status": "not_connected",
                "token_last4": "",
                "ready_for_ab_tests": False,
                "write_access": False,
                "seller_id": None,
                "access": {category: False for category in WBTokenService.CATEGORIES},
                "pings": {},
                "last_validated_at": None,
            }
        return {
            "id": connection.id,
            "store_name": connection.store_name,
            "connected": True,
            "status": connection.status,
            "token_last4": connection.token_last4,
            "ready_for_ab_tests": connection.ready_for_ab_tests,
            "write_access": bool(getattr(connection, "write_access", False)),
            "seller_id": getattr(connection, "seller_id", None),
            "access": {
                "content": connection.content_access,
                "promotion": connection.promotion_access,
                "analytics": connection.analytics_access,
                "statistics": connection.statistics_access,
            },
            "pings": connection.ping_results or {},
            "last_validated_at": connection.last_validated_at,
        }

    async def connect(self, user_id: int, token: str, store_name: str = "", require_ab_test_access: bool = False) -> dict[str, Any]:
        token = token.strip()
        result = await self.validate(token)
        if require_ab_test_access and not result["ready_for_ab_tests"]:
            required_categories = {"content": "«Контент»", "promotion": "«Продвижение»"}
            missing = ", ".join(
                label for category, label in required_categories.items() if not result["access"].get(category)
            )
            if not result.get("write_access"):
                missing = f"{missing}, право записи".strip(", ")
            raise HTTPException(
                status_code=403,
                detail={
                    "message": (
                        "Для A/B-тестов токен должен иметь доступ к категориям "
                        "«Контент» и «Продвижение». "
                        f"Недостающий доступ: {missing}."
                    ),
                    "access": result["access"],
                    "pings": result["pings"],
                },
            )
        connections = await self.repository.list_for_user(user_id)
        for existing in connections:
            if result.get("seller_id") and getattr(existing, "seller_id", None) == result["seller_id"]:
                raise HTTPException(status_code=409, detail={"code": "connection_already_exists", "message": "Этот кабинет WB уже подключён. Замените токен существующего подключения.", "connection_id": existing.id})
            try:
                existing_token = self.decrypt_token(existing.token_encrypted)
            except RuntimeError:
                # Do not let one legacy/corrupt record prevent a new valid
                # connection from being added; its own validation still runs.
                continue
            if hmac.compare_digest(existing_token, token):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "connection_already_exists",
                        "message": "Этот токен Wildberries уже подключён в выбранном пространстве.",
                        "connection_id": existing.id,
                    },
                )
        store_name = store_name.strip() or f"Магазин {len(connections) + 1}"
        values = {
            "store_name": store_name,
            "token_encrypted": self.encrypt_token(token),
            "token_last4": token[-4:],
            # Xuddi shu WB tokeni (demak, xuddi shu do'kon) boshqa foydalanuvchi
            # tomonidan ulangan bo'lsa ham aniqlash uchun. Shifrlash (Fernet)
            # har safar boshqacha natija beradi, shuning uchun taqqoslash
            # uchun tekis (deterministik) SHA-256 barmoq izi saqlanadi.
            "token_fingerprint": hashlib.sha256(token.encode()).hexdigest(),
            "seller_id": result.get("seller_id"),
            "write_access": bool(result.get("write_access")),
            "status": result["status"],
            "content_access": result["access"].get("content", False),
            "promotion_access": result["access"].get("promotion", False),
            "analytics_access": result["access"].get("analytics", False),
            "statistics_access": result["access"].get("statistics", False),
            "ready_for_ab_tests": result["ready_for_ab_tests"],
            "ping_results": result["pings"],
            "last_validated_at": datetime.now(timezone.utc),
        }
        connection = await self.repository.create(user_id=user_id, **values)
        await self.db.commit()
        await self.db.refresh(connection)
        return self.response(connection)

    async def rotate_token(self, user_id: int, connection_id: int, token: str) -> dict[str, Any]:
        token = token.strip()
        result = await self.validate(token)
        if not result["ready_for_ab_tests"]:
            raise HTTPException(status_code=403, detail={
                "message": "Новый токен должен иметь доступ к категориям «Контент» и «Продвижение».",
                "access": result["access"],
                "pings": result["pings"],
            })
        connection = await self.repository.get_for_user(user_id, connection_id)
        if not connection:
            raise HTTPException(status_code=404, detail="Магазин Wildberries не найден")
        new_fingerprint = hashlib.sha256(token.encode()).hexdigest()
        old_seller_id = getattr(connection, "seller_id", None) or self.token_claims(self.decrypt_token(connection.token_encrypted)).get("sid")
        if not old_seller_id or str(old_seller_id) != result.get("seller_id"):
            raise HTTPException(status_code=409, detail="Новый токен должен принадлежать тому же кабинету WB; смена магазина при замене токена запрещена")
        from app.repositories.ab_test_repository import ABTestRepository
        tests = await ABTestRepository(self.db).list_for_user(user_id, connection_id=connection.id)
        unsafe = [
            test for test in tests
            if test.status == ABTestStatus.RUNNING
            or test.operation_state == "reconciliation_required"
            or test.campaign_state in {"running", "starting", "pause_requested", "stop_requested", "unknown"}
            or test.media_status in {"variant_applied", "swapping", "restoring", "waiting_image_reupload"}
            or bool((test.media_state or {}).get("pending"))
        ]
        if unsafe:
            content = WBContentClient(token)
            promotion = WBPromotionClient(token)
            for test in unsafe:
                try:
                    card = await content.get_card(test.nm_id)
                    if not card:
                        raise RuntimeError(f"nmID {test.nm_id} недоступен через новый токен")
                    if test.wb_campaign_id:
                        status = await promotion.get_campaign_status(test.wb_campaign_id, refresh=True)
                        if status is None:
                            raise RuntimeError(f"Кампания #{int(test.wb_campaign_id)} недоступна через новый токен")
                except Exception as exc:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Новый токен не прошёл recovery-проверку для активных A/B-тестов. "
                            "Сначала выполните сверку текущих кампаний/фото."
                        ),
                    ) from exc
        connection.token_encrypted = self.encrypt_token(token)
        connection.token_last4 = token[-4:]
        connection.token_fingerprint = new_fingerprint
        self._apply_validation(connection, result)
        connection.status = result["status"]
        connection.content_access = result["access"].get("content", False)
        connection.promotion_access = result["access"].get("promotion", False)
        connection.analytics_access = result["access"].get("analytics", False)
        connection.statistics_access = result["access"].get("statistics", False)
        connection.ready_for_ab_tests = result["ready_for_ab_tests"]
        connection.ping_results = result["pings"]
        connection.last_validated_at = datetime.now(timezone.utc)
        # Keep the same lifecycle lock attached to this connection. The active
        # experiment therefore continues to use one seller-scope key after token rotation.
        for test in tests:
            if test.store_fingerprint and (test.store_fingerprint == connection.token_fingerprint or test.connection_id == connection.id):
                test.store_fingerprint = self.store_fingerprint(connection)
        await self.db.commit()
        await self.db.refresh(connection)
        return self.response(connection)

    async def validate_saved(self, user_id: int, connection_id: int | None = None) -> dict[str, Any]:
        connection = await self.repository.get_for_user(user_id, connection_id)
        if not connection:
            raise HTTPException(status_code=404, detail="Токен WB не подключён")
        token = self.decrypt_token(connection.token_encrypted)
        connection.token_fingerprint = hashlib.sha256(token.strip().encode()).hexdigest()
        result = await self.validate(token)
        connection.status = result["status"]
        self._apply_validation(connection, result)
        connection.content_access = result["access"].get("content", False)
        connection.promotion_access = result["access"].get("promotion", False)
        connection.analytics_access = result["access"].get("analytics", False)
        connection.statistics_access = result["access"].get("statistics", False)
        connection.ready_for_ab_tests = result["ready_for_ab_tests"]
        connection.ping_results = result["pings"]
        connection.last_validated_at = datetime.now(timezone.utc)
        await self.db.commit()
        await self.db.refresh(connection)
        return self.response(connection)
