from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import EmailVerificationCode, RefreshToken


class AuthRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def add_code(self, *, user_id: int, purpose: str, code_hash: str, expires_at: datetime) -> EmailVerificationCode:
        code = EmailVerificationCode(user_id=user_id, purpose=purpose, code_hash=code_hash, expires_at=expires_at)
        self.db.add(code)
        await self.db.flush()
        return code

    async def get_active_code(
        self, *, user_id: int, purpose: str, for_update: bool = False
    ) -> EmailVerificationCode | None:
        now = datetime.now(timezone.utc)
        stmt = (
            select(EmailVerificationCode)
            .where(
                EmailVerificationCode.user_id == user_id,
                EmailVerificationCode.purpose == purpose,
                EmailVerificationCode.consumed_at.is_(None),
                EmailVerificationCode.expires_at > now,
            )
            .order_by(EmailVerificationCode.created_at.desc())
        )
        if for_update:
            stmt = stmt.with_for_update()
        return await self.db.scalar(stmt)

    async def has_recent_code(self, *, user_id: int, purpose: str, cooldown_seconds: int = 60) -> bool:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=cooldown_seconds)
        stmt = (
            select(EmailVerificationCode.id)
            .where(
                EmailVerificationCode.user_id == user_id,
                EmailVerificationCode.purpose == purpose,
                EmailVerificationCode.created_at >= cutoff,
            )
            .limit(1)
        )
        return (await self.db.scalar(stmt)) is not None

    async def consume_code(self, code: EmailVerificationCode) -> None:
        code.consumed_at = datetime.now(timezone.utc)
        self.db.add(code)

    async def revoke_user_codes(self, *, user_id: int, purpose: str) -> None:
        await self.db.execute(
            update(EmailVerificationCode)
            .where(EmailVerificationCode.user_id == user_id, EmailVerificationCode.purpose == purpose, EmailVerificationCode.consumed_at.is_(None))
            .values(consumed_at=datetime.now(timezone.utc))
        )

    async def add_refresh_token(self, *, user_id: int, token_hash: str, expires_at: datetime) -> RefreshToken:
        token = RefreshToken(user_id=user_id, token_hash=token_hash, expires_at=expires_at)
        self.db.add(token)
        await self.db.flush()
        return token

    async def get_refresh_token(self, token_hash: str) -> RefreshToken | None:
        return await self.db.scalar(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash).with_for_update()
        )

    async def revoke_refresh_token(self, token: RefreshToken) -> None:
        token.revoked_at = datetime.now(timezone.utc)
        self.db.add(token)

    async def revoke_all_refresh_tokens(self, user_id: int) -> None:
        await self.db.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(timezone.utc))
        )
