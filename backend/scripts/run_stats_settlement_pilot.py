#!/usr/bin/env python3
"""Controlled 60-minute WB fullstats late-data pilot.

The script is intentionally read-only with respect to WB campaign state: it never
starts, pauses, stops, or changes media. The caller is responsible for pausing the
campaign before starting the pilot and keeping it paused for the full duration.
Raw observations are written as JSONL so the 3/10/30 minute reconciliation policy
can be calibrated from real provider behavior.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import argparse
import asyncio
import json
from datetime import datetime, timezone


async def run(test_id: int, minutes: int, output: Path) -> None:
    # Import the application only when the pilot actually runs so `--help` and
    # packaging checks do not require an installed database driver.
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from app.core.database import AsyncSessionLocal
    from app.models.ab_test import ABTest
    from app.services.ab_test_service import ABTestService

    async with AsyncSessionLocal() as db:
        stmt = select(ABTest).options(selectinload(ABTest.connection)).where(ABTest.id == test_id)
        test = (await db.execute(stmt)).scalar_one_or_none()
        if not test:
            raise SystemExit(f"A/B test {test_id} not found")
        if not test.wb_campaign_id:
            raise SystemExit("The test has no WB campaign id")
        connection = test.connection
        service = ABTestService(db)
        _, promotion = await service._clients(connection)
        output.parent.mkdir(parents=True, exist_ok=True)
        deadline = asyncio.get_running_loop().time() + max(minutes, 1) * 60
        with output.open("a", encoding="utf-8") as fh:
            while asyncio.get_running_loop().time() <= deadline:
                observed_at = datetime.now(timezone.utc).isoformat()
                status = await promotion.get_campaign_status(test.wb_campaign_id, refresh=True)
                totals = await service._fullstats_windowed(
                    promotion,
                    test.wb_campaign_id,
                    test.started_at,
                    refresh=True,
                )
                record = {
                    "observed_at": observed_at,
                    "test_id": test.id,
                    "campaign_id": test.wb_campaign_id,
                    "campaign_status": status,
                    "totals": totals,
                }
                fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                fh.flush()
                await asyncio.sleep(180)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-id", type=int, required=True)
    parser.add_argument("--minutes", type=int, default=60)
    parser.add_argument("--output", type=Path, default=Path("pilot_stats.jsonl"))
    args = parser.parse_args()
    await run(args.test_id, args.minutes, args.output)


if __name__ == "__main__":
    asyncio.run(main())
