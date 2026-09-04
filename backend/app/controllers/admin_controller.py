from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.schemas.admin import AdminUserCreateRequest, AdminUserUpdateRequest
from app.services.admin_service import AdminService


class AdminController:
    async def dashboard(self, db: AsyncSession) -> dict:
        return await AdminService(db).dashboard()

    async def users(self, db: AsyncSession, page: int, page_size: int, search: str) -> dict:
        return await AdminService(db).list_users(page=page, page_size=page_size, search=search)

    async def user_detail(self, db: AsyncSession, actor: User, user_id: int) -> dict:
        return await AdminService(db).get_user_detail(actor, user_id)

    async def create_user(self, db: AsyncSession, request: AdminUserCreateRequest) -> dict:
        return await AdminService(db).create_user(request)

    async def update_user(self, db: AsyncSession, actor: User, user_id: int, request: AdminUserUpdateRequest) -> dict:
        return await AdminService(db).update_user(actor, user_id, request)

    async def change_password(self, db: AsyncSession, actor: User, user_id: int, password: str) -> dict:
        return await AdminService(db).change_password(actor, user_id, password)

    async def delete_user(self, db: AsyncSession, actor: User, user_id: int) -> None:
        await AdminService(db).delete_user(actor, user_id)

    async def stores(self, db: AsyncSession, search: str) -> list[dict]:
        return await AdminService(db).list_stores(search)

    async def store_detail(self, db: AsyncSession, store_id: int) -> dict:
        return await AdminService(db).get_store_detail(store_id)

    async def settings(self, db: AsyncSession) -> dict:
        return await AdminService(db).get_settings()

    async def update_settings(self, db: AsyncSession, registration_enabled: bool) -> dict:
        return await AdminService(db).update_settings(registration_enabled)
