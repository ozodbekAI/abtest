"""Budget calculation shared by A/B-test start and API read models.

Keep this formula aligned with the original WB Optimizer flow. The extra
buffer protects a test from small billing/rounding differences. The current
connected account shows a 1,200 ₽ floor, so it is used as the safety floor
until WB exposes a supported account-specific minimum-budget endpoint.
"""

from __future__ import annotations

import math


MIN_TEST_BUDGET_RUB = 1_200
BUDGET_BUFFER_RATE = 1.10
BUDGET_STEP_RUB = 100


def calculate_required_budget(
    photos_count: int,
    views_per_variant: int,
    cpm_rub: int,
) -> int:
    """Return the campaign budget required for all A/B-test stages."""

    photos = max(int(photos_count or 0), 0)
    views = max(int(views_per_variant or 0), 0)
    cpm = max(int(cpm_rub or 0), 0)
    raw_spend = (photos * views * cpm) / 1_000.0
    protected_spend = max(raw_spend * BUDGET_BUFFER_RATE, float(MIN_TEST_BUDGET_RUB))
    return int(math.ceil(protected_spend / BUDGET_STEP_RUB) * BUDGET_STEP_RUB)
