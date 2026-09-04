from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import (
    create_access_token,
    create_refresh_token,
    generate_code,
    hash_opaque_token,
    hash_password,
    verify_password,
)
from app.models.user import User
from app.repositories.auth_repository import AuthRepository
from app.repositories.app_settings_repository import AppSettingsRepository
from app.repositories.user_repository import UserRepository
from app.services.email_service import EmailService


class AuthService:
    CODE_TTL_MINUTES = 15
    CODE_MAX_ATTEMPTS = 5
    LOGIN_MAX_ATTEMPTS = 5
    LOGIN_LOCK_MINUTES = 15

    def __init__(self, db: AsyncSession):
        self.db = db
        self.users = UserRepository(db)
        self.auth = AuthRepository(db)
        self.email = EmailService()

    @staticmethod
    def user_payload(user: User) -> dict:
        return {
            "id": user.id,
            "email": user.email,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "is_verified": user.is_verified,
            "is_active": user.is_active,
            "is_admin": user.is_admin,
            "created_at": user.created_at,
        }

    async def issue_tokens(self, user: User) -> dict:
        access_token, expires_in = create_access_token(user.id)
        refresh_token, expires_at = create_refresh_token(user.id)
        await self.auth.add_refresh_token(
            user_id=user.id,
            token_hash=hash_opaque_token(refresh_token),
            expires_at=expires_at,
        )
        await self.db.commit()
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
            "expires_in": expires_in,
            "user": self.user_payload(user),
        }

    async def _send_verification_code(self, user: User) -> dict:
        if await self.auth.has_recent_code(user_id=user.id, purpose="email_verification"):
            raise HTTPException(status_code=429, detail="Подождите перед повторным запросом кода")
        await self.auth.revoke_user_codes(user_id=user.id, purpose="email_verification")
        code = generate_code()
        await self.auth.add_code(
            user_id=user.id,
            purpose="email_verification",
            code_hash=hash_opaque_token(code),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=self.CODE_TTL_MINUTES),
        )
        await self.db.commit()
        await self.email.send_code(recipient=user.email, code=code, purpose="email_verification")
        return {"message": "Код подтверждения отправлен", "email": user.email, "expires_in": self.CODE_TTL_MINUTES * 60}

    async def start_registration(self, *, email: str, password: str, confirm_password: str, first_name: str, last_name: str) -> dict:
        if not await AppSettingsRepository(self.db).get_bool("registration_enabled", default=True):
            raise HTTPException(status_code=403, detail="Регистрация новых пользователей временно отключена администратором")
        if password != confirm_password:
            raise HTTPException(status_code=400, detail="Пароли не совпадают")
        normalized_email = email.lower().strip()
        user = await self.users.get_by_email(normalized_email)
        if user and user.is_verified:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Аккаунт с этой электронной почтой уже существует")

        if user:
            user.hashed_password = hash_password(password)
            user.first_name = first_name.strip()
            user.last_name = last_name.strip()
            user.is_active = True
        else:
            user = await self.users.create(
                email=normalized_email,
                hashed_password=hash_password(password),
                first_name=first_name.strip(),
                last_name=last_name.strip(),
                is_verified=False,
                is_active=True,
            )

        return await self._send_verification_code(user)

    async def verify_email(self, *, email: str, code: str) -> dict:
        user = await self.users.get_by_email(email)
        if not user:
            raise HTTPException(status_code=400, detail="Неверная электронная почта или код подтверждения")
        verification = await self.auth.get_active_code(user_id=user.id, purpose="email_verification", for_update=True)
        if not verification or verification.code_hash != hash_opaque_token(code.strip()):
            if verification:
                verification.attempts += 1
                if verification.attempts >= self.CODE_MAX_ATTEMPTS:
                    verification.consumed_at = datetime.now(timezone.utc)
                await self.db.commit()
            raise HTTPException(status_code=400, detail="Неверный или просроченный код подтверждения")
        user.is_verified = True
        await self.auth.consume_code(verification)
        await self.db.commit()
        await self.db.refresh(user)
        return await self.issue_tokens(user)

    async def resend_verification(self, *, email: str) -> dict:
        user = await self.users.get_by_email(email)
        if not user or user.is_verified:
            return {"message": "Если аккаунту требуется подтверждение, код отправлен", "email": email.lower().strip(), "expires_in": self.CODE_TTL_MINUTES * 60}
        return await self._send_verification_code(user)

    async def login(self, *, email: str, password: str) -> dict:
        # Serialize failed-attempt counters and refresh the lock state from the
        # same row. Without this lock, concurrent password guesses could all
        # read the same counter and bypass the five-attempt guard.
        user = await self.users.get_by_email(email, for_update=True)
        now = datetime.now(timezone.utc)
        if user and user.locked_until and user.locked_until > now:
            raise HTTPException(status_code=429, detail="Слишком много неудачных попыток. Повторите вход позже")
        if not user or not verify_password(password, user.hashed_password):
            if user:
                user.failed_login_attempts = int(user.failed_login_attempts or 0) + 1
                if user.failed_login_attempts >= self.LOGIN_MAX_ATTEMPTS:
                    user.failed_login_attempts = 0
                    user.locked_until = now + timedelta(minutes=self.LOGIN_LOCK_MINUTES)
                await self.db.commit()
            raise HTTPException(status_code=401, detail="Неверная электронная почта или пароль")
        if not user.is_verified:
            raise HTTPException(status_code=403, detail="Сначала подтвердите электронную почту")
        if not user.is_active:
            raise HTTPException(status_code=403, detail="Ваш аккаунт отключён")
        user.failed_login_attempts = 0
        user.locked_until = None
        return await self.issue_tokens(user)

    async def update_profile(self, *, user: User, first_name: str, last_name: str) -> User:
        user.first_name = first_name.strip()
        user.last_name = last_name.strip()
        await self.db.commit()
        await self.db.refresh(user)
        return user

    async def refresh(self, refresh_token: str) -> dict:
        stored = await self.auth.get_refresh_token(hash_opaque_token(refresh_token))
        now = datetime.now(timezone.utc)
        if not stored or stored.revoked_at or stored.expires_at <= now:
            raise HTTPException(status_code=401, detail="Неверный или просроченный токен обновления")
        user = await self.users.get_by_id(stored.user_id)
        if not user or not user.is_active:
            raise HTTPException(status_code=401, detail="Пользователь неактивен")
        await self.auth.revoke_refresh_token(stored)
        result = await self.issue_tokens(user)
        return result

    async def logout(self, refresh_token: str) -> None:
        stored = await self.auth.get_refresh_token(hash_opaque_token(refresh_token))
        if stored and not stored.revoked_at:
            await self.auth.revoke_refresh_token(stored)
            await self.db.commit()

    async def change_password(self, *, user: User, current_password: str, new_password: str) -> None:
        if not verify_password(current_password, user.hashed_password):
            raise HTTPException(status_code=400, detail="Текущий пароль указан неверно")
        user.hashed_password = hash_password(new_password)
        user.failed_login_attempts = 0
        user.locked_until = None
        await self.auth.revoke_all_refresh_tokens(user.id)
        await self.db.commit()

    async def request_password_reset(self, *, email: str) -> dict:
        normalized_email = email.lower().strip()
        user = await self.users.get_by_email(normalized_email)
        if user:
            if await self.auth.has_recent_code(user_id=user.id, purpose="password_reset"):
                return {"message": "Если аккаунт существует, код восстановления отправлен"}
            await self.auth.revoke_user_codes(user_id=user.id, purpose="password_reset")
            code = generate_code()
            await self.auth.add_code(
                user_id=user.id,
                purpose="password_reset",
                code_hash=hash_opaque_token(code),
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=self.CODE_TTL_MINUTES),
            )
            await self.db.commit()
            await self.email.send_code(recipient=user.email, code=code, purpose="password_reset")
        return {"message": "Если аккаунт существует, код восстановления отправлен"}

    async def reset_password(self, *, email: str, code: str, new_password: str) -> None:
        user = await self.users.get_by_email(email)
        if not user:
            raise HTTPException(status_code=400, detail="Неверная электронная почта или код восстановления")
        reset = await self.auth.get_active_code(user_id=user.id, purpose="password_reset", for_update=True)
        if not reset or reset.code_hash != hash_opaque_token(code.strip()):
            if reset:
                reset.attempts += 1
                if reset.attempts >= self.CODE_MAX_ATTEMPTS:
                    reset.consumed_at = datetime.now(timezone.utc)
                await self.db.commit()
            raise HTTPException(status_code=400, detail="Неверный или просроченный код восстановления")
        user.hashed_password = hash_password(new_password)
        user.failed_login_attempts = 0
        user.locked_until = None
        await self.auth.consume_code(reset)
        await self.auth.revoke_all_refresh_tokens(user.id)
        await self.db.commit()
