from base64 import urlsafe_b64encode
from datetime import datetime, timezone
import hashlib
import asyncio
from typing import Any

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.wb_connection import WBConnection
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
            except httpx.HTTPError as exc:
                return category, {"status": "unavailable", "http_status": None, "message": str(exc)}

        results: dict[str, dict[str, Any]] = {}
        async with httpx.AsyncClient(timeout=timeout) as client:
            pairs = await asyncio.gather(
                *(
                    ping(client, category, self.CATEGORIES[category])
                    for category in self.REQUIRED_CATEGORIES
                )
            )
            results = dict(pairs)

        access = {category: result.get("status") == "allowed" for category, result in results.items()}
        reachable = any(result.get("status") in {"allowed", "denied", "invalid"} for result in results.values())
        if not reachable:
            raise HTTPException(status_code=503, detail={"message": "WB API временно недоступен", "pings": results})
        if not any(result.get("status") == "allowed" for result in results.values()) and all(
            result.get("status") == "invalid" for result in results.values()
        ):
            raise HTTPException(status_code=401, detail="Токен WB недействителен или просрочен")

        return {
            "access": access,
            "pings": results,
            "ready_for_ab_tests": bool(access.get("content") and access.get("promotion")),
            "status": "ready" if access.get("content") and access.get("promotion") else "partial",
        }

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
        store_name = store_name.strip() or f"Магазин {len(connections) + 1}"
        values = {
            "store_name": store_name,
            "token_encrypted": self.encrypt_token(token),
            "token_last4": token[-4:],
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

    async def validate_saved(self, user_id: int, connection_id: int | None = None) -> dict[str, Any]:
        connection = await self.repository.get_for_user(user_id, connection_id)
        if not connection:
            raise HTTPException(status_code=404, detail="Токен WB не подключён")
        token = self.decrypt_token(connection.token_encrypted)
        result = await self.validate(token)
        connection.status = result["status"]
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
