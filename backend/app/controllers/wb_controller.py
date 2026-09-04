from sqlalchemy.ext.asyncio import AsyncSession

from fastapi import HTTPException
from app.models.ab_test import ABTestStatus
from app.repositories.ab_test_repository import ABTestRepository
from app.services.dashboard_service import DashboardService
from app.services.wb_promotion_client import WBPromotionClient
from app.services.wb_token_service import WBTokenService


class WBController:
    async def get_connection(self, db: AsyncSession, user_id: int, connection_id: int | None = None) -> dict:
        connection = await WBTokenService(db).repository.get_for_user(user_id, connection_id)
        return WBTokenService.response(connection)

    async def get_connections(self, db: AsyncSession, user_id: int) -> list[dict]:
        connections = await WBTokenService(db).repository.list_for_user(user_id)
        return [WBTokenService.response(connection) for connection in connections]

    async def connect(self, db: AsyncSession, user_id: int, token: str, store_name: str = "", require_ab_test_access: bool = False) -> dict:
        return await WBTokenService(db).connect(user_id, token, store_name=store_name, require_ab_test_access=require_ab_test_access)

    async def validate(self, db: AsyncSession, user_id: int, connection_id: int | None = None) -> dict:
        return await WBTokenService(db).validate_saved(user_id, connection_id)

    async def disconnect(self, db: AsyncSession, user_id: int, connection_id: int | None = None) -> None:
        service = WBTokenService(db)
        connection = await service.repository.get_for_user(user_id, connection_id)
        if connection:
            tests = await ABTestRepository(db).list_for_user(user_id, connection_id=connection.id)
            unsafe_tests = [
                test
                for test in tests
                if test.status == ABTestStatus.RUNNING
                or test.operation_state == "reconciliation_required"
                or test.campaign_state in {"running", "starting", "stop_requested", "unknown"}
                or test.media_status in {"variant_applied", "swapping", "restoring"}
                or bool((test.media_state or {}).get("pending"))
            ]
            if unsafe_tests:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Нельзя отключить магазин: сначала завершите или сверите A/B-тесты "
                        f"({', '.join(str(test.id) for test in unsafe_tests[:5])})."
                    ),
                )
        await service.repository.delete_for_user(user_id, connection_id)
        await db.commit()

    async def dashboard_stats(self, db: AsyncSession, user_id: int, connection_id: int, **kwargs):
        return await DashboardService(db).stats(user_id, connection_id, **kwargs)

    async def promotion_balance(self, db: AsyncSession, user_id: int, connection_id: int) -> dict:
        service = WBTokenService(db)
        connection = await service.repository.get_for_user(user_id, connection_id)
        if not connection:
            raise HTTPException(status_code=404, detail="Магазин Wildberries не найден")
        if not connection.promotion_access:
            raise HTTPException(status_code=403, detail="У токена нет доступа к категории «Продвижение»")

        try:
            token = service.decrypt_token(connection.token_encrypted)
            payload = await WBPromotionClient(token).get_balance()
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail="Не удалось получить баланс продвижения Wildberries") from exc

        from datetime import datetime, timezone

        return {
            "connection_id": connection.id,
            **WBPromotionClient.normalize_balance(payload),
            "fetched_at": datetime.now(timezone.utc),
        }
