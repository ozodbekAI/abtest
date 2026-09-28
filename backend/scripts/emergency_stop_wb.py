"""Out-of-band Wildberries emergency stop.

Usage:
  WB_TOKEN='...' CAMPAIGN_ID=123 python scripts/emergency_stop_wb.py

This path intentionally does not import the application database. It is for the
rare case where the DB is unavailable while a known WB campaign must be made
non-serving immediately. The command verifies the campaign after the stop
request and exits non-zero until a non-active status is confirmed.
"""
from __future__ import annotations

import asyncio
import os
import sys

from app.services.wb_promotion_client import WBPromotionClient


async def main() -> int:
    token = os.getenv("WB_TOKEN", "").strip()
    raw_campaign = os.getenv("CAMPAIGN_ID", "").strip()
    if not token or not raw_campaign.isdigit():
        print("Set WB_TOKEN and numeric CAMPAIGN_ID", file=sys.stderr)
        return 2
    campaign_id = int(raw_campaign)
    client = WBPromotionClient(token)
    status = await client.get_campaign_status(campaign_id, refresh=True)
    print(f"before_status={status}")
    if status in WBPromotionClient.NON_ACTIVE_CAMPAIGN_STATUSES:
        print("campaign_already_non_active")
        return 0
    try:
        await client.stop_campaign(campaign_id)
    except Exception as exc:
        print(f"stop_request_error={exc}", file=sys.stderr)
    for _ in range(6):
        await asyncio.sleep(5)
        status = await client.get_campaign_status(campaign_id, refresh=True)
        print(f"status={status}")
        if status in WBPromotionClient.NON_ACTIVE_CAMPAIGN_STATUSES:
            print("confirmed_non_active")
            return 0
    print("STOP_NOT_CONFIRMED", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
