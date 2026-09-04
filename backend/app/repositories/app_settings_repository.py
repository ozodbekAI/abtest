from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.app_setting import AppSetting


class AppSettingsRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get(self, key: str) -> AppSetting | None:
        return await self.db.scalar(select(AppSetting).where(AppSetting.key == key))

    async def get_bool(self, key: str, *, default: bool) -> bool:
        setting = await self.get(key)
        if not setting or not isinstance(setting.value, dict):
            return default
        value = setting.value.get("value", default)
        return value if isinstance(value, bool) else default

    async def set_bool(self, key: str, value: bool) -> AppSetting:
        setting = await self.get(key)
        if not setting:
            setting = AppSetting(key=key, value={"value": value})
            self.db.add(setting)
        else:
            setting.value = {"value": value}
        await self.db.flush()
        return setting
