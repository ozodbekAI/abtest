import run_backend
import asyncio
from app.core.database import AsyncSessionLocal
from app.services.ab_test_service import ABTestService
async def main():
    async with AsyncSessionLocal() as db:
        await ABTestService(db).scheduler_tick()
asyncio.run(main())
