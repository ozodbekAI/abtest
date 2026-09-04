from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.wb_connection import WBConnection


class WBRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_for_user(self, user_id: int) -> list[WBConnection]:
        result = await self.db.scalars(
            select(WBConnection).where(WBConnection.user_id == user_id).order_by(WBConnection.created_at, WBConnection.id)
        )
        return list(result.all())

    async def get_for_user(self, user_id: int, connection_id: int | None = None) -> WBConnection | None:
        query = select(WBConnection).where(WBConnection.user_id == user_id)
        if connection_id is not None:
            query = query.where(WBConnection.id == connection_id)
        query = query.order_by(WBConnection.created_at, WBConnection.id)
        return await self.db.scalar(query)

    async def create(self, **values) -> WBConnection:
        connection = WBConnection(**values)
        self.db.add(connection)
        await self.db.flush()
        return connection

    async def delete_for_user(self, user_id: int, connection_id: int | None = None) -> None:
        connection = await self.get_for_user(user_id, connection_id)
        if connection:
            await self.db.delete(connection)
