from __future__ import annotations

from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.ab_test import ABTestCreateRequest, ABTestStartRequest
from app.services.ab_test_service import ABTestService


class ABTestController:
    async def cards(
        self,
        db: AsyncSession,
        user_id: int,
        connection_id: int,
        search: str,
        cursor_updated_at: str | None = None,
        cursor_nm_id: int | None = None,
    ):
        return await ABTestService(db).cards(
            user_id,
            connection_id,
            search,
            cursor_updated_at=cursor_updated_at,
            cursor_nm_id=cursor_nm_id,
        )

    async def card(self, db: AsyncSession, user_id: int, connection_id: int, nm_id: int):
        return await ABTestService(db).card(user_id, connection_id, nm_id)

    async def create(self, db: AsyncSession, user_id: int, request: ABTestCreateRequest):
        return await ABTestService(db).create(user_id, request)

    async def upload_variant(self, db: AsyncSession, user_id: int, test_id: int, position: int, file: UploadFile):
        return await ABTestService(db).upload_variant(user_id, test_id, position, file)

    async def preview_image(self, db: AsyncSession, user_id: int, file: UploadFile):
        return await ABTestService(db).preview_image(user_id, file)

    async def set_variant_source(self, db: AsyncSession, user_id: int, test_id: int, position: int, source_url: str):
        return await ABTestService(db).set_variant_source(user_id, test_id, position, source_url)

    async def delete_variant(self, db: AsyncSession, user_id: int, test_id: int, position: int):
        return await ABTestService(db).delete_variant(user_id, test_id, position)

    async def delete(self, db: AsyncSession, user_id: int, test_id: int) -> None:
        await ABTestService(db).delete(user_id, test_id)

    async def list(self, db: AsyncSession, user_id: int, connection_id: int | None, status_value):
        return await ABTestService(db).list(user_id, connection_id, status_value)

    async def get(self, db: AsyncSession, user_id: int, test_id: int):
        service = ABTestService(db)
        return await service.get(user_id, test_id)

    async def start(
        self,
        db: AsyncSession,
        user_id: int,
        test_id: int,
        request: ABTestStartRequest,
        idempotency_key: str | None = None,
    ):
        return await ABTestService(db).start(user_id, test_id, request, idempotency_key=idempotency_key)

    async def stop(self, db: AsyncSession, user_id: int, test_id: int):
        return await ABTestService(db).stop(user_id, test_id)

    async def sync(self, db: AsyncSession, user_id: int, test_id: int):
        return await ABTestService(db).sync(user_id, test_id)

    async def reconcile(self, db: AsyncSession, user_id: int, test_id: int):
        return await ABTestService(db).reconcile(user_id, test_id)
