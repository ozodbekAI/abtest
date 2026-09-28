from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.ab_test import ABTest, ABTestAuditEvent, ABTestBudgetEntry, ABTestIncidentNotification, ABTestOperation, ABTestStatus, ABTestVariant
from app.models.user import User
from app.models.wb_connection import WBConnection


class ABTestRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_connection(self, user_id: int, connection_id: int) -> WBConnection | None:
        return await self.db.scalar(
            select(WBConnection).where(WBConnection.id == connection_id, WBConnection.user_id == user_id)
        )

    async def get_for_user(self, user_id: int, test_id: int) -> ABTest | None:
        return await self.db.scalar(
            select(ABTest)
            .options(selectinload(ABTest.variants), selectinload(ABTest.connection))
            .where(ABTest.id == test_id, ABTest.user_id == user_id)
        )

    async def get_for_update(self, user_id: int, test_id: int) -> ABTest | None:
        """Load a test with a row lock for state-changing A/B operations."""
        query = (
            select(ABTest)
            .options(selectinload(ABTest.variants), selectinload(ABTest.connection))
            .where(ABTest.id == test_id, ABTest.user_id == user_id)
            .with_for_update(of=ABTest)
            .execution_options(populate_existing=True)
        )
        return await self.db.scalar(query)

    async def list_for_user(
        self, user_id: int, *, connection_id: int | None = None, status: ABTestStatus | None = None
    ) -> list[ABTest]:
        query = (
            select(ABTest)
            .options(selectinload(ABTest.variants), selectinload(ABTest.connection))
            .where(ABTest.user_id == user_id)
            .order_by(ABTest.created_at.desc(), ABTest.id.desc())
        )
        if connection_id is not None:
            query = query.where(ABTest.connection_id == connection_id)
        if status is not None:
            query = query.where(ABTest.status == status)
        return list((await self.db.scalars(query)).all())

    async def list_running(self) -> list[ABTest]:
        query = (
            select(ABTest)
            .options(selectinload(ABTest.variants), selectinload(ABTest.connection))
            .where(ABTest.status == ABTestStatus.RUNNING)
            .order_by(ABTest.id)
        )
        return list((await self.db.scalars(query)).all())

    async def list_unresolved_campaigns(self) -> list[ABTest]:
        """Testlar statusi RUNNING bo'lmasa ham, kampaniyasi hali yopilmagan
        (WB'da faol/holati noaniq) qolganlarni qaytaradi.

        Bu ``list_running`` bilan bir xil emas: bir marta pauza/stop
        tasdiqlanmay ``FAILED`` deb belgilangan test odatiy planировщик
        aylanishidan chiqib qoladi, lekin uning tashqi reklama kampaniyasi
        hali ham faol bo'lishi mumkin. Shu testlarni topib, kampaniyani
        yopishga qayta-qayta urinish uchun ishlatiladi.
        """
        query = (
            select(ABTest)
            .options(selectinload(ABTest.variants), selectinload(ABTest.connection))
            .where(
                ABTest.status != ABTestStatus.RUNNING,
                ABTest.wb_campaign_id.isnot(None),
                or_(
                    ABTest.campaign_state.in_(
                        ["running", "starting", "unknown", "pause_requested", "stop_requested", "created", "paused"]
                    ),
                    # A stopped campaign with an unrestored card is still owed a restore.
                    and_(
                        ABTest.status == ABTestStatus.FAILED,
                        ABTest.lifecycle_lock.is_(True),
                        ABTest.media_status.notin_(["restored", "winner_applied", "original", "external_conflict"]),
                    ),
                ),
            )
            .order_by(ABTest.id)
        )
        return list((await self.db.scalars(query)).all())

    async def get_running_for_user_card(self, user_id: int, nm_id: int, *, exclude_test_id: int | None = None) -> ABTest | None:
        query = select(ABTest).where(
            ABTest.user_id == user_id,
            ABTest.nm_id == nm_id,
            ABTest.status == ABTestStatus.RUNNING,
        )
        if exclude_test_id is not None:
            query = query.where(ABTest.id != exclude_test_id)
        return await self.db.scalar(query)

    async def get_open_for_store_card(
        self, store_fingerprint: str, nm_id: int, *, exclude_test_id: int | None = None
    ) -> ABTest | None:
        if not store_fingerprint:
            return None
        query = select(ABTest).where(
            ABTest.store_fingerprint == store_fingerprint,
            ABTest.nm_id == nm_id,
            ABTest.lifecycle_lock.is_(True),
        )
        if exclude_test_id is not None:
            query = query.where(ABTest.id != exclude_test_id)
        return await self.db.scalar(query)

    async def get_unresolved_for_seller_card(
        self,
        token_fingerprint: str,
        nm_id: int,
        *,
        exclude_test_id: int | None = None,
    ) -> ABTest | None:
        """Bir xil WB do'kon (token) uchun, qaysi ilova foydalanuvchisi
        ulaganidan qat'i nazar, yopilmagan test bor-yo'qligini tekshiradi.

        ``get_running_for_user_card`` faqat ``user_id`` bo'yicha bloklaydi —
        egasi va menejer alohida foydalanuvchi bo'lib bitta do'konni ulasa,
        u orqali o'tib ketadi. Bu metod ``wb_connections.token_fingerprint``
        orqali xuddi shu WB tokenidan (demak, xuddi shu do'kondan)
        foydalanadigan barcha ulanishlarni hisobga oladi.
        """
        if not token_fingerprint:
            return None
        query = (
            select(ABTest)
            .join(WBConnection, ABTest.connection_id == WBConnection.id)
            .where(
                WBConnection.token_fingerprint == token_fingerprint,
                ABTest.nm_id == nm_id,
                ABTest.lifecycle_lock.is_(True),
            )
        )
        if exclude_test_id is not None:
            query = query.where(ABTest.id != exclude_test_id)
        return await self.db.scalar(query)

    async def get_operation(self, operation_key: str) -> ABTestOperation | None:
        return await self.db.scalar(
            select(ABTestOperation).where(ABTestOperation.operation_key == operation_key)
        )

    async def get_latest_operation(self, test_id: int) -> ABTestOperation | None:
        return await self.db.scalar(
            select(ABTestOperation)
            .where(ABTestOperation.test_id == test_id)
            .order_by(ABTestOperation.id.desc())
        )

    async def list_operations(self, test_id: int) -> list[ABTestOperation]:
        result = await self.db.scalars(
            select(ABTestOperation)
            .where(ABTestOperation.test_id == test_id)
            .order_by(ABTestOperation.id.desc())
        )
        return list(result.all())

    async def create_operation(self, **values) -> ABTestOperation:
        operation = ABTestOperation(**values)
        self.db.add(operation)
        await self.db.flush()
        return operation

    async def add_audit_event(self, **values) -> ABTestAuditEvent:
        event = ABTestAuditEvent(**values)
        self.db.add(event)
        await self.db.flush()
        return event

    async def archive_audit_events(self, test_id: int) -> None:
        # Keep immutable evidence even though the live ABTest graph uses
        # delete-orphan/cascade semantics. The archive table has no FK to the
        # mutable test row and therefore survives lifecycle deletion.
        from sqlalchemy import text
        if self.db.bind is not None and self.db.bind.dialect.name == "sqlite":
            await self.db.execute(text(
                """
                INSERT INTO ab_test_audit_event_archive
                    (original_event_id, test_id, user_id, nm_id, connection_id, action, before_state, after_state, details, created_at)
                SELECT id, test_id, user_id, nm_id, connection_id, action, before_state, after_state, details, created_at
                FROM ab_test_audit_events WHERE test_id = :test_id
                """), {"test_id": test_id})
        else:
            await self.db.execute(text(
                """
                INSERT INTO ab_test_audit_event_archive
                    (original_event_id, test_id, user_id, nm_id, connection_id, action, before_state, after_state, details, created_at)
                SELECT id, test_id, user_id, nm_id, connection_id, action, before_state, after_state, details, created_at
                FROM ab_test_audit_events WHERE test_id = :test_id
                """), {"test_id": test_id})
        await self.db.flush()

    async def archive_audit_events_for_user(self, user_id: int) -> None:
        """Archive all mutable audit rows before a user cascade can remove them."""
        from sqlalchemy import text
        await self.db.execute(text(
            """
            INSERT INTO ab_test_audit_event_archive
                (original_event_id, test_id, user_id, nm_id, connection_id, action, before_state, after_state, details, created_at)
            SELECT id, test_id, user_id, nm_id, connection_id, action, before_state, after_state, details, created_at
            FROM ab_test_audit_events WHERE user_id = :user_id
            AND NOT EXISTS (
                SELECT 1 FROM ab_test_audit_event_archive a WHERE a.original_event_id = ab_test_audit_events.id
            )
            """
        ), {"user_id": user_id})
        await self.db.flush()

    async def list_audit_archive(self, test_id: int) -> list[dict]:
        from sqlalchemy import text
        result = await self.db.execute(
            text("SELECT original_event_id, test_id, user_id, nm_id, connection_id, action, before_state, after_state, details, created_at FROM ab_test_audit_event_archive WHERE test_id=:test_id ORDER BY id ASC"),
            {"test_id": test_id},
        )
        return [dict(row._mapping) for row in result.fetchall()]

    async def list_audit_events(self, test_id: int) -> list[ABTestAuditEvent]:
        result = await self.db.scalars(
            select(ABTestAuditEvent).where(ABTestAuditEvent.test_id == test_id).order_by(ABTestAuditEvent.id.asc())
        )
        return list(result.all())

    async def list_reconciliation_operations(self) -> list[ABTestOperation]:
        result = await self.db.scalars(
            select(ABTestOperation)
            .where(ABTestOperation.status.in_(("PREPARED", "IN_PROGRESS", "RECONCILIATION_REQUIRED")))
            .order_by(ABTestOperation.id)
        )
        return list(result.all())

    async def count_for_user(self, user_id: int) -> int:
        return int(await self.db.scalar(select(func.count(ABTest.id)).where(ABTest.user_id == user_id)) or 0)

    async def create(self, **values) -> ABTest:
        test = ABTest(**values)
        self.db.add(test)
        await self.db.flush()
        return test

    async def add_variant(self, **values) -> ABTestVariant:
        variant = ABTestVariant(**values)
        self.db.add(variant)
        await self.db.flush()
        return variant

    async def get_variant(self, test_id: int, position: int) -> ABTestVariant | None:
        return await self.db.scalar(
            select(ABTestVariant).where(ABTestVariant.test_id == test_id, ABTestVariant.position == position)
        )

    async def delete_variant(self, variant: ABTestVariant) -> None:
        await self.db.delete(variant)

    async def get_incident_notification(self, test_id: int, incident_id: str) -> ABTestIncidentNotification | None:
        return await self.db.scalar(
            select(ABTestIncidentNotification).where(
                ABTestIncidentNotification.test_id == test_id,
                ABTestIncidentNotification.incident_id == incident_id,
            )
        )

    async def enqueue_incident_notification(self, test: ABTest) -> ABTestIncidentNotification | None:
        incident_id = str(test.incident_id or "").strip()
        if not incident_id:
            return None
        existing = await self.get_incident_notification(test.id, incident_id)
        if existing:
            return existing
        recipient = await self.db.scalar(select(User.email).where(User.id == test.user_id))
        if not recipient:
            return None
        body = (
            f"Магазин: {getattr(test.connection, 'store_name', '')}\n"
            f"Товар: {test.nm_id}\n"
            f"Кампания WB: {test.wb_campaign_id or 'не создана'}\n"
            f"Состояние кампании: {test.campaign_state}\n"
            f"Состояние фото: {test.media_status}\n"
            f"Последняя ошибка: {test.last_error or 'требуется сверка'}\n\n"
            "Откройте тест в AVEMOD, проверьте кампанию и карточку в кабинете WB, затем выполните сверку."
        )
        row = ABTestIncidentNotification(
            test_id=test.id,
            user_id=test.user_id,
            recipient=str(recipient),
            incident_id=incident_id,
            subject=f"AVEMOD: требуется сверка инцидента {incident_id}",
            body=body[:10000],
            status="queued",
        )
        self.db.add(row)
        await self.db.flush()
        return row

    async def list_pending_incident_notifications(self, *, limit: int = 20) -> list[ABTestIncidentNotification]:
        from sqlalchemy import or_
        now = datetime.now(timezone.utc)
        result = await self.db.scalars(
            select(ABTestIncidentNotification)
            .where(
                ABTestIncidentNotification.status.in_(("queued", "failed")),
                ABTestIncidentNotification.next_attempt_at <= now,
            )
            .order_by(ABTestIncidentNotification.id.asc())
            .limit(limit)
        )
        return list(result.all())

    async def add_budget_entry(self, **values) -> ABTestBudgetEntry:
        row = ABTestBudgetEntry(**values)
        self.db.add(row)
        await self.db.flush()
        return row
