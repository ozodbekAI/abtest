from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.user import User
from app.repositories.admin_repository import AdminRepository
from app.repositories.app_settings_repository import AppSettingsRepository
from app.repositories.auth_repository import AuthRepository
from app.repositories.wb_repository import WBRepository
from app.schemas.admin import AdminUserCreateRequest, AdminUserUpdateRequest


class AdminService:
    REGISTRATION_KEY = "registration_enabled"

    def __init__(self, db: AsyncSession):
        self.db = db
        self.repository = AdminRepository(db)
        self.settings_repo = AppSettingsRepository(db)
        self.auth = AuthRepository(db)

    @staticmethod
    def _user_name(user: User) -> str:
        return " ".join(part for part in (user.first_name, user.last_name) if part).strip() or "Без имени"

    @classmethod
    def user_item(cls, user: User, stores_count: int, tests_count: int) -> dict:
        return {
            "id": user.id,
            "email": user.email,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "is_verified": user.is_verified,
            "is_active": user.is_active,
            "is_admin": user.is_admin,
            "stores_count": stores_count,
            "tests_count": tests_count,
            "created_at": user.created_at,
        }

    @classmethod
    def store_item(cls, connection, owner: User, tests_count: int) -> dict:
        return {
            "id": connection.id,
            "user_id": owner.id,
            "owner_email": owner.email,
            "owner_name": cls._user_name(owner),
            "store_name": connection.store_name,
            "status": connection.status,
            "ready_for_ab_tests": connection.ready_for_ab_tests,
            "token_last4": connection.token_last4,
            "tests_count": tests_count,
            "last_validated_at": connection.last_validated_at,
            "created_at": connection.created_at,
        }

    @staticmethod
    def test_item(test) -> dict:
        status_value = test.status.value if hasattr(test.status, "value") else str(test.status)
        return {
            "id": test.id,
            "connection_id": test.connection_id,
            "store_name": test.connection.store_name,
            "user_id": test.user_id,
            "nm_id": test.nm_id,
            "title": test.title,
            "status": status_value,
            "wb_campaign_id": test.wb_campaign_id,
            "total_views": int(test.last_total_views or 0),
            "total_clicks": int(test.last_total_clicks or 0),
            "total_orders": int(getattr(test, "last_total_orders", 0) or 0),
            "total_spend_rub": round(float(test.last_total_spend_rub or 0), 2),
            "total_ctr": round(
                (int(test.last_total_clicks or 0) / int(test.last_total_views or 0)) * 100,
                4,
            ) if test.last_total_views else 0,
            "last_error": test.last_error,
            "started_at": test.started_at,
            "finished_at": test.finished_at,
            "created_at": test.created_at,
        }

    async def list_users(self, *, page: int, page_size: int, search: str) -> dict:
        rows, total = await self.repository.list_users(page=page, page_size=page_size, search=search)
        return {
            "items": [self.user_item(user, stores_count, tests_count) for user, stores_count, tests_count in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    async def get_user_detail(self, actor: User, user_id: int) -> dict:
        if actor.id == user_id:
            raise HTTPException(status_code=403, detail="Нельзя изменять собственную учётную запись через админ-панель")
        user = await self._user_or_404(user_id)
        stores = await WBRepository(self.db).list_for_user(user.id)
        tests = await self.repository.list_tests_for_user(user.id)
        return {
            "user": self.user_item(user, len(stores), len(tests)),
            "stores": [self.store_item(store, user, sum(test.connection_id == store.id for test in tests)) for store in stores],
            "tests": [self.test_item(test) for test in tests],
        }

    async def list_stores(self, search: str) -> list[dict]:
        rows = await self.repository.list_stores(search=search)
        return [self.store_item(connection, owner, tests_count) for connection, owner, tests_count in rows]

    async def get_store_detail(self, store_id: int) -> dict:
        store = await self.repository.get_store(store_id)
        if not store:
            raise HTTPException(status_code=404, detail="Магазин не найден")
        tests = await self.repository.list_tests_for_store(store.id)
        return {
            "store": self.store_item(store, store.user, len(tests)),
            "tests": [self.test_item(test) for test in tests],
        }

    async def dashboard(self) -> dict:
        return await self.repository.dashboard()

    async def create_user(self, request: AdminUserCreateRequest) -> dict:
        email = str(request.email).lower().strip()
        if await self.repository.get_user_by_email(email):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Пользователь с такой почтой уже существует")
        try:
            user = User(
                email=email,
                hashed_password=hash_password(request.password),
                first_name=request.first_name.strip(),
                last_name=request.last_name.strip(),
                is_verified=request.is_verified,
                is_active=request.is_active,
                is_admin=request.is_admin,
            )
            self.db.add(user)
            await self.db.flush()
        except IntegrityError as exc:
            await self.db.rollback()
            raise HTTPException(status_code=409, detail="Пользователь с такой почтой уже существует") from exc
        await self.db.commit()
        await self.db.refresh(user)
        return self.user_item(user, 0, 0)

    async def update_user(self, actor: User, user_id: int, request: AdminUserUpdateRequest) -> dict:
        if actor.id == user_id:
            raise HTTPException(status_code=403, detail="Нельзя изменять собственную учётную запись через админ-панель")
        user = await self._user_or_404(user_id)
        values = request.model_dump(exclude_unset=True)
        if "email" in values:
            email = str(values["email"]).lower().strip()
            existing = await self.repository.get_user_by_email(email)
            if existing and existing.id != user.id:
                raise HTTPException(status_code=409, detail="Пользователь с такой почтой уже существует")
            user.email = email
        for field in ("first_name", "last_name"):
            if field in values and values[field] is not None:
                user_value = values[field].strip()
                if field == "first_name" and len(user_value) < 2:
                    raise HTTPException(status_code=422, detail="Имя должно содержать минимум 2 символа")
                if field == "last_name" and user_value and len(user_value) < 2:
                    raise HTTPException(status_code=422, detail="Фамилия должна содержать минимум 2 символа")
                setattr(user, field, user_value)
        if values.get("is_admin") is False and user.is_admin:
            if user.id == actor.id or await self.repository.admin_count() <= 1:
                raise HTTPException(status_code=409, detail="Нельзя отключить последнего администратора")
        if values.get("is_active") is False and user.id == actor.id:
            raise HTTPException(status_code=409, detail="Нельзя отключить собственный аккаунт")
        for field in ("is_verified", "is_active", "is_admin"):
            if field in values and values[field] is not None:
                setattr(user, field, values[field])
        if values.get("is_active") is False:
            await self.auth.revoke_all_refresh_tokens(user.id)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise HTTPException(status_code=409, detail="Пользователь с такой почтой уже существует") from exc
        await self.db.refresh(user)
        stores = await WBRepository(self.db).list_for_user(user.id)
        tests = await self.repository.list_tests_for_user(user.id)
        return self.user_item(user, len(stores), len(tests))

    async def change_password(self, actor: User, user_id: int, password: str) -> dict:
        if actor.id == user_id:
            raise HTTPException(status_code=403, detail="Нельзя изменять собственную учётную запись через админ-панель")
        user = await self._user_or_404(user_id)
        user.hashed_password = hash_password(password)
        user.failed_login_attempts = 0
        user.locked_until = None
        await self.auth.revoke_all_refresh_tokens(user.id)
        await self.db.commit()
        return {"message": "Пароль пользователя изменён, активные сессии завершены"}

    async def delete_user(self, actor: User, user_id: int) -> None:
        if user_id == actor.id:
            raise HTTPException(status_code=409, detail="Нельзя удалить собственный аккаунт")
        user = await self._user_or_404(user_id)
        if user.is_admin and await self.repository.admin_count() <= 1:
            raise HTTPException(status_code=409, detail="Нельзя удалить последнего администратора")
        # User deletion cascades WB connections, tests, operations, and local
        # media metadata. Never allow that cascade while an external campaign
        # may still be serving or while a media restore/reconciliation is
        # pending; otherwise the operator loses the only durable recovery
        # record and cannot safely stop/restore the WB card.
        tests = await self.repository.list_tests_for_user(user.id)
        unsafe_tests = [
            test
            for test in tests
            if test.status.value == "running"
            or test.operation_state == "reconciliation_required"
            or test.campaign_state in {"running", "starting", "stop_requested", "unknown"}
            or test.media_status in {"variant_applied", "swapping", "restoring"}
            or bool((test.media_state or {}).get("pending"))
        ]
        if unsafe_tests:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Нельзя удалить пользователя: сначала завершите или сверите его A/B-тесты "
                    f"({', '.join(str(test.id) for test in unsafe_tests[:5])})."
                ),
            )
        await self.repository.delete_user(user)
        await self.db.commit()

    async def get_settings(self) -> dict:
        return {"registration_enabled": await self.settings_repo.get_bool(self.REGISTRATION_KEY, default=True)}

    async def update_settings(self, registration_enabled: bool) -> dict:
        await self.settings_repo.set_bool(self.REGISTRATION_KEY, registration_enabled)
        await self.db.commit()
        return await self.get_settings()

    async def _user_or_404(self, user_id: int) -> User:
        user = await self.repository.get_user(user_id)
        if not user:
            raise HTTPException(status_code=404, detail="Пользователь не найден")
        return user
