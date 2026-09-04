from datetime import date

from pydantic import BaseModel


class DashboardChartPoint(BaseModel):
    """A local, test-level aggregate used by the workspace dashboard.

    The values are the latest totals saved for experiments started on this
    date. They are deliberately not presented as WB day-by-day attribution.
    """

    date: date
    tests: int
    views: int
    clicks: int
    spend_rub: float


class DashboardStatsResponse(BaseModel):
    connection_id: int
    period: str
    begin_date: date
    end_date: date
    campaign_count: int
    active_campaign_count: int
    views: int
    clicks: int
    spend_rub: float
    orders: int
    ctr: float
    cpo_rub: float | None = None
    completed_campaign_count: int
    failed_campaign_count: int
    chart: list[DashboardChartPoint]
    stats_complete: bool = True
