from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.ab_test import ABTest, ABTestOperation, ABTestStatus, ABTestVariant
from app.models.wb_connection import WBConnection


class ABTestRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_connection(self, user_id: int, connection_id: int) -> WBConnection | None:
        return await self.db.scalar(
            select(WBConnection).where(WBConnection.id == connection_id, WBConnection.user_id == user_id)
        )

    async def get_for_user(self, user_id: int, test_id: int) -> ABTest | None:
        return await self.db.scalar(
            select(ABTest)
            .options(selectinload(ABTest.variants), selectinload(ABTest.connection))
            .where(ABTest.id == test_id, ABTest.user_id == user_id)
        )

    async def get_for_update(self, user_id: int, test_id: int) -> ABTest | None:
        """Load a test with a row lock for state-changing A/B operations."""
        query = (
            select(ABTest)
            .options(selectinload(ABTest.variants), selectinload(ABTest.connection))
            .where(ABTest.id == test_id, ABTest.user_id == user_id)
            .with_for_update(of=ABTest)
        )
        return await self.db.scalar(query)

    async def list_for_user(
        self, user_id: int, *, connection_id: int | None = None, status: ABTestStatus | None = None
    ) -> list[ABTest]:
        query = (
            select(ABTest)
            .options(selectinload(ABTest.variants), selectinload(ABTest.connection))
            .where(ABTest.user_id == user_id)
            .order_by(ABTest.created_at.desc(), ABTest.id.desc())
        )
        if connection_id is not None:
            query = query.where(ABTest.connection_id == connection_id)
        if status is not None:
            query = query.where(ABTest.status == status)
        return list((await self.db.scalars(query)).all())

    async def list_running(self) -> list[ABTest]:
        query = (
            select(ABTest)
            .options(selectinload(ABTest.variants), selectinload(ABTest.connection))
            .where(ABTest.status == ABTestStatus.RUNNING)
            .order_by(ABTest.id)
        )
        return list((await self.db.scalars(query)).all())

    async def get_running_for_card(self, connection_id: int, nm_id: int, *, exclude_test_id: int | None = None) -> ABTest | None:
        query = select(ABTest).where(
            ABTest.connection_id == connection_id,
            ABTest.nm_id == nm_id,
            ABTest.status == ABTestStatus.RUNNING,
        )
        if exclude_test_id is not None:
            query = query.where(ABTest.id != exclude_test_id)
        return await self.db.scalar(query)

    async def get_operation(self, operation_key: str) -> ABTestOperation | None:
        return await self.db.scalar(
            select(ABTestOperation).where(ABTestOperation.operation_key == operation_key)
        )

    async def get_latest_operation(self, test_id: int) -> ABTestOperation | None:
        return await self.db.scalar(
            select(ABTestOperation)
            .where(ABTestOperation.test_id == test_id)
            .order_by(ABTestOperation.id.desc())
        )

    async def create_operation(self, **values) -> ABTestOperation:
        operation = ABTestOperation(**values)
        self.db.add(operation)
        await self.db.flush()
        return operation

    async def list_reconciliation_operations(self) -> list[ABTestOperation]:
        result = await self.db.scalars(
            select(ABTestOperation)
            .where(ABTestOperation.status.in_(("PREPARED", "IN_PROGRESS", "RECONCILIATION_REQUIRED")))
            .order_by(ABTestOperation.id)
        )
        return list(result.all())

    async def count_for_user(self, user_id: int) -> int:
        return int(await self.db.scalar(select(func.count(ABTest.id)).where(ABTest.user_id == user_id)) or 0)

    async def create(self, **values) -> ABTest:
        test = ABTest(**values)
        self.db.add(test)
        await self.db.flush()
        return test

    async def add_variant(self, **values) -> ABTestVariant:
        variant = ABTestVariant(**values)
        self.db.add(variant)
        await self.db.flush()
        return variant

    async def get_variant(self, test_id: int, position: int) -> ABTestVariant | None:
        return await self.db.scalar(
            select(ABTestVariant).where(ABTestVariant.test_id == test_id, ABTestVariant.position == position)
        )

    async def delete_variant(self, variant: ABTestVariant) -> None:
        await self.db.delete(variant)
