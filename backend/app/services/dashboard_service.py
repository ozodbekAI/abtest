from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ab_test import ABTestStatus
from app.repositories.ab_test_repository import ABTestRepository
from app.repositories.wb_repository import WBRepository

class DashboardService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repository = WBRepository(db)

    async def stats(
        self,
        user_id: int,
        connection_id: int,
        *,
        period: str = "today",
        begin_date: date | None = None,
        end_date: date | None = None,
    ) -> dict:
        connection = await self.repository.get_for_user(user_id, connection_id)
        if not connection:
            raise HTTPException(status_code=404, detail="Магазин Wildberries не найден")

        today = date.today()
        normalized_period = period.strip().lower()
        if normalized_period == "today":
            begin, end = today, today
        elif normalized_period == "week":
            begin, end = today - timedelta(days=6), today
        elif normalized_period == "month":
            begin, end = today - timedelta(days=29), today
        elif normalized_period == "custom":
            if not begin_date or not end_date:
                raise HTTPException(status_code=422, detail="Для произвольного периода укажите обе даты")
            begin, end = begin_date, end_date
        else:
            raise HTTPException(status_code=422, detail="Период должен быть today, week, month или custom")
        if begin > end:
            raise HTTPException(status_code=422, detail="Начальная дата не может быть позже конечной")
        # Dashboard metrics intentionally come only from experiments created
        # by this application. The values are the last successfully synced
        # totals stored on ABTest, so opening the dashboard never requests all
        # seller campaigns from WB and never mixes external campaigns into
        # the workspace overview.
        tests = await ABTestRepository(self.db).list_for_user(user_id, connection_id=connection.id)
        period_tests = [
            test
            for test in tests
            if test.wb_campaign_id
            and test.started_at
            and begin <= test.started_at.date() <= end
        ]
        chart_by_date: dict[date, dict[str, int | float]] = defaultdict(
            lambda: {"tests": 0, "views": 0, "clicks": 0, "spend_rub": 0.0}
        )
        for test in period_tests:
            point = chart_by_date[test.started_at.date()]
            point["tests"] = int(point["tests"]) + 1
            point["views"] = int(point["views"]) + int(test.last_total_views or 0)
            point["clicks"] = int(point["clicks"]) + int(test.last_total_clicks or 0)
            point["spend_rub"] = float(point["spend_rub"]) + float(test.last_total_spend_rub or 0)

        totals = {
            "views": sum(int(test.last_total_views or 0) for test in period_tests),
            "clicks": sum(int(test.last_total_clicks or 0) for test in period_tests),
            "sum": sum(float(test.last_total_spend_rub or 0) for test in period_tests),
            "orders": sum(int(getattr(test, "last_total_orders", 0) or 0) for test in period_tests),
        }
        campaign_count = len(period_tests)
        active_campaign_count = sum(test.status == ABTestStatus.RUNNING for test in period_tests)
        completed_campaign_count = sum(test.status == ABTestStatus.FINISHED for test in period_tests)
        failed_campaign_count = sum(
            test.status in {ABTestStatus.FAILED, ABTestStatus.STOPPED} for test in period_tests
        )
        views = int(totals["views"])
        clicks = int(totals["clicks"])
        spend_rub = round(float(totals["sum"]), 2)
        orders = int(totals["orders"])

        return {
            "connection_id": connection.id,
            "period": normalized_period,
            "begin_date": begin,
            "end_date": end,
            "campaign_count": campaign_count,
            "active_campaign_count": active_campaign_count,
            "views": views,
            "clicks": clicks,
            "spend_rub": spend_rub,
            "orders": orders,
            "ctr": round((clicks / views) * 100, 2) if views else 0,
            "cpo_rub": round(spend_rub / orders, 2) if orders else None,
            "completed_campaign_count": completed_campaign_count,
            "failed_campaign_count": failed_campaign_count,
            "chart": [
                {
                    "date": day,
                    "tests": int(point["tests"]),
                    "views": int(point["views"]),
                    "clicks": int(point["clicks"]),
                    "spend_rub": round(float(point["spend_rub"]), 2),
                }
                for day, point in sorted(chart_by_date.items())
            ],
            "stats_complete": True,
        }
