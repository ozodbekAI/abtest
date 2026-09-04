from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.ab_test import ABTest, ABTestStatus
from app.models.user import User
from app.models.wb_connection import WBConnection


class AdminRepository:
    """Read and write queries scoped for the administrator service only."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_users(self, *, page: int, page_size: int, search: str = "") -> tuple[list[tuple[User, int, int]], int]:
        connection_count = (
            select(func.count(WBConnection.id))
            .where(WBConnection.user_id == User.id)
            .correlate(User)
            .scalar_subquery()
        )
        test_count = (
            select(func.count(ABTest.id))
            .where(ABTest.user_id == User.id)
            .correlate(User)
            .scalar_subquery()
        )
        query = select(User, connection_count.label("stores_count"), test_count.label("tests_count"))
        count_query = select(func.count(User.id))
        value = search.strip()
        if value:
            pattern = f"%{value}%"
            condition = or_(
                User.email.ilike(pattern),
                User.first_name.ilike(pattern),
                User.last_name.ilike(pattern),
            )
            query = query.where(condition)
            count_query = count_query.where(condition)
        total = int(await self.db.scalar(count_query) or 0)
        rows = list(
            (
                await self.db.execute(
                    query.order_by(User.created_at.desc(), User.id.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            ).all()
        )
        return [(row[0], int(row[1] or 0), int(row[2] or 0)) for row in rows], total

    async def get_user(self, user_id: int) -> User | None:
        return await self.db.scalar(select(User).where(User.id == user_id))

    async def get_user_by_email(self, email: str) -> User | None:
        return await self.db.scalar(select(User).where(User.email == email.lower().strip()))

    async def admin_count(self) -> int:
        return int(await self.db.scalar(select(func.count(User.id)).where(User.is_admin.is_(True))) or 0)

    async def list_stores(self, *, search: str = "") -> list[tuple[WBConnection, User, int]]:
        tests_count = (
            select(func.count(ABTest.id))
            .where(ABTest.connection_id == WBConnection.id)
            .correlate(WBConnection)
            .scalar_subquery()
        )
        query = select(WBConnection, User, tests_count.label("tests_count")).join(User, User.id == WBConnection.user_id)
        value = search.strip()
        if value:
            pattern = f"%{value}%"
            query = query.where(or_(WBConnection.store_name.ilike(pattern), User.email.ilike(pattern), User.first_name.ilike(pattern)))
        rows = list((await self.db.execute(query.order_by(WBConnection.created_at.desc(), WBConnection.id.desc()))).all())
        return [(row[0], row[1], int(row[2] or 0)) for row in rows]

    async def get_store(self, store_id: int) -> WBConnection | None:
        return await self.db.scalar(
            select(WBConnection)
            .options(selectinload(WBConnection.user))
            .where(WBConnection.id == store_id)
        )

    async def list_tests_for_user(self, user_id: int) -> list[ABTest]:
        result = await self.db.scalars(
            select(ABTest)
            .options(selectinload(ABTest.connection), selectinload(ABTest.variants))
            .where(ABTest.user_id == user_id)
            .order_by(ABTest.created_at.desc(), ABTest.id.desc())
        )
        return list(result.all())

    async def list_tests_for_store(self, store_id: int) -> list[ABTest]:
        result = await self.db.scalars(
            select(ABTest)
            .options(selectinload(ABTest.connection), selectinload(ABTest.variants))
            .where(ABTest.connection_id == store_id)
            .order_by(ABTest.created_at.desc(), ABTest.id.desc())
        )
        return list(result.all())

    async def dashboard(self) -> dict:
        today = date.today()
        begin = today - timedelta(days=29)
        user_count = int(await self.db.scalar(select(func.count(User.id))) or 0)
        active_user_count = int(await self.db.scalar(select(func.count(User.id)).where(User.is_active.is_(True))) or 0)
        verified_user_count = int(await self.db.scalar(select(func.count(User.id)).where(User.is_verified.is_(True))) or 0)
        store_count = int(await self.db.scalar(select(func.count(WBConnection.id))) or 0)
        test_count = int(await self.db.scalar(select(func.count(ABTest.id))) or 0)
        running_test_count = int(
            await self.db.scalar(select(func.count(ABTest.id)).where(ABTest.status == ABTestStatus.RUNNING)) or 0
        )
        finished_test_count = int(
            await self.db.scalar(select(func.count(ABTest.id)).where(ABTest.status == ABTestStatus.FINISHED)) or 0
        )
        failed_test_count = int(
            await self.db.scalar(
                select(func.count(ABTest.id)).where(ABTest.status.in_([ABTestStatus.FAILED, ABTestStatus.STOPPED]))
            )
            or 0
        )
        totals = (
            await self.db.execute(
                select(
                    func.coalesce(func.sum(ABTest.last_total_views), 0),
                    func.coalesce(func.sum(ABTest.last_total_clicks), 0),
                    func.coalesce(func.sum(ABTest.last_total_orders), 0),
                    func.coalesce(func.sum(ABTest.last_total_spend_rub), 0),
                )
            )
        ).one()
        users = list((await self.db.scalars(select(User.created_at).where(User.created_at >= begin))).all())
        registrations = []
        for offset in range(30):
            day = begin + timedelta(days=offset)
            registrations.append({"date": day, "count": sum(1 for created_at in users if created_at and created_at.date() == day)})
        return {
            "users_count": user_count,
            "active_users_count": active_user_count,
            "verified_users_count": verified_user_count,
            "stores_count": store_count,
            "tests_count": test_count,
            "running_tests_count": running_test_count,
            "finished_tests_count": finished_test_count,
            "failed_tests_count": failed_test_count,
            "views": int(totals[0] or 0),
            "clicks": int(totals[1] or 0),
            "orders": int(totals[2] or 0),
            "spend_rub": round(float(totals[3] or 0), 2),
            "registrations": registrations,
        }

    async def delete_user(self, user: User) -> None:
        await self.db.delete(user)
