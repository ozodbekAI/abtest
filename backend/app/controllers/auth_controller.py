from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.services.auth_service import AuthService


class AuthController:
    async def register(self, db: AsyncSession, payload: dict) -> dict:
        return await AuthService(db).start_registration(**payload)

    async def verify_email(self, db: AsyncSession, payload: dict) -> dict:
        return await AuthService(db).verify_email(**payload)

    async def resend_code(self, db: AsyncSession, email: str) -> dict:
        return await AuthService(db).resend_verification(email=email)

    async def login(self, db: AsyncSession, payload: dict) -> dict:
        return await AuthService(db).login(**payload)

    async def update_profile(self, db: AsyncSession, user: User, payload: dict) -> User:
        return await AuthService(db).update_profile(user=user, **payload)

    async def refresh(self, db: AsyncSession, refresh_token: str) -> dict:
        return await AuthService(db).refresh(refresh_token)

    async def logout(self, db: AsyncSession, refresh_token: str) -> None:
        await AuthService(db).logout(refresh_token)

    async def change_password(self, db: AsyncSession, user: User, payload: dict) -> None:
        await AuthService(db).change_password(user=user, **payload)

    async def request_reset(self, db: AsyncSession, email: str) -> dict:
        return await AuthService(db).request_password_reset(email=email)

    async def reset_password(self, db: AsyncSession, payload: dict) -> None:
        await AuthService(db).reset_password(**payload)
