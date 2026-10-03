from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncAttrs, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from app.core.config import settings


class Base(AsyncAttrs, DeclarativeBase):
    pass


engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# Card-operation advisory locks must not consume the application QueuePool slot
# for the whole duration of a long reconciliation/media operation. A dedicated
# NullPool gives each lock a short-lived dedicated DB connection that is closed
# when the lock is released, keeping normal API/statistics traffic isolated.
operation_lock_engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True, poolclass=NullPool)
OperationLockSessionLocal = async_sessionmaker(operation_lock_engine, class_=AsyncSession, expire_on_commit=False)

# Fullstats/advisory reservations also must not hold a normal API QueuePool
# connection while a rate-limit wait or provider request is in flight.
stats_lock_engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True, poolclass=NullPool)
StatsLockSessionLocal = async_sessionmaker(stats_lock_engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session
