from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class WBTokenRequest(BaseModel):
    token: str = Field(min_length=20, max_length=4096)
    store_name: str = Field(default="", max_length=120)

    @field_validator("store_name")
    @classmethod
    def strip_store_name(cls, value: str) -> str:
        return value.strip()


class WBConnectionCreateRequest(WBTokenRequest):
    pass


class WBPingResult(BaseModel):
    category: str
    status: str
    http_status: int | None = None
    message: str | None = None


class WBConnectionResponse(BaseModel):
    id: int
    store_name: str
    connected: bool
    status: str
    token_last4: str = ""
    ready_for_ab_tests: bool
    access: dict[str, bool]
    pings: dict[str, Any]
    last_validated_at: datetime | None = None


class WBPromotionCashback(BaseModel):
    sum: float = 0
    percent: int = 0
    expiration_date: str | None = None


class WBPromotionBalanceResponse(BaseModel):
    connection_id: int
    account_balance: float = 0
    mutual_balance: float = 0
    promo_bonus_balance: float = 0
    cashbacks: list[WBPromotionCashback] = Field(default_factory=list)
    fetched_at: datetime
