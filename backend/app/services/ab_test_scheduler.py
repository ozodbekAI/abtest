from __future__ import annotations

import asyncio
import logging

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.services.ab_test_service import ABTestService


logger = logging.getLogger(__name__)


class ABTestScheduler:
    def __init__(self):
        self.task: asyncio.Task | None = None

    def start(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run(), name="wb-ab-test-scheduler")

    async def stop(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.task = None

    async def _run(self) -> None:
        interval = max(int(settings.ab_test_scheduler_interval_sec), 20)
        while True:
            try:
                async with AsyncSessionLocal() as db:
                    await ABTestService(db).scheduler_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("A/B-test scheduler tick failed")
            await asyncio.sleep(interval)
