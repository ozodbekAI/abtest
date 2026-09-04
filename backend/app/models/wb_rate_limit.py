from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class WBApiRateLimit(Base):
    """Distributed reservation state for seller-scoped WB API limits."""

    __tablename__ = "wb_api_rate_limits"

    scope: Mapped[str] = mapped_column(String(128), primary_key=True)
    next_allowed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
