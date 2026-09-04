from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class WBConnection(Base):
    __tablename__ = "wb_connections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    store_name: Mapped[str] = mapped_column(String(120), default="Магазин", server_default="Магазин")
    token_encrypted: Mapped[str] = mapped_column(Text)
    token_last4: Mapped[str] = mapped_column(String(4), default="")
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    content_access: Mapped[bool] = mapped_column(Boolean, default=False)
    promotion_access: Mapped[bool] = mapped_column(Boolean, default=False)
    analytics_access: Mapped[bool] = mapped_column(Boolean, default=False)
    statistics_access: Mapped[bool] = mapped_column(Boolean, default=False)
    ready_for_ab_tests: Mapped[bool] = mapped_column(Boolean, default=False)
    ping_results: Mapped[dict] = mapped_column(JSON, default=dict)
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="wb_connections")
