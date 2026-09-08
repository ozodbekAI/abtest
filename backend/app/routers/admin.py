from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.controllers.admin_controller import AdminController
from app.core.database import get_db
from app.core.security import get_current_admin
from app.models.user import User
from app.schemas.admin import (
    AdminDashboardResponse,
    AdminAuditResponse,
    AdminPasswordRequest,
    AdminSettingsResponse,
    AdminSettingsUpdateRequest,
    AdminStoreDetail,
    AdminStoreItem,
    AdminUserCreateRequest,
    AdminUserDetail,
    AdminUserItem,
    AdminUserListResponse,
    AdminUserUpdateRequest,
)


router = APIRouter(prefix="/admin", tags=["Администрирование"])
controller = AdminController()


@router.get("/dashboard", response_model=AdminDashboardResponse)
async def dashboard(
    _: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    return await controller.dashboard(db)


@router.get("/users", response_model=AdminUserListResponse)
async def users(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    search: str = Query(default="", max_length=120),
    _: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    return await controller.users(db, page, page_size, search)


@router.post("/users", response_model=AdminUserItem, status_code=status.HTTP_201_CREATED)
async def create_user(
    request: AdminUserCreateRequest,
    actor: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    return await controller.create_user(db, actor, request)


@router.get("/users/{user_id}", response_model=AdminUserDetail)
async def user_detail(
    user_id: int,
    actor: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    return await controller.user_detail(db, actor, user_id)


@router.patch("/users/{user_id}", response_model=AdminUserItem)
async def update_user(
    user_id: int,
    request: AdminUserUpdateRequest,
    actor: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    return await controller.update_user(db, actor, user_id, request)


@router.post("/users/{user_id}/password", response_model=dict[str, str])
async def change_password(
    user_id: int,
    request: AdminPasswordRequest,
    actor: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    return await controller.change_password(db, actor, user_id, request.password)


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: int,
    actor: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    await controller.delete_user(db, actor, user_id)


@router.get("/stores", response_model=list[AdminStoreItem])
async def stores(
    search: str = Query(default="", max_length=120),
    _: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    return await controller.stores(db, search)


@router.get("/stores/{store_id}", response_model=AdminStoreDetail)
async def store_detail(
    store_id: int,
    _: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    return await controller.store_detail(db, store_id)


@router.get("/settings", response_model=AdminSettingsResponse)
async def get_settings(
    _: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    return await controller.settings(db)


@router.patch("/settings", response_model=AdminSettingsResponse)
async def update_settings(
    request: AdminSettingsUpdateRequest,
    actor: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    return await controller.update_settings(db, actor, request.registration_enabled)


@router.get("/audit", response_model=AdminAuditResponse)
async def audit(
    limit: int = Query(default=100, ge=1, le=500),
    _: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    return await controller.audit_logs(db, limit)
