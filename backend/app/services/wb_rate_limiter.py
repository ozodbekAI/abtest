from __future__ import annotations

from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import asyncio
from typing import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal


class WBRateLimiter:
    """Serialize seller-scoped WB mutations and fullstats requests."""

    _locks: defaultdict[tuple[int, int, int], asyncio.Lock] = defaultdict(asyncio.Lock)
    _locks_guard = asyncio.Lock()
    FULLSTATS_INTERVAL_SECONDS = 20.2

    @classmethod
    async def _lock_for(cls, connection_id: int, resource_id: int, seller_id: int | None = None) -> asyncio.Lock:
        async with cls._locks_guard:
            return cls._locks[(int(seller_id or 0), int(connection_id), int(resource_id))]

    @classmethod
    @asynccontextmanager
    async def operation_lock(
        cls,
        db: AsyncSession,
        connection_id: int,
        nm_id: int,
        seller_id: int | None = None,
    ) -> AsyncIterator[None]:
        lock = await cls._lock_for(connection_id, nm_id, seller_id)
        async with lock:
            lock_db: AsyncSession | None = None
            lock_scope = int(seller_id or connection_id)
            lock_key = f"wb-ab-test:{lock_scope}:{int(nm_id)}"
            try:
                bind = db.get_bind()
                if bind.dialect.name == "postgresql":
                    # The service commits several durable checkpoints while
                    # one WB operation is in flight. An xact lock on ``db``
                    # would be released by the first checkpoint and another
                    # worker could then mutate the same card concurrently.
                    # Keep a dedicated DB session open and use a session-level
                    # advisory lock for the whole context instead.
                    lock_db = AsyncSessionLocal()
                    await lock_db.execute(
                        text("SELECT pg_advisory_lock(hashtext(:lock_key))"),
                        {"lock_key": lock_key},
                    )
            except (AttributeError, RuntimeError):
                # Lightweight unit-test sessions may not expose a bind.
                if lock_db is not None:
                    await lock_db.close()
                    lock_db = None
            try:
                yield
            finally:
                if lock_db is not None:
                    try:
                        await lock_db.execute(
                            text("SELECT pg_advisory_unlock(hashtext(:lock_key))"),
                            {"lock_key": lock_key},
                        )
                    finally:
                        await lock_db.close()

    @classmethod
    @asynccontextmanager
    async def stats_lock(cls, db: AsyncSession, connection_id: int) -> AsyncIterator[None]:
        """Reserve a fullstats slot across every worker process."""
        lock = await cls._lock_for(connection_id, 0)
        async with lock:
            wait_for = 0.0
            try:
                bind = db.get_bind()
                if bind.dialect.name == "postgresql":
                    # Keep the caller's transaction lock alive while the
                    # network request runs. Only the short reservation uses a
                    # separate transaction because committing db here would
                    # release the A/B row/advisory lock.
                    await db.execute(
                        text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
                        {"lock_key": f"wb-fullstats:{int(connection_id)}"},
                    )
                    scope = f"fullstats:{int(connection_id)}"
                    async with AsyncSessionLocal() as rate_db:
                        await rate_db.execute(
                            text(
                                """
                                INSERT INTO wb_api_rate_limits (scope, next_allowed_at)
                                VALUES (:scope, clock_timestamp())
                                ON CONFLICT (scope) DO NOTHING
                                """
                            ),
                            {"scope": scope},
                        )
                        next_allowed_at = await rate_db.scalar(
                            text(
                                """
                                SELECT next_allowed_at
                                FROM wb_api_rate_limits
                                WHERE scope = :scope
                                FOR UPDATE
                                """
                            ),
                            {"scope": scope},
                        )
                        now = datetime.now(timezone.utc)
                        if next_allowed_at is not None:
                            if next_allowed_at.tzinfo is None:
                                next_allowed_at = next_allowed_at.replace(tzinfo=timezone.utc)
                            wait_for = max((next_allowed_at - now).total_seconds(), 0.0)
                        reservation_base = now + timedelta(seconds=wait_for)
                        await rate_db.execute(
                            text(
                                """
                                UPDATE wb_api_rate_limits
                                SET next_allowed_at = :next_allowed_at
                                WHERE scope = :scope
                                """
                            ),
                            {
                                "scope": scope,
                                "next_allowed_at": reservation_base + timedelta(seconds=cls.FULLSTATS_INTERVAL_SECONDS),
                            },
                        )
                        await rate_db.commit()
            except (AttributeError, RuntimeError):
                pass
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            yield
