"""Budget calculation shared by A/B-test start and API read models."""

from __future__ import annotations

MIN_TEST_BUDGET_RUB = 1_200
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
    # Keep the arithmetic integer-based: CPM and the input volume are in
    # whole rubles/impressions, while WB's campaign budget is sent in rubles.
    # Round only the fractional ruble up, then apply the explicitly displayed
    # 1,200 ₽ platform floor. There is no hidden percentage reserve here.
    raw_spend_milli_rub = photos * views * cpm
    raw_spend_rub = (raw_spend_milli_rub + 999) // 1_000
    required = max(raw_spend_rub, MIN_TEST_BUDGET_RUB)
    return ((required + BUDGET_STEP_RUB - 1) // BUDGET_STEP_RUB) * BUDGET_STEP_RUB
