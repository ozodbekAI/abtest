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
    wb_common_api_url: str = "https://common-api.wildberries.ru"
    wb_advert_api_url: str = "https://advert-api.wildberries.ru"
    wb_analytics_api_url: str = "https://seller-analytics-api.wildberries.ru"
    wb_statistics_api_url: str = "https://statistics-api.wildberries.ru"
    wb_request_timeout: float = 15.0
    # Real WB mutations are disabled unless explicitly enabled. Loopback
    # providers remain available for deterministic synthetic audit runs.
    wb_allow_external_requests: bool = False
    media_root: str = "./media"
    media_signing_secret: str = ""
    media_url_expire_sec: int = 3600
    ab_test_scheduler_interval_sec: int = 60
    ab_test_no_progress_timeout_sec: int = 172800
    # Stage reconciliation contract (Sergey 2026-09-28): poll every 3 min,
    # settle after >=10 min and three consecutive identical snapshots,
    # hard-stop the reconciliation wait at 30 min.
    ab_test_stats_settle_attempts: int = 3  # legacy compatibility; no longer the primary control
    ab_test_stats_settle_delay_sec: int = 180  # 3 minutes
    ab_test_stats_min_settle_sec: int = 600  # 10 minutes
    ab_test_stats_max_settle_sec: int = 1800  # 30 minutes
    ab_test_stats_stable_snapshots: int = 3
    ab_test_stats_transition_window_sec: int = 180  # first 3 minutes of a new stage stay unallocated
    ab_test_winner_confidence: float = 0.95
    ab_test_budget_guard_reserve_rub: int = 300
    ab_test_budget_guard_pending_lag_sec: int = 300  # reserve up to 5 min of delayed provider spend
    ab_test_scheduler_per_test_timeout_sec: int = 2400
    ab_test_scheduler_concurrency: int = 4
    ab_test_max_budget_rub: int = 1_000_000_000

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
        if self.app_env == "production":
            if not self.wb_allow_external_requests:
                raise ValueError("WB_ALLOW_EXTERNAL_REQUESTS must be true in production")
            for name in ("wb_content_api_url", "wb_common_api_url", "wb_advert_api_url", "wb_analytics_api_url", "wb_statistics_api_url"):
                value = str(getattr(self, name)).lower()
                if any(host in value for host in ("localhost", "127.0.0.1", "0.0.0.0", "::1")):
                    raise ValueError(f"{name} must point to a non-loopback provider in production")
        elif self.app_env in {"test", "testing", "ci"} and self.wb_allow_external_requests:
            raise ValueError("WB_ALLOW_EXTERNAL_REQUESTS must remain false in test/CI environments")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
