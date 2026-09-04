from datetime import date

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.controllers.wb_controller import WBController
from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.schemas.wb import WBConnectionCreateRequest, WBConnectionResponse, WBPromotionBalanceResponse, WBTokenRequest
from app.schemas.dashboard import DashboardStatsResponse


router = APIRouter(prefix="/wb", tags=["Wildberries"])
controller = WBController()


@router.get("/dashboard", response_model=DashboardStatsResponse)
async def dashboard_stats(
    connection_id: int = Query(gt=0),
    period: str = Query(default="today", pattern="^(today|week|month|custom)$"),
    begin_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await controller.dashboard_stats(
        db,
        current_user.id,
        connection_id,
        period=period,
        begin_date=begin_date,
        end_date=end_date,
    )


@router.get("/promotion/balance", response_model=WBPromotionBalanceResponse)
async def promotion_balance(
    connection_id: int = Query(gt=0),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await controller.promotion_balance(db, current_user.id, connection_id)


@router.get("/token", response_model=WBConnectionResponse)
async def get_wb_token(
    connection_id: int | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await controller.get_connection(db, current_user.id, connection_id)


@router.get("/connections", response_model=list[WBConnectionResponse])
async def get_wb_connections(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await controller.get_connections(db, current_user.id)


@router.post("/connections", response_model=WBConnectionResponse)
async def create_wb_connection(
    request: WBConnectionCreateRequest,
    require_ab_test_access: bool = False,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await controller.connect(
        db,
        current_user.id,
        request.token,
        store_name=request.store_name,
        require_ab_test_access=require_ab_test_access,
    )


@router.post("/token", response_model=WBConnectionResponse)
async def connect_wb_token(
    request: WBTokenRequest,
    require_ab_test_access: bool = False,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await controller.connect(
        db,
        current_user.id,
        request.token,
        store_name=request.store_name,
        require_ab_test_access=require_ab_test_access,
    )


@router.post("/token/validate", response_model=WBConnectionResponse)
async def validate_wb_token(
    connection_id: int | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await controller.validate(db, current_user.id, connection_id)


@router.post("/connections/{connection_id}/validate", response_model=WBConnectionResponse)
async def validate_wb_connection(
    connection_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await controller.validate(db, current_user.id, connection_id)


@router.delete("/token", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_wb_token(
    connection_id: int | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await controller.disconnect(db, current_user.id, connection_id)


@router.delete("/connections/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_wb_connection(
    connection_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await controller.disconnect(db, current_user.id, connection_id)
