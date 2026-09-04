from functools import lru_cache

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "WB Optimizer"
    app_env: str = "development"
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/wb_optimizer"
    auto_create_tables: bool = True

    jwt_secret_key: str = "change-this-in-production"
    fernet_key: str = ""
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 30
    admin_email: str = ""
    admin_password: str = ""

    frontend_url: str = "http://localhost:5173"
    cors_origins: str = "http://localhost:5173"

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    email_from: str = "noreply@wb-optimizer.local"

    wb_content_api_url: str = "https://content-api.wildberries.ru"
    wb_advert_api_url: str = "https://advert-api.wildberries.ru"
    wb_analytics_api_url: str = "https://seller-analytics-api.wildberries.ru"
    wb_statistics_api_url: str = "https://statistics-api.wildberries.ru"
    wb_request_timeout: float = 15.0
    media_root: str = "./media"
    media_signing_secret: str = ""
    media_url_expire_sec: int = 3600
    ab_test_scheduler_interval_sec: int = 60

    @field_validator("cors_origins", mode="before")
    @classmethod
    def normalize_cors_origins(cls, value: object) -> str:
        if isinstance(value, list):
            return ",".join(str(item) for item in value)
        return str(value or "")

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @model_validator(mode="after")
    def validate_production_secrets(self):
        if self.app_env == "production" and self.jwt_secret_key in {"", "change-this-in-production"}:
            raise ValueError("JWT_SECRET_KEY must be configured in production")
        if self.app_env == "production" and not self.fernet_key.strip():
            raise ValueError("FERNET_KEY must be configured in production")
        if self.app_env == "production" and not self.media_signing_secret.strip():
            raise ValueError("MEDIA_SIGNING_SECRET must be configured in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
