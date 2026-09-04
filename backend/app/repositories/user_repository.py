from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


class UserRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_by_id(self, user_id: int) -> User | None:
        return await self.db.scalar(select(User).where(User.id == user_id))

    async def get_by_email(self, email: str, *, for_update: bool = False) -> User | None:
        query = select(User).where(User.email == email.lower().strip())
        if for_update:
            query = query.with_for_update()
        return await self.db.scalar(query)

    async def create(self, **values) -> User:
        user = User(**values)
        self.db.add(user)
        await self.db.flush()
        return user
