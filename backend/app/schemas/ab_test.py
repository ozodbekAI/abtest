from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class ABTestCreateRequest(BaseModel):
    connection_id: int = Field(gt=0)
    nm_id: int = Field(gt=0)
    title: str = Field(min_length=2, max_length=512)
    skip_current_photo: bool = True
    keep_winner_as_main: bool = False
    delete_test_media: bool = True
    views_per_variant: int = Field(default=1000, ge=300, le=2_147_483_647)
    cpm_rub: int = Field(default=300, ge=1, le=2_147_483_647)
    budget_rub: int = Field(default=1000, ge=1, le=2_147_483_647)
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
    # SHA-256 fingerprint of the draft the user explicitly reviewed.
    # The server rejects stale/mutated drafts before any external WB mutation.
    draft_fingerprint: str = Field(min_length=64, max_length=64, pattern="^[0-9a-f]{64}$")
    auto_deposit: bool = False
    deposit_rub: int | None = Field(default=None, ge=1, le=100_000_000)
    funding_source: Literal["auto", "account", "mutual", "bonus"] = "auto"
    # Required only when resuming a running test paused because WB raised its
    # minimum CPM. It is intentionally separate from the draft's old CPM.
    confirmed_cpm: int | None = Field(default=None, ge=1, le=100_000_000)


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
    ctr: float | None = None
    spend_rub: float
    is_winner: bool
    attribution_quality: str = "unverified"
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
    winner_p_value: float | None = None
    winner_confidence: float = 0.95
    winner_worst_case_safe: bool | None = None
    stats_reconciliation_status: str | None = None
    stats_reconciliation_elapsed_sec: int = 0
    stats_reconciliation_stable_snapshots: int = 0
    operation_state: str = "ready"
    campaign_state: str = "not_created"
    media_status: str = "original"
    stats_quality: str = "not_started"
    incident_id: str | None = None
    unallocated_views: int = 0
    unallocated_clicks: int = 0
    unallocated_spend_rub: float = 0
    funding_source: str = "auto"
    stage_views: int = 0
    stage_clicks: int = 0
    stage_spend_rub: float = 0
    total_views: int
    total_clicks: int
    total_spend_rub: float
    total_ctr: float | None = None
    total_orders: int = 0
    total_cpo: float | None = None
    last_error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    last_synced_at: datetime | None = None
    created_at: datetime
    draft_fingerprint: str
    start_confirmation_fingerprint: str
    stage_exposure_views: int = 0
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
