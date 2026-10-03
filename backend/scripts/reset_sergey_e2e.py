"""Reset selected Sergey E2E A/B tests without weakening production lifecycle locks.

This utility is deliberately test-only. It deletes ABTest rows from an isolated
E2E database so the fixed nm_id matrix can be executed repeatedly. Production
code still treats reconciliation-required tests as hard lifecycle locks.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

# Allow direct execution via: python backend/scripts/reset_sergey_e2e.py
# by adding the backend package root to sys.path.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import select

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.ab_test import ABTest


ALLOWED_E2E_ENVS = {"e2e", "test", "testing", "ci"}
ACTIVE_CAMPAIGN_STATES = {
    "running",
    "starting",
    "created",
    "unknown",
    "pause_requested",
    "stop_requested",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Delete selected A/B-test rows from a Sergey E2E/test database."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--test-id", type=int, action="append", dest="test_ids")
    group.add_argument("--nm-id", type=int, action="append", dest="nm_ids")
    parser.add_argument(
        "--allow-orphaned-campaigns",
        action="store_true",
        help="Allow deletion of failed E2E rows whose DB campaign state is not stopped/not_created.",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required acknowledgement that this operation permanently removes selected E2E test rows.",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    if settings.app_env not in ALLOWED_E2E_ENVS:
        raise SystemExit(
            f"Refusing reset: APP_ENV={settings.app_env!r}. "
            f"Expected one of {sorted(ALLOWED_E2E_ENVS)}."
        )
    if settings.wb_allow_external_requests:
        raise SystemExit(
            "Refusing reset while WB_ALLOW_EXTERNAL_REQUESTS=true. "
            "Run this utility only against an isolated E2E/test environment."
        )
    if not args.confirm:
        raise SystemExit("Pass --confirm to acknowledge the destructive E2E reset.")

    async with AsyncSessionLocal() as db:
        if args.test_ids:
            query = select(ABTest).where(ABTest.id.in_(args.test_ids)).order_by(ABTest.id)
        else:
            query = select(ABTest).where(ABTest.nm_id.in_(args.nm_ids)).order_by(ABTest.id)

        tests = list((await db.scalars(query)).all())
        if not tests:
            print("No matching A/B tests found. E2E database is already clear for the selection.")
            return

        media_dirs: list[Path] = []
        for test in tests:
            print(
                f"test_id={test.id} nm_id={test.nm_id} status={test.status.value} "
                f"operation_state={test.operation_state} campaign_state={test.campaign_state} "
                f"media_status={test.media_status} lock={test.lifecycle_lock}"
            )

            if test.campaign_state in ACTIVE_CAMPAIGN_STATES and not args.allow_orphaned_campaigns:
                raise SystemExit(
                    f"Refusing test_id={test.id}: campaign_state={test.campaign_state!r}. "
                    "Use --allow-orphaned-campaigns only after the E2E/mock provider has been reset."
                )

            media_dirs.append(Path(settings.media_root) / "ab_tests" / str(test.id))
            await db.delete(test)

        await db.commit()

    for media_dir in media_dirs:
        if media_dir.exists():
            shutil.rmtree(media_dir)
            print(f"removed media: {media_dir}")

    print(f"Reset {len(tests)} E2E test(s).")


if __name__ == "__main__":
    asyncio.run(main())
