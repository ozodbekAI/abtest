#!/usr/bin/env python3
"""Produce a deterministic estimate of the separate-campaign-per-photo option.

Provider-specific campaign creation latency, provider minimum deposit rules and
unused-balance refund/settlement behavior are intentionally reported as UNKNOWN
unless the operator supplies verified live values. The script never invents WB
contract values and never mutates a campaign.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import argparse
import json

from app.services.ab_test_budget import calculate_budget_breakdown, calculate_required_budget


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--photos", type=int, required=True)
    parser.add_argument("--views-per-variant", type=int, required=True)
    parser.add_argument("--cpm-rub", type=int, required=True)
    parser.add_argument("--reserve-rub", type=int, default=300)
    args = parser.parse_args()

    per_campaign = calculate_budget_breakdown(1, args.views_per_variant, args.cpm_rub, args.reserve_rub)
    one_campaign = calculate_budget_breakdown(args.photos, args.views_per_variant, args.cpm_rub, args.reserve_rub)
    result = {
        "photos": args.photos,
        "single_campaign": one_campaign,
        "separate_campaigns": {
            "campaign_count": args.photos,
            "funding_per_campaign_rub": per_campaign["funding_required_rub"],
            "funding_total_rub": per_campaign["funding_required_rub"] * args.photos,
            "forecast_spend_total_rub": per_campaign["forecast_spend_rub"] * args.photos,
            "minimum_provider_deposit_rule": "UNKNOWN - verify against current WB Promotion API contract",
            "campaign_start_latency": "UNKNOWN - measure on controlled WB pilot",
            "remaining_budget_behavior": "UNKNOWN - verify provider settlement/refund semantics on controlled WB pilot",
        },
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
