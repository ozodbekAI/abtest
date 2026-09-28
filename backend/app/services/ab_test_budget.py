"""Budget calculation shared by A/B-test start and API read models."""

from __future__ import annotations

MIN_TEST_BUDGET_RUB = 1_200
BUDGET_STEP_RUB = 100
DEFAULT_SAFETY_RESERVE_RUB = 300


def calculate_budget_breakdown(
    photos_count: int,
    views_per_variant: int,
    cpm_rub: int,
    safety_reserve_rub: int = DEFAULT_SAFETY_RESERVE_RUB,
) -> dict[str, int | float]:
    """Expose the forecast separately from campaign funding and stop reserve.

    All rounding is integer arithmetic. A campaign allocation is permission
    to transfer funds, not evidence of advertising spend; the reserve is a
    disclosed stopping threshold, not a guarantee against delayed WB charges.
    """
    milli_rub = max(int(photos_count or 0), 0) * max(int(views_per_variant or 0), 0) * max(int(cpm_rub or 0), 0)
    forecast_kopecks = (milli_rub + 9) // 10
    base = calculate_required_budget(photos_count, views_per_variant, cpm_rub)
    protected = calculate_protected_budget(photos_count, views_per_variant, cpm_rub, safety_reserve_rub)
    reserve = max(int(safety_reserve_rub or 0), 0)
    return {
        "forecast_spend_rub": forecast_kopecks / 100,
        "minimum_campaign_budget_rub": MIN_TEST_BUDGET_RUB,
        "funding_step_rub": BUDGET_STEP_RUB,
        "base_funding_rub": base,
        "safety_reserve_rub": reserve,
        "funding_rounding_rub": protected - base - reserve,
        "funding_required_rub": protected,
        "stop_threshold_rub": protected - reserve,
    }


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


def calculate_protected_budget(
    photos_count: int,
    views_per_variant: int,
    cpm_rub: int,
    safety_reserve_rub: int = DEFAULT_SAFETY_RESERVE_RUB,
) -> int:
    """Return the amount required for the planned stages plus a hard safety reserve."""
    base = calculate_required_budget(photos_count, views_per_variant, cpm_rub)
    reserve = max(int(safety_reserve_rub or 0), 0)
    protected = base + reserve
    return ((protected + BUDGET_STEP_RUB - 1) // BUDGET_STEP_RUB) * BUDGET_STEP_RUB
