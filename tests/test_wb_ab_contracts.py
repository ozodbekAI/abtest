from __future__ import annotations

import asyncio
import hashlib
import io
import struct

import pytest
from fastapi import HTTPException

from app.models.ab_test import ABTest, ABTestOperation, ABTestOperationStatus, ABTestStatus, ABTestVariant
from app.services.ab_test_service import ABTestReconciliationRequired, ABTestService
from app.services.wb_content_client import WBContentClient
from app.services.wb_promotion_client import WBApiError, WBPromotionClient
from app.services.ab_test_budget import calculate_required_budget


def make_test(*rows: tuple[int, int]) -> ABTest:
    test = ABTest()
    test.variants = [ABTestVariant(position=position, views=views, clicks=clicks) for position, views, clicks in rows]
    return test


def test_winner_requires_a_real_sample() -> None:
    test = make_test((1, 299, 20), (2, 299, 30))
    winner, decision = ABTestService._winner_result(test)
    assert winner is None
    assert decision == "insufficient_data"


def test_winner_rejects_a_tie() -> None:
    test = make_test((1, 500, 20), (2, 500, 21))
    winner, decision = ABTestService._winner_result(test)
    assert winner is None
    assert decision == "no_clear_winner"


def test_winner_selects_a_clear_variant() -> None:
    test = make_test((1, 500, 10), (2, 500, 50))
    winner, decision = ABTestService._winner_result(test)
    assert winner is not None
    assert winner.position == 2
    assert decision == "winner_found"


def test_content_page_preserves_wb_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    client = WBContentClient("token")

    async def fake_request(*args, **kwargs):
        return {
            "cards": [{"nmID": 123, "title": "Товар", "photos": [{"big": "https://cdn.example/1.jpg"}]}],
            "cursor": {"updatedAt": "2026-09-03T00:00:00Z", "nmID": 123, "total": 1},
        }

    monkeypatch.setattr(client, "_request", fake_request)

    async def run():
        return await client.list_cards_page(search="", limit=100)

    cards, cursor = asyncio.run(run())
    assert cards[0]["nm_id"] == 123
    assert cursor == {"updatedAt": "2026-09-03T00:00:00Z", "nmID": 123, "total": 1}


def test_content_rejects_small_png() -> None:
    data = bytearray(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    data[16:24] = struct.pack(">II", 699, 900)
    with pytest.raises(Exception, match="700"):
        ABTestService._validate_image("variant.png", "image/png", bytes(data))


def test_tiff_is_converted_to_wb_compatible_jpeg() -> None:
    from PIL import Image

    source = Image.new("RGB", (700, 900), (32, 96, 192))
    raw = io.BytesIO()
    source.save(raw, format="TIFF")

    filename, content_type, converted = ABTestService._prepare_image(
        "creative.tiff", "image/tiff", raw.getvalue()
    )

    assert filename == "creative.jpg"
    assert content_type == "image/jpeg"
    assert converted.startswith(b"\xff\xd8\xff")
    assert ABTestService._image_dimensions(converted) == (700, 900)


def _valid_png(marker: bytes = b"") -> bytes:
    data = bytearray(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16 + marker)
    data[16:24] = struct.pack(">II", 700, 900)
    return bytes(data)


def test_apply_variant_uses_upload_success_without_card_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _valid_png(b"original")
    replacement = _valid_png(b"replacement")

    class FakeContent:
        def __init__(self) -> None:
            self.upload_calls: list[dict[str, object]] = []

        async def upload_media_file(self, **kwargs):
            self.upload_calls.append(kwargs)

    async def fake_variant_bytes(self, test, variant, content):
        return replacement, "image/png", "replacement.png"

    monkeypatch.setattr(ABTestService, "_variant_bytes", fake_variant_bytes)

    service = ABTestService(None)  # type: ignore[arg-type]
    test = ABTest(id=1, nm_id=123)
    variant = ABTestVariant(position=1, source_type="upload")
    content = FakeContent()

    asyncio.run(service._apply_variant(test, variant, content))

    assert len(content.upload_calls) == 1
    assert content.upload_calls[0]["photo_number"] == 1
    assert variant.wb_url is None


def test_apply_variant_accepts_successful_upload_without_media_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _valid_png(b"original")
    replacement = _valid_png(b"replacement")

    class FakeContent:
        async def upload_media_file(self, **kwargs):
            # A successful upload is accepted without asking the card API for
            # a stale CDN snapshot.
            return None

    async def fake_variant_bytes(self, test, variant, content):
        return replacement, "image/png", "replacement.png"

    monkeypatch.setattr(ABTestService, "_variant_bytes", fake_variant_bytes)

    service = ABTestService(None)  # type: ignore[arg-type]
    test = ABTest(id=2, nm_id=456)
    variant = ABTestVariant(position=1, source_type="upload")

    asyncio.run(service._apply_variant(test, variant, FakeContent()))
    assert variant.wb_url is None


def test_pending_variant_recovery_replays_local_shadow_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    first = _valid_png(b"first")
    second = _valid_png(b"second")
    shadow_dir = tmp_path / "ab_tests" / "58" / "current"
    shadow_dir.mkdir(parents=True)
    (shadow_dir / "slot_1.jpg").write_bytes(second)
    (shadow_dir / "slot_2.jpg").write_bytes(first)

    class FakeContent:
        def __init__(self) -> None:
            self.upload_calls: list[dict[str, object]] = []

        async def upload_media_file(self, **kwargs):
            self.upload_calls.append(kwargs)

    state = {
        "version": 1,
        "slot_count": 2,
        "pending": {
            "kind": "swap",
            "variant_position": 2,
            "slots": [1, 2],
            "targets": {
                "1": {"shadow_path": "ab_tests/58/current/slot_1.jpg", "mime": "image/jpeg"},
                "2": {"shadow_path": "ab_tests/58/current/slot_2.jpg", "mime": "image/jpeg"},
            },
        },
    }
    test = ABTest(id=58, nm_id=123, current_variant_order=1)
    test.media_state = state

    service = ABTestService(None)  # type: ignore[arg-type]
    monkeypatch.setattr(ABTestService, "_media_root", staticmethod(lambda: tmp_path))
    content = FakeContent()
    result = asyncio.run(service._recover_pending_variant_media(test, content))

    assert result is True
    assert [call["photo_number"] for call in content.upload_calls] == [2, 1]
    assert test.media_state["pending"] is None
    assert test.current_variant_order == 2
    assert test.media_status == "variant_applied"


def test_card_variant_swaps_main_and_source_slot_and_restores_exactly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    original_main = _valid_png(b"main")
    original_second = _valid_png(b"second")

    class FakeContent:
        def __init__(self) -> None:
            self.urls = ["https://cdn.example/slot-1.jpg", "https://cdn.example/slot-2.jpg"]
            self.payloads = [original_main, original_second]
            self.upload_calls: list[dict[str, object]] = []

        async def get_card(self, nm_id: int):
            return {"photos": list(self.urls)}

        async def download_image(self, url: str, *, cache_bust: bool = False):
            index = self.urls.index(url.split("?", 1)[0])
            return self.payloads[index], "image/png"

        async def upload_media_file(self, **kwargs):
            self.upload_calls.append(kwargs)
            self.payloads[int(kwargs["photo_number"]) - 1] = kwargs["content"]

    async def run() -> None:
        content = FakeContent()
        service = ABTestService(None)  # type: ignore[arg-type]
        test = ABTest(id=10, nm_id=123)
        variant = ABTestVariant(
            position=1,
            source_type="card",
            source_url="https://cdn.example/slot-2.jpg",
            file_name="slot-2.jpg",
        )
        await service._initialize_media_state(test, list(content.urls), [variant], content)
        await service._apply_variant(test, variant, content)

        assert [call["photo_number"] for call in content.upload_calls] == [2, 1]
        assert content.payloads == [original_second, original_main]
        assert variant.wb_url is None
        assert test.media_state["touched"] == [1, 2]

        await service._restore_original(test, content)
        assert [call["photo_number"] for call in content.upload_calls[-2:]] == [2, 1]
        assert content.payloads == [original_main, original_second]
        assert test.media_state["touched"] == []

    original_sleep = asyncio.sleep
    monkeypatch.setattr("app.services.ab_test_service.asyncio.sleep", lambda _: original_sleep(0))
    monkeypatch.setattr("app.services.ab_test_service.ABTestService._media_root", staticmethod(lambda: tmp_path))
    asyncio.run(run())


def test_custom_variant_uses_parking_slot_before_main_and_restores(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    original_payloads = [_valid_png(b"main"), _valid_png(b"source"), _valid_png(b"parking")]
    replacement = _valid_png(b"uploaded")

    class FakeContent:
        def __init__(self) -> None:
            self.urls = [
                "https://cdn.example/slot-1.jpg",
                "https://cdn.example/slot-2.jpg",
                "https://cdn.example/slot-3.jpg",
            ]
            self.payloads = list(original_payloads)
            self.upload_calls: list[dict[str, object]] = []

        async def get_card(self, nm_id: int):
            return {"photos": list(self.urls)}

        async def download_image(self, url: str, *, cache_bust: bool = False):
            index = self.urls.index(url.split("?", 1)[0])
            return self.payloads[index], "image/png"

        async def upload_media_file(self, **kwargs):
            self.upload_calls.append(kwargs)
            self.payloads[int(kwargs["photo_number"]) - 1] = kwargs["content"]

    async def fake_variant_bytes(self, test, variant, content):
        return replacement, "image/png", "uploaded.png"

    async def run() -> None:
        content = FakeContent()
        service = ABTestService(None)  # type: ignore[arg-type]
        test = ABTest(id=11, nm_id=123)
        card_variant = ABTestVariant(
            position=1,
            source_type="card",
            source_url="https://cdn.example/slot-2.jpg",
            file_name="slot-2.jpg",
        )
        upload_variant = ABTestVariant(position=2, source_type="upload", file_name="uploaded.png")
        await service._initialize_media_state(test, list(content.urls), [card_variant, upload_variant], content)
        assert test.media_state["parking_slot"] == 3
        monkeypatch.setattr(ABTestService, "_variant_bytes", fake_variant_bytes)
        await service._apply_variant(test, upload_variant, content)

        assert [call["photo_number"] for call in content.upload_calls] == [3, 1]
        assert content.payloads == [replacement, original_payloads[1], original_payloads[0]]
        await service._restore_original(test, content)
        assert content.payloads == original_payloads

    original_sleep = asyncio.sleep
    monkeypatch.setattr("app.services.ab_test_service.asyncio.sleep", lambda _: original_sleep(0))
    monkeypatch.setattr("app.services.ab_test_service.ABTestService._media_root", staticmethod(lambda: tmp_path))
    asyncio.run(run())


def test_promotion_metrics_sum_nested_aliases() -> None:
    payload = {"days": [{"views": "10", "clicks": 2, "orders": 1, "sum_price": 4.5}, {"views": 5, "clicks": 1, "orders": "2", "sum_price": 1.5}]}
    assert WBPromotionClient._metric_total(payload, "views") == 15
    assert WBPromotionClient._metric_total(payload, "orders") == 3
    assert WBPromotionClient._metric_total(payload, "sum", ("sum_price",)) == 6


def test_promotion_count_parses_nested_adverts(monkeypatch: pytest.MonkeyPatch) -> None:
    client = WBPromotionClient("nested-campaign-test-token")
    client.__class__._campaign_cache.clear()

    async def fake_request(*args, **kwargs):
        return {
            "data": {
                "adverts": [
                    {"status": 9, "advert_list": [{"advertId": 101}, {"advertId": "102", "status": 7}]},
                ]
            }
        }

    monkeypatch.setattr(client, "_request", fake_request)
    campaigns = asyncio.run(client.list_campaigns())
    assert campaigns == [{"id": 101, "status": 9}, {"id": 102, "status": 7}]


def test_promotion_count_parses_wb_flat_status_id(monkeypatch: pytest.MonkeyPatch) -> None:
    client = WBPromotionClient("flat-campaign-test-token")
    client.__class__._campaign_cache.clear()

    async def fake_request(*args, **kwargs):
        return [{"id": 201, "statusId": 9}, {"id": 202, "statusId": 4}]

    monkeypatch.setattr(client, "_request", fake_request)
    assert asyncio.run(client.list_campaigns()) == [
        {"id": 201, "status": 9},
        {"id": 202, "status": 4},
    ]


def test_unified_campaign_omits_manual_placement_types(monkeypatch: pytest.MonkeyPatch) -> None:
    client = WBPromotionClient("unified-create-test-token")
    calls: list[tuple[str, str, dict]] = []

    async def fake_request(method: str, path: str, **kwargs):
        calls.append((method, path, kwargs))
        return 123456

    monkeypatch.setattr(client, "_request", fake_request)
    assert asyncio.run(client.create_campaign(name="Test", nm_id=987, placement="search")) == 123456
    assert calls == [
        (
            "POST",
            "/adv/v2/seacat/save-ad",
            {
                "json": {
                    "name": "Test",
                    "nms": [987],
                    "bid_type": "unified",
                    "payment_type": "cpm",
                }
            },
        )
    ]


def test_unified_minimum_bid_requests_combined(monkeypatch: pytest.MonkeyPatch) -> None:
    client = WBPromotionClient("unified-min-bid-test-token")
    calls: list[tuple[str, str, dict]] = []

    async def fake_request(method: str, path: str, **kwargs):
        calls.append((method, path, kwargs))
        return {"bids": [{"nm_id": 987, "bids": [{"type": "combined", "value": 40500}]}]}

    monkeypatch.setattr(client, "_request", fake_request)
    assert asyncio.run(client.get_min_bid(campaign_id=123456, nm_id=987, placement="search")) == 405
    assert calls[0][2]["json"]["placement_types"] == ["combined"]


def test_unified_bid_update_forces_combined_placement(monkeypatch: pytest.MonkeyPatch) -> None:
    client = WBPromotionClient("unified-set-bid-test-token")
    calls: list[tuple[str, str, dict]] = []

    async def fake_request(method: str, path: str, **kwargs):
        calls.append((method, path, kwargs))
        return {"ok": True}

    monkeypatch.setattr(client, "_request", fake_request)
    asyncio.run(client.set_bid(campaign_id=123456, nm_id=987, cpm_rub=405, placement="search"))
    assert calls[0][2]["json"]["bids"][0]["nm_bids"][0]["placement"] == "combined"



def test_budget_deposit_uses_the_wb_contract_and_requests_updated_total(monkeypatch: pytest.MonkeyPatch) -> None:
    client = WBPromotionClient("deposit-contract-test-token")
    calls: list[tuple[str, str, dict]] = []

    async def fake_request(method: str, path: str, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/adv/v1/balance":
            return {"balance": 2025, "net": 0}
        return {"total": 2025}

    monkeypatch.setattr(client, "_request", fake_request)
    assert asyncio.run(client.deposit_budget(campaign_id=459, amount_rub=2025)) == {"total": 2025}
    assert calls == [
        ("GET", "/adv/v1/balance", {}),
        (
            "POST",
            "/adv/v1/budget/deposit",
            {
                "params": {"id": 459},
                "json": {"sum": 2025, "type": 0, "return": True},
            },
        )
    ]


def test_budget_deposit_uses_mutual_settlement_when_account_balance_is_insufficient(monkeypatch: pytest.MonkeyPatch) -> None:
    client = WBPromotionClient("deposit-net-test-token")
    calls: list[tuple[str, str, dict]] = []

    async def fake_request(method: str, path: str, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/adv/v1/balance":
            return {"balance": 100, "net": 2025}
        return {"total": 2125}

    monkeypatch.setattr(client, "_request", fake_request)
    assert asyncio.run(client.deposit_budget(campaign_id=459, amount_rub=2025)) == {"total": 2125}
    assert calls[-1][2]["json"] == {"sum": 2025, "type": 1, "return": True}


def test_balance_exposes_account_mutual_and_promo_bonus_sources() -> None:
    normalized = WBPromotionClient.normalize_balance(
        {
            "balance": 2025,
            "net": 1500,
            "bonus": 700,
            "cashbacks": [{"sum": 300, "percent": 30, "expiration_date": "2026-10-01T00:00:00Z"}],
        }
    )
    assert normalized == {
        "account_balance": 2025.0,
        "mutual_balance": 1500.0,
        "promo_bonus_balance": 700.0,
        "cashbacks": [{"sum": 300.0, "percent": 30, "expiration_date": "2026-10-01T00:00:00Z"}],
    }


def test_budget_deposit_uses_explicit_promo_bonus_source(monkeypatch: pytest.MonkeyPatch) -> None:
    client = WBPromotionClient("deposit-bonus-contract-test-token")
    calls: list[tuple[str, str, dict]] = []

    async def fake_request(method: str, path: str, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/adv/v1/balance":
            return {"balance": 0, "net": 0, "bonus": 2025}
        return {"total": 2025}

    monkeypatch.setattr(client, "_request", fake_request)
    assert asyncio.run(client.deposit_budget(campaign_id=459, amount_rub=2025, source_type=3)) == {"total": 2025}
    assert calls[-1][2]["json"] == {"sum": 2025, "type": 3, "return": True}


def test_budget_deposit_does_not_fallback_from_explicit_source(monkeypatch: pytest.MonkeyPatch) -> None:
    client = WBPromotionClient("deposit-explicit-source-test-token")
    calls: list[tuple[str, str, dict]] = []

    async def fake_request(method: str, path: str, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/adv/v1/balance":
            return {"balance": 2025, "net": 0, "bonus": 0}
        return {"total": 2025}

    monkeypatch.setattr(client, "_request", fake_request)
    with pytest.raises(WBApiError, match="выбранном источнике"):
        asyncio.run(client.deposit_budget(campaign_id=459, amount_rub=2025, source_type=3))
    assert [path for _, path, _ in calls] == ["/adv/v1/balance"]


def test_budget_deposit_does_not_call_deposit_without_available_funding(monkeypatch: pytest.MonkeyPatch) -> None:
    client = WBPromotionClient("deposit-empty-test-token")
    calls: list[tuple[str, str, dict]] = []

    async def fake_request(method: str, path: str, **kwargs):
        calls.append((method, path, kwargs))
        return {"balance": 0, "net": 0}

    monkeypatch.setattr(client, "_request", fake_request)
    with pytest.raises(WBApiError, match="Недостаточно средств"):
        asyncio.run(client.deposit_budget(campaign_id=459, amount_rub=2025))
    assert [path for _, path, _ in calls] == ["/adv/v1/balance"]


def test_thirty_variants_have_a_budget_floor() -> None:
    variants = 30
    views_per_variant = 1_000
    cpm_rub = 300
    assert calculate_required_budget(variants, views_per_variant, cpm_rub) == 9_900


def test_budget_matches_wb_optimizer_floor_and_buffer() -> None:
    assert calculate_required_budget(3, 1_000, 300) == 1_200
    assert calculate_required_budget(5, 1_000, 250) == 1_400


def test_safe_error_keeps_minimum_cpm_message_readable() -> None:
    error = HTTPException(
        status_code=409,
        detail={
            "code": "minimum_cpm_increased",
            "message": "Минимальная ставка Wildberries сейчас 405 ₽.",
            "minimum_cpm": 405,
        },
    )

    assert ABTestService._safe_error(error) == "Минимальная ставка Wildberries сейчас 405 ₽."


def test_campaign_start_confirmation_retries_until_wb_reports_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePromotion:
        def __init__(self) -> None:
            self.statuses = iter([4, None, 9])
            self.calls = 0

        async def get_campaign_status(self, campaign_id: int, *, refresh: bool = True):
            self.calls += 1
            return next(self.statuses)

    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr("app.services.ab_test_service.asyncio.sleep", fake_sleep)
    service = ABTestService(None)  # type: ignore[arg-type]
    promotion = FakePromotion()

    assert asyncio.run(service._confirm_campaign_active(promotion, 39932131)) == 9
    assert promotion.calls == 3
    assert delays == [1.0, 2.0]


def test_operation_state_does_not_replace_a_detailed_incident_message() -> None:
    class FakeDB:
        async def flush(self):
            return None

    service = ABTestService(FakeDB())  # type: ignore[arg-type]
    test = ABTest(last_error="Запуск не подтверждён. Инцидент INC-TEST123.")
    operation = ABTestOperation()

    asyncio.run(
        service._operation_state(
            test,
            operation,
            ABTestOperationStatus.RECONCILIATION_REQUIRED,
            error=WBApiError("provider timeout"),
        )
    )

    assert test.last_error == "Запуск не подтверждён. Инцидент INC-TEST123."
    assert operation.last_error == "Не удалось надёжно определить результат запроса к Wildberries"


def test_stop_requires_reconciliation_when_wb_still_reports_active() -> None:
    class FakeDB:
        async def flush(self):
            return None

    class FakePromotion:
        async def stop_campaign(self, campaign_id: int):
            return {"accepted": True}

        async def get_campaign_status(self, campaign_id: int, *, refresh: bool = True):
            assert refresh is True
            return 9

    service = ABTestService(FakeDB())  # type: ignore[arg-type]
    test = ABTest(id=20, nm_id=123, wb_campaign_id=456)

    with pytest.raises(ABTestReconciliationRequired, match="не подтвердил остановку"):
        asyncio.run(service._stop_campaign_confirmed(test, FakePromotion()))
    assert test.campaign_state == "running"
    assert test.operation_state == "reconciliation_required"
    assert test.incident_id


def test_stop_polls_until_wb_reports_inactive(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeDB:
        async def flush(self):
            return None

    class FakePromotion:
        def __init__(self) -> None:
            self.statuses = iter([9, 9, 4])
            self.stop_calls = 0

        async def stop_campaign(self, campaign_id: int):
            self.stop_calls += 1
            return {"accepted": True}

        async def get_campaign_status(self, campaign_id: int, *, refresh: bool = True):
            return next(self.statuses)

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("app.services.ab_test_service.asyncio.sleep", no_sleep)
    service = ABTestService(FakeDB())  # type: ignore[arg-type]
    promotion = FakePromotion()
    test = ABTest(id=24, nm_id=123, wb_campaign_id=459)

    asyncio.run(service._stop_campaign_confirmed(test, promotion))

    assert promotion.stop_calls == 1
    assert test.campaign_state == "stopped"


def test_promotion_error_message_reads_nested_wb_error_shapes() -> None:
    payload = {"errors": [{"message": "Недостаточно средств на счёте"}]}
    assert WBPromotionClient._error_message(payload) == "Недостаточно средств на счёте"


@pytest.mark.parametrize("campaign_status", [7, 11])
def test_stop_accepts_only_a_known_inactive_campaign_state(campaign_status: int) -> None:
    class FakeDB:
        async def flush(self):
            return None

    class FakePromotion:
        async def stop_campaign(self, campaign_id: int):
            return {"accepted": True}

        async def get_campaign_status(self, campaign_id: int, *, refresh: bool = True):
            return campaign_status

    service = ABTestService(FakeDB())  # type: ignore[arg-type]
    test = ABTest(id=21, nm_id=123, wb_campaign_id=457)
    asyncio.run(service._stop_campaign_confirmed(test, FakePromotion()))
    assert test.campaign_state == "stopped"


def test_stop_does_not_call_wb_stop_for_a_campaign_that_is_already_inactive() -> None:
    class FakeDB:
        async def flush(self):
            return None

    class FakePromotion:
        def __init__(self) -> None:
            self.stop_calls = 0

        async def stop_campaign(self, campaign_id: int):
            self.stop_calls += 1
            return {"accepted": True}

        async def get_campaign_status(self, campaign_id: int, *, refresh: bool = True):
            return 4

    service = ABTestService(FakeDB())  # type: ignore[arg-type]
    promotion = FakePromotion()
    test = ABTest(id=22, nm_id=123, wb_campaign_id=458)

    asyncio.run(service._stop_campaign_confirmed(test, promotion))

    assert test.campaign_state == "stopped"
    assert promotion.stop_calls == 0


def test_prestart_failure_can_resume_the_same_campaign() -> None:
    test = ABTest(
        id=23,
        status=ABTestStatus.DRAFT,
        wb_campaign_id=459,
        campaign_state="stopped",
    )
    operation = ABTestOperation(
        status=ABTestOperationStatus.FAILED,
        response_snapshot={"phase": "campaign_created", "campaign_id": 459},
    )

    assert ABTestService._can_resume_existing_campaign(test, operation) is True

    test.started_at = ABTestService._now()
    assert ABTestService._can_resume_existing_campaign(test, operation) is False


def test_five_photo_sequence_swaps_each_slot_once_and_restores_exact_originals(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Model five WB slots, mixed card/upload variants and the final rollback."""
    originals = [_valid_png(f"original-{slot}".encode()) for slot in range(1, 6)]
    uploads = {
        3: _valid_png(b"uploaded-3"),
        5: _valid_png(b"uploaded-5"),
    }

    class FakeContent:
        def __init__(self) -> None:
            self.urls = [f"https://cdn.example/slot-{slot}.jpg" for slot in range(1, 6)]
            self.payloads = list(originals)
            self.upload_calls: list[dict[str, object]] = []

        async def get_card(self, nm_id: int):
            return {"photos": list(self.urls)}

        async def download_image(self, url: str, *, cache_bust: bool = False):
            index = self.urls.index(url.split("?", 1)[0])
            return self.payloads[index], "image/png"

        async def upload_media_file(self, **kwargs):
            self.upload_calls.append(kwargs)
            self.payloads[int(kwargs["photo_number"]) - 1] = kwargs["content"]

    async def fake_variant_bytes(self, test, variant, content):
        data = uploads[variant.position]
        return data, "image/png", f"upload-{variant.position}.png"

    async def run() -> None:
        content = FakeContent()
        service = ABTestService(None)  # type: ignore[arg-type]
        test = ABTest(id=50, nm_id=123)
        variants = [
            ABTestVariant(position=1, source_type="card", source_url=content.urls[1]),
            ABTestVariant(position=2, source_type="card", source_url=content.urls[3]),
            ABTestVariant(position=3, source_type="upload"),
            ABTestVariant(position=4, source_type="card", source_url=content.urls[2]),
            ABTestVariant(position=5, source_type="upload"),
        ]

        await service._initialize_media_state(test, list(content.urls), variants, content)
        for variant in variants:
            await service._apply_variant(test, variant, content)

        # Card source slots 2, 4 and 3 are swapped with the active main slot;
        # uploads use the unused parking slot 5 before changing slot 1.
        assert [call["photo_number"] for call in content.upload_calls] == [
            2, 1, 4, 1, 5, 1, 3, 1, 5, 1
        ]
        assert test.media_state["touched"] == [1, 2, 3, 4, 5]

        await service._restore_original(test, content)
        assert [call["photo_number"] for call in content.upload_calls[-5:]] == [5, 4, 3, 2, 1]
        assert content.payloads == originals
        assert test.media_state["touched"] == []
        assert test.media_status == "restored"

    monkeypatch.setattr(ABTestService, "_variant_bytes", fake_variant_bytes)
    original_sleep = asyncio.sleep
    monkeypatch.setattr("app.services.ab_test_service.asyncio.sleep", lambda _: original_sleep(0))
    monkeypatch.setattr("app.services.ab_test_service.ABTestService._media_root", staticmethod(lambda: tmp_path))
    asyncio.run(run())


def test_five_photo_card_sources_without_parking_slot_replace_only_main_for_upload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    originals = [_valid_png(f"original-{slot}".encode()) for slot in range(1, 6)]
    replacement = _valid_png(b"replacement")

    class FakeContent:
        def __init__(self) -> None:
            self.urls = [f"https://cdn.example/slot-{slot}.jpg" for slot in range(1, 6)]
            self.payloads = list(originals)
            self.upload_calls: list[dict[str, object]] = []

        async def get_card(self, nm_id: int):
            return {"photos": list(self.urls)}

        async def download_image(self, url: str, *, cache_bust: bool = False):
            index = self.urls.index(url.split("?", 1)[0])
            return self.payloads[index], "image/png"

        async def upload_media_file(self, **kwargs):
            self.upload_calls.append(kwargs)
            self.payloads[int(kwargs["photo_number"]) - 1] = kwargs["content"]

    async def fake_variant_bytes(self, test, variant, content):
        return replacement, "image/png", "replacement.png"

    async def run() -> None:
        content = FakeContent()
        service = ABTestService(None)  # type: ignore[arg-type]
        test = ABTest(id=51, nm_id=123)
        card_variants = [
            ABTestVariant(position=position, source_type="card", source_url=content.urls[position])
            for position in range(1, 5)
        ]
        upload_variant = ABTestVariant(position=5, source_type="upload")
        variants = [*card_variants, upload_variant]
        await service._initialize_media_state(test, list(content.urls), variants, content)
        assert test.media_state["parking_slot"] is None

        await service._apply_variant(test, upload_variant, content)
        assert [call["photo_number"] for call in content.upload_calls] == [1]
        assert content.payloads[1:] == originals[1:]
        await service._restore_original(test, content)
        assert content.payloads == originals

    monkeypatch.setattr(ABTestService, "_variant_bytes", fake_variant_bytes)
    original_sleep = asyncio.sleep
    monkeypatch.setattr("app.services.ab_test_service.asyncio.sleep", lambda _: original_sleep(0))
    monkeypatch.setattr("app.services.ab_test_service.ABTestService._media_root", staticmethod(lambda: tmp_path))
    asyncio.run(run())


def test_five_photo_sequence_continues_after_card_media_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    originals = [_valid_png(f"original-{slot}".encode()) for slot in range(1, 6)]

    class FakeContent:
        def __init__(self) -> None:
            self.urls = [f"https://cdn.example/slot-{slot}.jpg" for slot in range(1, 6)]
            self.payloads = list(originals)
            self.upload_calls: list[dict[str, object]] = []

        async def get_card(self, nm_id: int):
            return {"photos": list(self.urls)}

        async def download_image(self, url: str, *, cache_bust: bool = False):
            index = self.urls.index(url.split("?", 1)[0])
            return self.payloads[index], "image/png"

        async def upload_media_file(self, **kwargs):
            self.upload_calls.append(kwargs)
            self.payloads[int(kwargs["photo_number"]) - 1] = kwargs["content"]

    async def run() -> None:
        content = FakeContent()
        service = ABTestService(None)  # type: ignore[arg-type]
        test = ABTest(id=52, nm_id=123)
        first = ABTestVariant(position=1, source_type="card", source_url=content.urls[1])
        second = ABTestVariant(position=2, source_type="card", source_url=content.urls[2])
        await service._initialize_media_state(test, list(content.urls), [first, second], content)
        await service._apply_variant(test, first, content)
        calls_before_next_variant = len(content.upload_calls)

        # Seller changes slot 5 outside the experiment after the first stage.
        content.payloads[4] = _valid_png(b"seller-changed-slot-5")
        await service._apply_variant(test, second, content)
        assert len(content.upload_calls) > calls_before_next_variant
        assert test.media_status == "variant_applied"

    original_sleep = asyncio.sleep
    monkeypatch.setattr("app.services.ab_test_service.asyncio.sleep", lambda _: original_sleep(0))
    monkeypatch.setattr("app.services.ab_test_service.ABTestService._media_root", staticmethod(lambda: tmp_path))
    asyncio.run(run())


def test_restore_does_not_read_back_card_after_upload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    originals = [_valid_png(b"original-1"), _valid_png(b"original-2")]
    replacement = _valid_png(b"replacement")

    class FakeContent:
        def __init__(self) -> None:
            self.urls = ["https://cdn.example/slot-1.jpg", "https://cdn.example/slot-2.jpg"]
            self.payloads = list(originals)
            self.card_reads_allowed = True

        async def get_card(self, nm_id: int):
            if not self.card_reads_allowed:
                raise AssertionError("card read-back must not be used by media operations")
            return {"photos": list(self.urls)}

        async def download_image(self, url: str, *, cache_bust: bool = False):
            index = self.urls.index(url.split("?", 1)[0])
            return self.payloads[index], "image/png"

        async def upload_media_file(self, **kwargs):
            self.payloads[int(kwargs["photo_number"]) - 1] = kwargs["content"]

    async def fake_variant_bytes(self, test, variant, content):
        return replacement, "image/png", "replacement.png"

    async def run() -> None:
        content = FakeContent()
        service = ABTestService(None)  # type: ignore[arg-type]
        test = ABTest(id=54, nm_id=123)
        variant = ABTestVariant(position=1, source_type="upload")
        await service._initialize_media_state(test, list(content.urls), [variant], content)
        monkeypatch.setattr(ABTestService, "_variant_bytes", fake_variant_bytes)
        content.card_reads_allowed = False
        await service._apply_variant(test, variant, content)
        await service._restore_original(test, content, allow_pending=True)
        assert content.payloads == originals
        assert test.media_state["pending"] is None
        assert test.media_status == "restored"

    monkeypatch.setattr(ABTestService, "_media_root", staticmethod(lambda: tmp_path))
    asyncio.run(run())


def test_partial_swap_result_is_owned_and_is_restored_without_false_conflict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """A failed second request is rolled back without external-edit checks."""
    originals = [_valid_png(b"original-main"), _valid_png(b"original-second")]

    class FakeContent:
        def __init__(self) -> None:
            self.urls = ["https://cdn.example/slot-1.jpg", "https://cdn.example/slot-2.jpg"]
            self.payloads = list(originals)
            self.upload_calls: list[dict[str, object]] = []
            self.fail_main_upload = True

        async def get_card(self, nm_id: int):
            return {"photos": list(self.urls)}

        async def download_image(self, url: str, *, cache_bust: bool = False):
            index = self.urls.index(url.split("?", 1)[0])
            return self.payloads[index], "image/png"

        async def upload_media_file(self, **kwargs):
            self.upload_calls.append(kwargs)
            slot = int(kwargs["photo_number"]) - 1
            if self.fail_main_upload and slot == 0:
                self.fail_main_upload = False
                raise RuntimeError("simulated slot upload failure")
            self.payloads[slot] = kwargs["content"]

    async def run() -> None:
        content = FakeContent()
        service = ABTestService(None)  # type: ignore[arg-type]
        test = ABTest(id=56, nm_id=123)
        variant = ABTestVariant(
            position=1,
            source_type="card",
            source_url=content.urls[1],
            file_name="slot-2.jpg",
        )
        await service._initialize_media_state(test, list(content.urls), [variant], content)

        with pytest.raises(RuntimeError, match="slot upload failure"):
            await service._apply_variant(test, variant, content)

            # The secondary slot may already contain the active main image,
            # while the main-slot request failed. The journal can restore both.
            assert test.media_state["pending"]["kind"] == "swap"
            assert content.payloads == [originals[0], originals[0]]

        await service._restore_original(test, content, allow_pending=True)

        assert content.payloads == originals
        assert test.media_state["pending"] is None
        assert test.media_status == "restored"

    original_sleep = asyncio.sleep
    monkeypatch.setattr("app.services.ab_test_service.asyncio.sleep", lambda _: original_sleep(0))
    monkeypatch.setattr(ABTestService, "_media_root", staticmethod(lambda: tmp_path))
    asyncio.run(run())


def test_aggregate_fullstats_delta_is_kept_as_unallocated_audit_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeDB:
        async def commit(self):
            return None

    class FakeContent:
        pass

    class FakePromotion:
        def cached_fullstats(self, campaign_id: int, *, started_at=None):
            assert campaign_id == 9001
            return {"views": 120, "clicks": 7, "orders": 2, "sum": 18.5}

    async def fake_clients(self, connection):
        return FakeContent(), FakePromotion()

    async def run() -> None:
        service = ABTestService(FakeDB())  # type: ignore[arg-type]
        test = ABTest(id=53, nm_id=123, wb_campaign_id=9001, current_variant_order=1)
        test.variants = [ABTestVariant(position=1, source_type="upload", views=0, clicks=0, spend_rub=0)]
        await service._sync_loaded(test)
        assert test.stats_quality == "aggregate_unverified"
        assert test.unallocated_views == 120
        assert test.unallocated_clicks == 7
        assert test.unallocated_spend_rub == 18.5
        assert test.last_total_orders == 2
        # The current stage is still used only as a progress counter for the
        # switch scheduler; it is not presented as attributable photo CTR.
        assert test.variants[0].views == 120
        assert test.variants[0].clicks == 7
        assert test.variants[0].orders == 2

    monkeypatch.setattr(ABTestService, "_clients", fake_clients)
    asyncio.run(run())


def test_sync_switch_keeps_existing_campaign_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeDB:
        async def commit(self):
            return None

    class FakeContent:
        pass

    class FakePromotion:
        def cached_fullstats(self, campaign_id: int, *, started_at=None):
            return {"views": 100, "clicks": 4, "orders": 0, "sum": 10}

        async def stop_campaign(self, campaign_id: int):
            raise AssertionError("variant switch must not stop the campaign")

        async def start_campaign(self, campaign_id: int):
            raise AssertionError("variant switch must not restart the campaign")

    applied: list[int] = []

    async def fake_clients(self, connection):
        return FakeContent(), FakePromotion()

    async def fake_apply(self, test, variant, content):
        applied.append(variant.position)

    async def run() -> None:
        service = ABTestService(FakeDB())  # type: ignore[arg-type]
        test = ABTest(
            id=59,
            nm_id=123,
            wb_campaign_id=9002,
            current_variant_order=1,
            views_per_variant=100,
        )
        test.variants = [
            ABTestVariant(position=1, source_type="upload", views=0, clicks=0),
            ABTestVariant(position=2, source_type="upload", views=0, clicks=0),
        ]
        await service._sync_loaded(test)
        assert applied == [2]
        assert test.current_variant_order == 2
        assert test.campaign_state == "running"

    monkeypatch.setattr(ABTestService, "_clients", fake_clients)
    monkeypatch.setattr(ABTestService, "_apply_variant", fake_apply)
    asyncio.run(run())
