"""Deterministic acceptance regressions. No seller credentials or WB writes."""
from __future__ import annotations

import asyncio
import base64
import io
import json
import socket
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from PIL import Image

from app.services.wb_content_client import WBApiError, WBContentClient, _retry_after_seconds
from app.services.wb_promotion_client import WBPromotionClient
from app.core.config import settings
from app.services.wb_rate_limiter import WBRateLimiter
from app.services.wb_token_service import WBTokenService


def token(seller="seller-a", permissions=66):
    claims = {"sid": seller, "s": permissions, "exp": int((datetime.now(timezone.utc) + timedelta(days=1)).timestamp())}
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"eyJhbGciOiJIUzI1NiJ9.{encoded}.synthetic-signature"


def mocked_http(monkeypatch, handler):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))


@pytest.mark.asyncio
@pytest.mark.parametrize("permissions,ready", [(66, True), (66 | (1 << 30), False)])
async def test_token_read_only_bit_blocks_ready_even_when_both_pings_succeed(monkeypatch, permissions, ready):
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"sid": "seller-a"} if request.url.path.endswith("seller-info") else {"Status": "OK"})
    mocked_http(monkeypatch, handle)
    result = await WBTokenService(SimpleNamespace()).validate(token(permissions=permissions))
    assert result["ready_for_ab_tests"] is ready
    assert result["write_access"] is ready
    assert result["seller_id"] == "seller-a"
    assert all(item.method == "GET" for item in calls)
    assert any(item.url.path == "/api/v1/seller-info" for item in calls)


@pytest.mark.asyncio
async def test_seller_info_mismatch_fails_closed(monkeypatch):
    mocked_http(monkeypatch, lambda request: httpx.Response(200, json={"sid": "seller-b"} if request.url.path.endswith("seller-info") else {}))
    with pytest.raises(HTTPException) as error:
        await WBTokenService(SimpleNamespace()).validate(token())
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_invalid_token_does_not_call_seller_info(monkeypatch):
    calls = []
    def handle(request):
        calls.append(request.url.path)
        return httpx.Response(401, json={"message": "invalid"})
    mocked_http(monkeypatch, handle)
    with pytest.raises(HTTPException) as error:
        await WBTokenService(SimpleNamespace()).validate(token())
    assert error.value.status_code == 401
    assert calls == ["/ping", "/ping"]


def test_store_identity_survives_different_tokens_and_owners():
    first = SimpleNamespace(seller_id="seller-a", user_id=1, token_fingerprint="token-one")
    second = SimpleNamespace(seller_id="seller-a", user_id=2, token_fingerprint="token-two")
    assert WBTokenService.store_fingerprint(first) == WBTokenService.store_fingerprint(second)
    with pytest.raises(HTTPException):
        WBTokenService.store_fingerprint(SimpleNamespace(seller_id=None))


@pytest.mark.asyncio
async def test_rotation_cannot_switch_seller_even_without_open_tests():
    service = WBTokenService(SimpleNamespace(commit=AsyncMock()))
    service.validate = AsyncMock(return_value={"ready_for_ab_tests": True, "seller_id": "seller-b"})
    service.repository.get_for_user = AsyncMock(return_value=SimpleNamespace(seller_id="seller-a"))
    with pytest.raises(HTTPException) as error:
        await service.rotate_token(1, 1, token("seller-b"))
    assert error.value.status_code == 409
    service.db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_numeric_search_ignores_vendor_code_collision_and_preserves_safety_metadata():
    client = WBContentClient("synthetic")
    client._request = AsyncMock(return_value={"cards": [
        {"nmID": 111, "vendorCode": "222", "photos": ["https://cdn.example/one.jpg"]},
        {"nmID": 222, "photos": ["https://cdn.example/two.jpg"], "video": "https://cdn.example/movie.mp4", "blocked": True},
    ], "cursor": {"total": 2}})
    card = await client.get_card(222)
    assert card["nm_id"] == 222
    assert card["blocked"] is True
    assert WBContentClient.media_snapshot(card)["video_count"] == 1
    assert client._request.await_args.kwargs["json"]["settings"]["cursor"]["limit"] == 100


@pytest.mark.asyncio
async def test_catalog_end_and_pagination_without_echoed_limit():
    client = WBContentClient("synthetic")
    client._request = AsyncMock(side_effect=[
        {"cards": [{"nmID": n} for n in range(1, 101)], "cursor": {"total": 100, "updatedAt": "today", "nmID": 100}},
        {"cards": [{"nmID": 101}], "cursor": {"total": 1, "updatedAt": "today", "nmID": 101}},
    ])
    assert len(await client.list_all_cards()) == 101
    assert client._request.await_count == 2
    assert client._request.await_args.kwargs["json"]["settings"]["cursor"]["nmID"] == 100


def test_media_snapshot_retains_duplicate_slots_and_video():
    snapshot = WBContentClient.media_snapshot({"photos": ["https://cdn/a", "https://cdn/a"], "video": "https://cdn/v"})
    assert snapshot == {"photos": ["https://cdn/a", "https://cdn/a"], "videos": ["https://cdn/v"], "photo_count": 2, "video_count": 1}


def png():
    output = io.BytesIO()
    Image.new("RGB", (20, 20), "blue").save(output, "PNG")
    return output.getvalue()


@pytest.mark.asyncio
async def test_image_download_pins_resolved_ip_and_keeps_host_and_tls_sni(monkeypatch):
    lookups = []
    requests = []
    def resolve(*args, **kwargs):
        lookups.append(args)
        ip = "93.184.216.34" if len(lookups) == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    def handle(request):
        requests.append(request)
        return httpx.Response(200, content=png(), headers={"content-type": "image/png"})
    mocked_http(monkeypatch, handle)
    content, mime = await WBContentClient("synthetic").download_image("https://cdn.example/photo.png")
    assert content == png() and mime == "image/png"
    assert len(lookups) == 1
    assert requests[0].url.host == "93.184.216.34"
    assert requests[0].headers["host"] == "cdn.example"
    assert requests[0].extensions["sni_hostname"] == "cdn.example"
    assert "authorization" not in requests[0].headers


@pytest.mark.asyncio
async def test_image_redirect_to_private_address_is_rejected_before_second_connection(monkeypatch):
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})
    mocked_http(monkeypatch, handle)
    with pytest.raises(WBApiError, match="внутренней"):
        await WBContentClient("synthetic").download_image("https://93.184.216.34/photo.png")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_image_mime_does_not_allow_non_image_bytes(monkeypatch):
    mocked_http(monkeypatch, lambda request: httpx.Response(200, content=b"<html>fake image</html>", headers={"content-type": "image/jpeg"}))
    with pytest.raises(WBApiError, match="повреждено"):
        await WBContentClient("synthetic").download_image("https://93.184.216.34/photo.jpg")


@pytest.mark.asyncio
@pytest.mark.parametrize("currency", ["USD", "KZT", "CNY"])
async def test_minimum_bid_rejects_foreign_currency_at_envelope(currency):
    client = WBPromotionClient("synthetic")
    client._request = AsyncMock(return_value={"currency": currency, "bids": [{"nm_id": 123, "bids": [{"type": "combined", "value": 25001}]}]})
    with pytest.raises(WBApiError, match="валюте"):
        await client.get_min_bid(campaign_id=1, nm_id=123)


@pytest.mark.asyncio
async def test_minimum_bid_kopecks_round_up_exactly():
    client = WBPromotionClient("synthetic")
    client._request = AsyncMock(return_value={"bids": [{"nm_id": 123, "bids": [{"type": "combined", "value": 25001}]}]})
    assert await client.get_min_bid(campaign_id=1, nm_id=123) == 251


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["NaN", "Infinity", -1, True])
async def test_nonfinite_or_negative_budget_is_not_accepted(bad):
    client = WBPromotionClient("synthetic")
    client.get_budget = AsyncMock(return_value={"total": bad})
    with pytest.raises(WBApiError):
        await client.get_budget_total(1)


@pytest.mark.asyncio
async def test_explicit_funding_source_never_silently_falls_back():
    client = WBPromotionClient("synthetic")
    client.get_balance = AsyncMock(return_value={"balance": 10000, "net": 0})
    client._request = AsyncMock()
    with pytest.raises(WBApiError, match="выбранном источнике"):
        await client.deposit_budget(campaign_id=1, amount_rub=1200, source_type=1)
    client._request.assert_not_awaited()


@pytest.fixture
def stats_client():
    WBPromotionClient._stats_cache.clear()
    WBPromotionClient._last_stats_at.clear()
    WBPromotionClient._stats_locks.clear()
    return WBPromotionClient("synthetic-statistics")


def observation(**overrides):
    return {"advertId": 1, "views": 100, "clicks": 4, "orders": 2, "sum": 20.0, **overrides}


@pytest.mark.asyncio
async def test_duplicate_campaign_observations_do_not_double_counters(stats_client):
    stats_client._request = AsyncMock(return_value=[observation(), observation()])
    result = await stats_client.fullstats(1)
    assert result["views"] == 100 and result["sum"] == 20
    assert result["_data_complete"] is True


@pytest.mark.asyncio
async def test_distinct_daily_campaign_rows_are_both_counted(stats_client):
    stats_client._request = AsyncMock(return_value=[
        observation(day="2026-09-26"), observation(day="2026-09-27", views=50, clicks=2, sum=10.0)
    ])
    result = await stats_client.fullstats(1)
    assert result["views"] == 150
    assert result["clicks"] == 6
    assert result["sum"] == 30


@pytest.mark.asyncio
async def test_explicit_variant_attribution_is_preserved(stats_client):
    stats_client._request = AsyncMock(return_value=[
        observation(views=400, clicks=24, sum=120.0) | {"variantPosition": 1},
        observation(views=420, clicks=30, sum=126.0) | {"variantPosition": 2},
    ])
    result = await stats_client.fullstats(1)
    assert result["_variant_attribution_complete"] is True
    assert result["_variant_totals"]["1"]["views"] == 400
    assert result["_variant_totals"]["2"]["clicks"] == 30


@pytest.mark.asyncio
async def test_mixed_variant_attribution_is_fail_closed(stats_client):
    stats_client._request = AsyncMock(return_value=[
        observation(views=400) | {"variantPosition": 1},
        observation(views=420),
    ])
    result = await stats_client.fullstats(1)
    assert result["_variant_attribution_complete"] is False
    assert "_variant_totals" not in result


@pytest.mark.asyncio
async def test_conflicting_campaign_observations_are_rejected(stats_client):
    stats_client._request = AsyncMock(return_value=[observation(), observation(views=101)])
    with pytest.raises(WBApiError, match="противоречивые"):
        await stats_client.fullstats(1)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["NaN", "Infinity", -1, 1.5, True, None])
async def test_invalid_impression_counters_are_rejected(stats_client, bad):
    stats_client._request = AsyncMock(return_value=[observation(views=bad)])
    with pytest.raises(WBApiError):
        await stats_client.fullstats(1)


@pytest.mark.asyncio
async def test_missing_campaign_or_spend_is_incomplete_and_revenue_is_not_spend(stats_client):
    row = observation(sum_price=9000)
    del row["sum"]
    stats_client._request = AsyncMock(return_value=[row])
    result = await stats_client.fullstats([1, 2])
    assert result["_data_complete"] is False
    assert result["_available_metrics"]["sum"] is False
    assert result["sum"] == 0


@pytest.mark.asyncio
async def test_fullstats_refresh_bypasses_cache_and_keeps_cached_observation_time(stats_client, monkeypatch):
    monkeypatch.setattr("app.services.wb_promotion_client.asyncio.sleep", AsyncMock())
    stats_client._request = AsyncMock(side_effect=[[observation()], [observation(views=101)]])
    first = await stats_client.fullstats(1)
    cached = await stats_client.fullstats(1)
    fresh = await stats_client.fullstats(1, refresh=True)
    assert stats_client._request.await_count == 2
    assert cached["_observed_at"] == first["_observed_at"]
    assert fresh["views"] == 101



@pytest.mark.asyncio
async def test_real_wb_mutations_are_blocked_without_explicit_opt_in(monkeypatch):
    monkeypatch.setattr(settings, "wb_allow_external_requests", False)
    client = WBPromotionClient("synthetic")
    client.base_url = "https://advert-api.wildberries.ru"
    with pytest.raises(WBApiError, match="Внешние изменения Wildberries отключены"):
        await client.start_campaign(123)


@pytest.mark.asyncio
async def test_loopback_provider_mutation_remains_available_for_synthetic_audit(monkeypatch):
    monkeypatch.setattr(settings, "wb_allow_external_requests", False)
    client = WBPromotionClient("synthetic")
    client.base_url = "http://127.0.0.1:18901/p"
    client._request = AsyncMock(return_value={"ok": True})
    assert await client.start_campaign(123) == {"ok": True}


@pytest.mark.asyncio
async def test_variant_attribution_rejects_missing_metric(stats_client):
    row = observation(views=400, clicks=24, sum=120.0) | {"variantPosition": 1}
    del row["orders"]
    stats_client._request = AsyncMock(return_value=[row])
    result = await stats_client.fullstats(1)
    assert result["_variant_attribution_complete"] is False
    assert "_variant_totals" not in result


@pytest.mark.asyncio
async def test_variant_attribution_rejects_clicks_above_views(stats_client):
    stats_client._request = AsyncMock(return_value=[
        observation(views=100, clicks=101, orders=0, sum=20.0) | {"variantPosition": 1},
        observation(views=100, clicks=10, orders=0, sum=20.0) | {"variantPosition": 2},
    ])
    result = await stats_client.fullstats(1)
    assert result["_variant_attribution_complete"] is False
    assert "_variant_totals" not in result

def test_retry_after_accepts_seconds_and_http_date():
    assert _retry_after_seconds(httpx.Response(429, headers={"Retry-After": "90"})) == 90
    deadline = (datetime.now(timezone.utc) + timedelta(seconds=90)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert 88 <= _retry_after_seconds(httpx.Response(429, headers={"Retry-After": deadline})) <= 90


@pytest.mark.asyncio
async def test_recovery_nonblocking_lock_skips_a_live_operation():
    async with WBRateLimiter.operation_lock(SimpleNamespace(), 1, 123, scope_key="seller-test") as acquired:
        assert acquired is True
        async with WBRateLimiter.operation_lock(SimpleNamespace(), 2, 123, scope_key="seller-test", wait=False) as acquired_again:
            assert acquired_again is False
    async with WBRateLimiter.operation_lock(SimpleNamespace(), 2, 123, scope_key="seller-test", wait=False) as acquired_again:
        assert acquired_again is True


@pytest.mark.asyncio
async def test_lost_lock_fences_money_request_before_http(monkeypatch):
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar=lambda: False)))
    context = WBRateLimiter._operation_owner.set((db, 1234, "seller-lock"))
    client_factory = AsyncMock()
    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    try:
        with pytest.raises(RuntimeError, match="fenced"):
            await WBPromotionClient("synthetic").start_campaign(1)
        client_factory.assert_not_called()
    finally:
        WBRateLimiter._operation_owner.reset(context)


@pytest.mark.asyncio
async def test_provider_error_cannot_echo_token_into_logs_or_exception(monkeypatch, caplog):
    secret = token()
    mocked_http(monkeypatch, lambda request: httpx.Response(403, json={"message": f"Rejected token {secret}"}))
    with pytest.raises(WBApiError) as error:
        await WBPromotionClient(secret).get_balance()
    assert secret not in str(error.value)
    assert secret not in caplog.text
    assert secret not in str(error.value.payload)
