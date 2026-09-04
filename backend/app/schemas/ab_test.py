from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class ABTestCreateRequest(BaseModel):
    connection_id: int = Field(gt=0)
    nm_id: int = Field(gt=0)
    title: str = Field(min_length=2, max_length=512)
    skip_current_photo: bool = True
    keep_winner_as_main: bool = True
    delete_test_media: bool = True
    views_per_variant: int = Field(default=1000, ge=300, le=1_000_000)
    cpm_rub: int = Field(default=300, ge=1, le=1_000_000)
    budget_rub: int = Field(default=1000, ge=1, le=100_000_000)
    # The A/B workflow intentionally creates unified campaigns. Keep legacy
    # placement values accepted for old clients, but normalize them to the
    # single unified placement used by WB.
    placement: str = Field(default="combined", pattern="^(combined|search|recommendations)$")

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("placement")
    @classmethod
    def normalize_unified_placement(cls, value: str) -> str:
        return "combined"


class ABTestStartRequest(BaseModel):
    auto_deposit: bool = False
    deposit_rub: int | None = Field(default=None, ge=1, le=100_000_000)
    funding_source: Literal["auto", "account", "mutual", "bonus"] = "auto"


class ABTestVariantSourceRequest(BaseModel):
    source_url: str = Field(min_length=10, max_length=2048)


class ABTestImagePreviewResponse(BaseModel):
    image_url: str
    file_name: str
    width: int
    height: int


class ABTestVariantResponse(BaseModel):
    id: int
    position: int
    source_type: str
    file_name: str | None = None
    image_url: str | None = None
    source_url: str | None = None
    wb_url: str | None = None
    views: int
    clicks: int
    ctr: float
    spend_rub: float
    is_winner: bool
    orders: int = 0
    cpo: float | None = None


class ABTestResponse(BaseModel):
    id: int
    connection_id: int
    store_name: str
    nm_id: int
    title: str
    status: str
    wb_campaign_id: int | None = None
    bid_type: str = "unified"
    skip_current_photo: bool
    keep_winner_as_main: bool
    delete_test_media: bool
    views_per_variant: int
    cpm_rub: int
    budget_rub: int
    placement: str
    current_variant_order: int
    winner_variant_order: int | None = None
    winner_decision: str | None = None
    operation_state: str = "ready"
    campaign_state: str = "not_created"
    media_status: str = "original"
    stats_quality: str = "not_started"
    incident_id: str | None = None
    unallocated_views: int = 0
    unallocated_clicks: int = 0
    unallocated_spend_rub: float = 0
    total_views: int
    total_clicks: int
    total_spend_rub: float
    total_ctr: float = 0
    total_orders: int = 0
    total_cpo: float | None = None
    last_error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    last_synced_at: datetime | None = None
    created_at: datetime
    variants: list[ABTestVariantResponse] = Field(default_factory=list)


class ABTestCardResponse(BaseModel):
    nm_id: int
    vendor_code: str | None = None
    title: str | None = None
    brand: str | None = None
    main_photo_url: str | None = None
    photos: list[str] = Field(default_factory=list)


class ABTestCardsPageResponse(BaseModel):
    items: list[ABTestCardResponse] = Field(default_factory=list)
    next_cursor: dict[str, Any] | None = None
    total: int | None = None


class ABTestListResponse(BaseModel):
    items: list[ABTestResponse]
    total: int
