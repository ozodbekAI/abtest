from __future__ import annotations

from collections import defaultdict
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import asyncio
from typing import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal


class WBRateLimiter:
    """Serialize seller-scoped WB mutations and fullstats requests."""

    _locks: defaultdict[tuple[str, int], asyncio.Lock] = defaultdict(asyncio.Lock)
    _locks_guard = asyncio.Lock()
    FULLSTATS_INTERVAL_SECONDS = 20.2
    _operation_owner: ContextVar[tuple[AsyncSession, int, str] | None] = ContextVar("wb_operation_owner", default=None)

    @classmethod
    async def assert_lock_owned(cls) -> None:
        """Fence a worker whose dedicated advisory-lock connection was lost."""
        owner = cls._operation_owner.get()
        if owner is None:
            return
        lock_db, backend_pid, lock_key = owner
        result = await lock_db.execute(
            text("""
                SELECT pg_backend_pid() = :pid AND EXISTS (
                    SELECT 1 FROM pg_locks
                    WHERE pid = :pid AND locktype = 'advisory' AND granted
                      AND classid = ((hashtext(:lock_key)::bigint >> 32) & 4294967295)::oid
                      AND objid = (hashtext(:lock_key)::bigint & 4294967295)::oid
                      AND objsubid = 1
                )
            """), {"pid": backend_pid, "lock_key": lock_key},
        )
        if not result.scalar():
            raise RuntimeError("WB operation lock was lost; external mutation is fenced")

    @classmethod
    async def _lock_for(cls, scope_key: str, resource_id: int) -> asyncio.Lock:
        async with cls._locks_guard:
            return cls._locks[(str(scope_key), int(resource_id))]

    @classmethod
    @asynccontextmanager
    async def operation_lock(
        cls,
        db: AsyncSession,
        connection_id: int,
        nm_id: int,
        scope_key: str | int | None = None,
        wait: bool = True,
    ) -> AsyncIterator[bool]:
        scope = str(scope_key or connection_id)
        lock = await cls._lock_for(scope, nm_id)
        if not wait and lock.locked():
            yield False
            return
        async with lock:
            lock_db: AsyncSession | None = None
            lock_key = f"wb-ab-test:{scope}:{int(nm_id)}"
            owner_token = None
            acquired = False
            try:
                try:
                    bind = db.get_bind()
                except (AttributeError, RuntimeError):
                    bind = None  # Lightweight deterministic unit-test sessions.
                if bind is not None and bind.dialect.name == "postgresql":
                    # The service commits several durable checkpoints while
                    # one WB operation is in flight. An xact lock on ``db``
                    # would be released by the first checkpoint and another
                    # worker could then mutate the same card concurrently.
                    # Keep a dedicated DB session open and use a session-level
                    # advisory lock for the whole context instead.
                    lock_db = AsyncSessionLocal()
                    result = await lock_db.execute(
                        text("SELECT pg_advisory_lock(hashtext(:lock_key))" if wait else "SELECT pg_try_advisory_lock(hashtext(:lock_key))"),
                        {"lock_key": lock_key},
                    )
                    acquired = wait or bool(result.scalar())
                    if not acquired:
                        yield False
                        return
                    backend_pid = int(await lock_db.scalar(text("SELECT pg_backend_pid()")))
                    owner_token = cls._operation_owner.set((lock_db, backend_pid, lock_key))
                yield True
            finally:
                if owner_token is not None:
                    cls._operation_owner.reset(owner_token)
                if lock_db is not None:
                    try:
                        if acquired:
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
        lock = await cls._lock_for(f"connection:{int(connection_id)}", 0)
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
