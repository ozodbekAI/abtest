from datetime import date, datetime

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator


class AdminUserItem(BaseModel):
    id: int
    email: EmailStr
    first_name: str
    last_name: str
    is_verified: bool
    is_active: bool
    is_admin: bool
    stores_count: int
    tests_count: int
    created_at: datetime


class AdminUserListResponse(BaseModel):
    items: list[AdminUserItem]
    total: int
    page: int
    page_size: int


class AdminUserCreateRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    confirm_password: str = Field(min_length=8, max_length=128)
    first_name: str = Field(min_length=2, max_length=80)
    last_name: str = Field(default="", max_length=80)
    is_verified: bool = True
    is_active: bool = True
    is_admin: bool = False

    @model_validator(mode="after")
    def passwords_must_match(self):
        if self.password != self.confirm_password:
            raise ValueError("Пароли не совпадают")
        return self

    @field_validator("first_name", "last_name")
    @classmethod
    def normalize_names(cls, value: str, info):
        value = value.strip()
        if info.field_name == "first_name" and len(value) < 2:
            raise ValueError("Имя должно содержать минимум 2 символа")
        if info.field_name == "last_name" and value and len(value) < 2:
            raise ValueError("Фамилия должна содержать минимум 2 символа")
        return value


class AdminUserUpdateRequest(BaseModel):
    email: EmailStr | None = None
    first_name: str | None = Field(default=None, min_length=2, max_length=80)
    last_name: str | None = Field(default=None, max_length=80)
    is_verified: bool | None = None
    is_active: bool | None = None
    is_admin: bool | None = None

    @field_validator("first_name", "last_name")
    @classmethod
    def normalize_names(cls, value: str | None, info):
        if value is None:
            return value
        value = value.strip()
        if info.field_name == "first_name" and len(value) < 2:
            raise ValueError("Имя должно содержать минимум 2 символа")
        if info.field_name == "last_name" and value and len(value) < 2:
            raise ValueError("Фамилия должна содержать минимум 2 символа")
        return value


class AdminPasswordRequest(BaseModel):
    password: str = Field(min_length=8, max_length=128)
    confirm_password: str = Field(min_length=8, max_length=128)

    @model_validator(mode="after")
    def passwords_must_match(self):
        if self.password != self.confirm_password:
            raise ValueError("Пароли не совпадают")
        return self


class AdminStoreItem(BaseModel):
    id: int
    user_id: int
    owner_email: EmailStr
    owner_name: str
    store_name: str
    status: str
    ready_for_ab_tests: bool
    token_last4: str
    tests_count: int
    last_validated_at: datetime | None = None
    created_at: datetime


class AdminTestItem(BaseModel):
    id: int
    connection_id: int
    store_name: str
    user_id: int
    nm_id: int
    title: str
    status: str
    wb_campaign_id: int | None = None
    total_views: int
    total_clicks: int
    total_orders: int
    total_spend_rub: float
    total_ctr: float
    last_error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime


class AdminUserDetail(BaseModel):
    user: AdminUserItem
    stores: list[AdminStoreItem]
    tests: list[AdminTestItem]


class AdminStoreDetail(BaseModel):
    store: AdminStoreItem
    tests: list[AdminTestItem]


class AdminRegistrationPoint(BaseModel):
    date: date
    count: int


class AdminDashboardResponse(BaseModel):
    users_count: int
    active_users_count: int
    verified_users_count: int
    stores_count: int
    tests_count: int
    running_tests_count: int
    finished_tests_count: int
    failed_tests_count: int
    views: int
    clicks: int
    orders: int
    spend_rub: float
    registrations: list[AdminRegistrationPoint]


class AdminSettingsResponse(BaseModel):
    registration_enabled: bool


class AdminSettingsUpdateRequest(BaseModel):
    registration_enabled: bool
