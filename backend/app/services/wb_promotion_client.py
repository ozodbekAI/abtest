from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import asyncio
import hashlib
import json
import logging
import math
import time
from typing import Any
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit

import httpx

from app.core.config import settings
from app.services.wb_content_client import WBApiError, _json_or_text, _redact_token, _retry_after_seconds


logger = logging.getLogger(__name__)


class WBPromotionClient:
    _MUTATING_PATHS = frozenset({
        "/adv/v2/seacat/save-ad",
        "/api/advert/v1/bids",
        "/adv/v1/budget/deposit",
        "/adv/v0/start",
        "/adv/v0/pause",
        "/adv/v0/stop",
        "/adv/v0/delete",
    })
    _stats_locks: dict[str, asyncio.Lock] = {}
    _last_stats_at: dict[str, float] = {}
    _stats_cache: dict[tuple[str, tuple[int, ...], str, str], tuple[float, dict[str, Any]]] = {}
    _stats_cache_ttl = 45.0
    _campaign_cache: dict[str, tuple[float, list[dict[str, int]]]] = {}
    _campaign_cache_ttl = 45.0

    def __init__(self, token: str):
        self.token = token.strip()
        self.base_url = settings.wb_advert_api_url.rstrip("/")

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": self.token, "Content-Type": "application/json", "Accept": "application/json"}

    @staticmethod
    def _error_message(payload: Any) -> str | None:
        """Extract a short useful message from all WB error shapes."""
        if isinstance(payload, dict):
            for key in (
                "errorText",
                "message",
                "detail",
                "errorMessage",
                "description",
                "error",
                "errors",
            ):
                value = payload.get(key)
                if value not in (None, "", False, []):
                    if isinstance(value, (dict, list)):
                        nested = WBPromotionClient._error_message(value)
                        if nested:
                            return nested
                    else:
                        return str(value)[:500]
            for value in payload.values():
                nested = WBPromotionClient._error_message(value)
                if nested:
                    return nested
        elif isinstance(payload, list):
            messages = [WBPromotionClient._error_message(item) for item in payload]
            values = [message for message in messages if message]
            if values:
                return "; ".join(values[:3])[:500]
        elif payload not in (None, "", False):
            return str(payload)[:500]
        return None

    def _assert_external_mutation_allowed(self, path: str) -> None:
        """Fail closed before any real WB mutation unless explicitly enabled."""
        if path not in self._MUTATING_PATHS or settings.wb_allow_external_requests:
            return
        host = (urlsplit(self.base_url).hostname or "").lower()
        if host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}:
            return
        raise WBApiError(
            "Внешние изменения Wildberries отключены настройкой WB_ALLOW_EXTERNAL_REQUESTS. "
            "Для реального кабинета включите её только в явно разрешённом окружении.",
            status_code=503,
            payload={"code": "external_wb_mutations_disabled", "path": path},
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if path in self._MUTATING_PATHS:
            from app.services.wb_rate_limiter import WBRateLimiter
            await WBRateLimiter.assert_lock_owned()
            self._assert_external_mutation_allowed(path)
        timeout = max(settings.wb_request_timeout, 30.0)
        started = time.perf_counter()
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(method, f"{self.base_url}{path}", headers=self.headers, **kwargs)
        logger.info(
            "WB Promotion %s %s status=%s elapsed_ms=%s",
            method,
            path,
            response.status_code,
            int((time.perf_counter() - started) * 1000),
        )
        payload = _redact_token(_json_or_text(response), self.token)
        if not 200 <= response.status_code < 300:
            message = self._error_message(payload)
            logger.warning(
                "WB Promotion error path=%s status=%s message=%s",
                path,
                response.status_code,
                message,
            )
            retry_after = _retry_after_seconds(response) if response.status_code == 429 else None
            raise WBApiError(
                f"WB Продвижение: {message or response.reason_phrase}",
                status_code=response.status_code,
                payload=payload,
                retry_after=retry_after,
            )
        if isinstance(payload, dict) and payload.get("error"):
            message = self._error_message(payload) or "WB Продвижение вернуло ошибку"
            logger.warning("WB Promotion response error path=%s error=%s", path, message)
            raise WBApiError(message, status_code=response.status_code, payload=payload)
        return payload

    @staticmethod
    def _normalize_bid_type(bid_type: str) -> str:
        value = str(bid_type or "unified").strip().lower()
        if value not in {"manual", "unified"}:
            raise WBApiError("Некорректный тип ставки кампании", status_code=400)
        return value

    @staticmethod
    def _normalize_placement(placement: str, *, bid_type: str) -> str:
        value = str(placement or "combined").strip().lower()
        if bid_type == "unified":
            return "combined"
        if value == "recommendation":
            value = "recommendations"
        if value not in {"search", "recommendations"}:
            raise WBApiError("Для ручной ставки указано некорректное размещение", status_code=400)
        return value

    async def create_campaign(
        self,
        *,
        name: str,
        nm_id: int,
        placement: str = "combined",
        bid_type: str = "unified",
    ) -> int:
        normalized_bid_type = self._normalize_bid_type(bid_type)
        normalized_placement = self._normalize_placement(placement, bid_type=normalized_bid_type)
        payload_body: dict[str, Any] = {
            "name": name,
            "nms": [int(nm_id)],
            "bid_type": normalized_bid_type,
            "payment_type": "cpm",
        }
        # WB accepts placement_types only for manual campaigns. Unified
        # campaigns always cover search + recommendations through combined.
        if normalized_bid_type == "manual":
            payload_body["placement_types"] = [normalized_placement]
        payload = await self._request(
            "POST",
            "/adv/v2/seacat/save-ad",
            json=payload_body,
        )
        if isinstance(payload, int):
            return payload
        if isinstance(payload, str) and payload.isdigit():
            return int(payload)
        if isinstance(payload, dict):
            for key in ("advertId", "advert_id", "campaignId", "campaign_id", "id"):
                if payload.get(key) is not None:
                    return int(payload[key])
        raise WBApiError(f"Неожиданный ответ WB при создании кампании: {payload}", payload=payload)

    async def set_bid(
        self,
        *,
        campaign_id: int,
        nm_id: int,
        cpm_rub: int,
        placement: str = "combined",
        bid_type: str = "unified",
    ) -> Any:
        normalized_bid_type = self._normalize_bid_type(bid_type)
        normalized_placement = self._normalize_placement(placement, bid_type=normalized_bid_type)
        return await self._request(
            "PATCH",
            "/api/advert/v1/bids",
            json={
                "bids": [
                    {
                        "advert_id": int(campaign_id),
                        "nm_bids": [
                            {
                                "nm_id": int(nm_id),
                                "bid_kopecks": int(cpm_rub) * 100,
                                "placement": normalized_placement,
                            }
                        ],
                    }
                ]
            },
        )

    async def get_min_bid(
        self,
        *,
        campaign_id: int,
        nm_id: int,
        placement: str = "combined",
        bid_type: str = "unified",
    ) -> int | None:
        """Return the current minimum CPM in rubles when WB exposes it."""
        normalized_bid_type = self._normalize_bid_type(bid_type)
        normalized_placement = self._normalize_placement(placement, bid_type=normalized_bid_type)
        # The create/update bid APIs use ``recommendations`` while the
        # minimum-bid endpoint documents the singular ``recommendation``.
        # Unified campaigns use ``combined`` for both search and
        # recommendations.
        api_placement = (
            "combined"
            if normalized_bid_type == "unified"
            else "recommendation" if normalized_placement == "recommendations" else normalized_placement
        )
        payload = await self._request(
            "POST",
            "/api/advert/v1/bids/min",
            json={
                "advert_id": int(campaign_id),
                "nm_ids": [int(nm_id)],
                "payment_type": "cpm",
                "placement_types": [api_placement],
            },
        )
        rows = payload.get("bids", []) if isinstance(payload, dict) else payload if isinstance(payload, list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_nm = int(row.get("nm_id") or row.get("nmId") or nm_id)
            if row_nm != int(nm_id):
                continue
            for bid in row.get("bids") or row.get("values") or []:
                if not isinstance(bid, dict):
                    continue
                returned_bid_type = str(bid.get("type") or "").lower()
                if returned_bid_type not in {api_placement.lower(), "combined"}:
                    continue
                try:
                    value = Decimal(str(bid.get("value")))
                    if not value.is_finite() or value <= 0:
                        raise ValueError("invalid minimum bid")
                except (TypeError, ValueError, InvalidOperation) as exc:
                    raise WBApiError(
                        "Wildberries вернул минимальную ставку в неподдерживаемом формате; запуск заблокирован"
                    ) from exc
                currency = str(
                    bid.get("currency")
                    or bid.get("currency_code")
                    or row.get("currency")
                    or row.get("currencyCode")
                    or (payload.get("currency") if isinstance(payload, dict) else None)
                    or (payload.get("currencyCode") if isinstance(payload, dict) else None)
                    or "RUB"
                ).strip().upper()
                if currency not in {"RUB", "RUR", "₽"}:
                    raise WBApiError(
                        f"Wildberries вернул минимальную ставку в валюте {currency}; запуск заблокирован"
                    )
                unit = str(bid.get("unit") or bid.get("units") or bid.get("value_unit") or "").strip().lower()
                if unit:
                    kopeck_units = {"kopeck", "kopecks", "kop", "коп", "копейка", "копейки", "копеек"}
                    ruble_units = {"rub", "ruble", "rubles", "руб", "рубль", "рубли", "рублей"}
                    if unit in kopeck_units:
                        value /= 100
                    elif unit in ruble_units:
                        pass
                    else:
                        raise WBApiError(
                            "Wildberries вернул минимальную ставку в неизвестных единицах; запуск заблокирован"
                        )
                else:
                    # The documented v1 endpoint returns kopecks when no
                    # explicit unit is present.
                    value /= 100
                if value > 0:
                    return int(value.to_integral_value(rounding=ROUND_CEILING))
        return None

    async def get_balance(self) -> Any:
        return await self._request("GET", "/adv/v1/balance")

    async def is_card_advertisable(self, nm_id: int, *, subject_id: int | None = None) -> bool:
        """Read WB's list of cards available for campaigns before creation."""
        payload = await self._request("POST", "/adv/v2/supplier/nms", json=[int(subject_id)] if subject_id else [])
        if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
            raise WBApiError("WB не вернул список доступных для рекламы карточек")
        for item in payload:
            try:
                if int(item.get("nm") or item.get("nmID") or item.get("nm_id") or 0) == int(nm_id):
                    return True
            except (TypeError, ValueError):
                raise WBApiError("WB вернул некорректный идентификатор рекламируемого товара")
        return False

    @staticmethod
    def _amount(value: Any) -> float:
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            return max(float(value), 0.0)
        if isinstance(value, str):
            try:
                parsed = float(value.replace(",", "."))
                return max(parsed, 0.0) if math.isfinite(parsed) else 0.0
            except ValueError:
                return 0.0
        return 0.0

    @classmethod
    def normalize_balance(cls, payload: Any) -> dict[str, Any]:
        """Expose the funding sources returned by WB in a stable API shape."""
        if not isinstance(payload, dict):
            payload = {}

        cashbacks: list[dict[str, Any]] = []
        for item in payload.get("cashbacks") or []:
            if not isinstance(item, dict):
                continue
            cashbacks.append(
                {
                    "sum": round(cls._amount(item.get("sum")), 2),
                    "percent": max(int(cls._amount(item.get("percent"))), 0),
                    "expiration_date": item.get("expiration_date"),
                }
            )

        return {
            "account_balance": round(cls._amount(payload.get("balance")), 2),
            "mutual_balance": round(cls._amount(payload.get("net")), 2),
            "promo_bonus_balance": round(cls._amount(payload.get("bonus")), 2),
            "cashbacks": cashbacks,
        }

    async def get_budget(self, campaign_id: int) -> Any:
        return await self._request("GET", "/adv/v1/budget", params={"id": int(campaign_id)})

    async def get_budget_total(self, campaign_id: int) -> float:
        """Return the campaign balance in rubles.

        WB has returned both a flat ``total`` field and a nested budget object
        for different advertiser accounts, so the parser accepts both forms.
        It deliberately raises when no numeric balance is present instead of
        starting a campaign whose funding state is unknown.
        """
        payload = await self.get_budget(campaign_id)

        def find_total(node: Any) -> float | None:
            if isinstance(node, dict):
                for key in ("total", "totalBudget", "total_budget", "balance"):
                    value = node.get(key)
                    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
                        return max(float(value), 0.0)
                    if isinstance(value, str):
                        try:
                            amount = float(value.replace(",", "."))
                            if math.isfinite(amount) and amount >= 0:
                                return amount
                        except ValueError:
                            pass
                for value in node.values():
                    found = find_total(value)
                    if found is not None:
                        return found
            elif isinstance(node, list):
                for value in node:
                    found = find_total(value)
                    if found is not None:
                        return found
            return None

        total = find_total(payload)
        if total is None:
            raise WBApiError(f"Не удалось определить остаток бюджета кампании WB: {payload}", payload=payload)
        return total

    async def deposit_budget(
        self,
        *,
        campaign_id: int,
        amount_rub: int,
        source_type: int | None = None,
    ) -> Any:
        amount = int(amount_rub)
        if amount < 1:
            raise WBApiError("Сумма пополнения кампании должна быть больше 0 ₽", status_code=400)

        # The Promotion API has two different funding sources:
        #   0 — the seller's Promotion account (``balance``),
        #   1 — mutual-settlement balance (``net``).
        # Sending type=1 unconditionally makes a funded seller account look
        # empty to WB and results in an opaque HTTP 400.
        balance_payload = await self.get_balance()

        normalized = self.normalize_balance(balance_payload)
        account_balance = float(normalized["account_balance"])
        mutual_balance = float(normalized["mutual_balance"])
        promo_bonus_balance = float(normalized["promo_bonus_balance"])

        requested_source = None if source_type is None else int(source_type)
        if requested_source is not None and requested_source not in {0, 1, 3}:
            raise WBApiError("Некорректный источник пополнения кампании", status_code=400)

        # Without an explicit source keep the safe legacy order. The wizard
        # sends an explicit source after the user chooses it.
        if requested_source is None:
            if account_balance >= amount:
                selected_source = 0
            elif mutual_balance >= amount:
                selected_source = 1
            else:
                selected_source = None
        else:
            selected_source = requested_source

        available_by_source = {
            0: (account_balance, "счёта Продвижения"),
            1: (mutual_balance, "баланса взаиморасчётов"),
            3: (promo_bonus_balance, "промо-бонусов WB"),
        }
        if selected_source is None:
            available_text = (
                f"на счёте Продвижения доступно {int(account_balance)} ₽, "
                f"на балансе взаиморасчётов — {int(mutual_balance)} ₽, "
                f"промо-бонусов — {int(promo_bonus_balance)} ₽"
            )
            raise WBApiError(
                f"Недостаточно средств для пополнения кампании: нужно {amount} ₽, {available_text}. "
                "Пополните выбранный источник WB и повторите запуск.",
                status_code=400,
                payload={
                    "code": "insufficient_promotion_funds",
                    "required": amount,
                    "account_balance": account_balance,
                    "mutual_balance": mutual_balance,
                    "promo_bonus_balance": promo_bonus_balance,
                },
            )

        source_available, source_name = available_by_source[selected_source]
        if source_available < amount:
            raise WBApiError(
                (
                    f"Недостаточно средств для пополнения кампании: нужно {amount} ₽, "
                    f"в выбранном источнике ({source_name}) доступно {int(source_available)} ₽. "
                    "Пополните выбранный источник WB и повторите запуск."
                ),
                status_code=400,
                payload={
                    "code": "insufficient_promotion_funds",
                    "required": amount,
                    "source_type": selected_source,
                    "account_balance": account_balance,
                    "mutual_balance": mutual_balance,
                    "promo_bonus_balance": promo_bonus_balance,
                },
            )

        logger.info(
            "WB Promotion budget deposit campaign_id=%s amount_rub=%s source_type=%s source=%s available=%s",
            int(campaign_id),
            amount,
            selected_source,
            source_name,
            source_available,
        )
        return await self._request(
            "POST",
            "/adv/v1/budget/deposit",
            params={"id": int(campaign_id)},
            json={"sum": amount, "type": selected_source, "return": True},
        )

    async def start_campaign(self, campaign_id: int) -> Any:
        return await self._request("GET", "/adv/v0/start", params={"id": int(campaign_id)})

    async def pause_campaign(self, campaign_id: int) -> Any:
        return await self._request("GET", "/adv/v0/pause", params={"id": int(campaign_id)})

    async def stop_campaign(self, campaign_id: int) -> Any:
        return await self._request("GET", "/adv/v0/stop", params={"id": int(campaign_id)})

    async def list_campaigns(self, *, refresh: bool = False) -> list[dict[str, int]]:
        """Return campaign IDs and their WB statuses from promotion/count."""
        cache_key = hashlib.sha256(self.token.encode()).hexdigest()
        cached = self.__class__._campaign_cache.get(cache_key)
        if not refresh and cached and time.monotonic() - cached[0] <= self.__class__._campaign_cache_ttl:
            return [dict(item) for item in cached[1]]
        payload = await self._request("GET", "/adv/v1/promotion/count")
        def extract_groups(node: Any) -> list[dict[str, Any]]:
            if isinstance(node, list):
                return [item for item in node if isinstance(item, dict)]
            if not isinstance(node, dict):
                return []
            for key in ("adverts", "advert_list", "advertList", "items", "data"):
                value = node.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
                if isinstance(value, dict):
                    nested = extract_groups(value)
                    if nested:
                        return nested
            if node.get("advertId") or node.get("advert_id") or node.get("id"):
                return [node]
            return []

        groups = extract_groups(payload)
        result: list[dict[str, int]] = []
        seen: set[int] = set()
        for group in groups:
            if not isinstance(group, dict):
                continue
            try:
                status = int(group.get("statusId") or group.get("status") or group.get("status_id") or 0)
            except (TypeError, ValueError):
                status = 0
            adverts = group.get("advert_list") or group.get("advertList") or group.get("adverts") or []
            if not adverts and (group.get("advertId") or group.get("advert_id") or group.get("id")):
                adverts = [group]
            for advert in adverts if isinstance(adverts, list) else []:
                if not isinstance(advert, dict):
                    continue
                raw_id = advert.get("advertId") or advert.get("advert_id") or advert.get("id")
                if raw_id is None:
                    continue
                try:
                    campaign_id = int(raw_id)
                except (TypeError, ValueError):
                    continue
                if campaign_id not in seen:
                    seen.add(campaign_id)
                    try:
                        advert_status = int(
                            advert.get("statusId")
                            or advert.get("status")
                            or advert.get("status_id")
                            or status
                        )
                    except (TypeError, ValueError):
                        advert_status = status
                    result.append({"id": campaign_id, "status": advert_status})
        if len(self.__class__._campaign_cache) >= 256:
            self.__class__._campaign_cache.pop(next(iter(self.__class__._campaign_cache)))
        self.__class__._campaign_cache[cache_key] = (time.monotonic(), result)
        return [dict(item) for item in result]

    async def get_campaign_status(self, campaign_id: int, *, refresh: bool = True) -> int | None:
        """Read back the campaign status instead of trusting a mutation 200."""
        campaigns = await self.list_campaigns(refresh=refresh)
        target = int(campaign_id)
        for campaign in campaigns:
            if int(campaign.get("id", 0)) == target:
                return int(campaign.get("status", 0))
        return None

    async def find_campaigns_for_nm(self, nm_id: int, *, refresh: bool = True) -> list[dict[str, Any]]:
        """Discover external campaigns containing a card before creating one."""
        matches: list[dict[str, Any]] = []
        for campaign in await self.list_campaigns(refresh=refresh):
            campaign_id = int(campaign.get("id") or 0)
            if not campaign_id:
                continue
            details = await self.get_campaign_details(campaign_id)
            if int(nm_id) in self.campaign_nm_ids(details):
                matches.append({"id": campaign_id, "status": int(campaign.get("status") or 0), "details": details or {}})
        return matches

    async def get_campaign_details(self, campaign_id: int) -> dict[str, Any] | None:
        """Return one campaign from WB's current campaign details endpoint.

        ``promotion/count`` only contains an ID and status. It is not enough
        to prove that a campaign belongs to the selected product. The v2
        details endpoint includes ``nm_settings`` with the campaign's WB
        articles, so callers can verify the external object before depositing
        money or starting it.
        """
        payload = await self._request(
            "GET",
            "/api/advert/v2/adverts",
            params={"ids": str(int(campaign_id))},
        )
        rows = payload.get("adverts") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            return None
        target = int(campaign_id)
        for row in rows:
            if not isinstance(row, dict):
                continue
            raw_id = row.get("id") or row.get("advertId") or row.get("advert_id")
            try:
                if raw_id is not None and int(raw_id) == target:
                    return dict(row)
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def campaign_nm_ids(details: dict[str, Any] | None) -> set[int]:
        """Extract product IDs from the v2 campaign details response."""
        if not isinstance(details, dict):
            return set()
        result: set[int] = set()
        for key in ("nm_settings", "nmSettings", "nms", "nm_ids", "nmIds"):
            values = details.get(key)
            if not isinstance(values, list):
                continue
            for value in values:
                if isinstance(value, dict):
                    raw_id = value.get("nm_id") or value.get("nmId")
                else:
                    raw_id = value
                try:
                    if raw_id is not None and int(raw_id) > 0:
                        result.add(int(raw_id))
                except (TypeError, ValueError):
                    continue
        return result

    async def fullstats(
        self,
        campaign_id: int | list[int],
        *,
        started_at: date | None = None,
        end_at: date | None = None,
        refresh: bool = False,
    ) -> dict[str, Any]:
        campaign_ids = [int(campaign_id)] if isinstance(campaign_id, int) else [int(value) for value in campaign_id]
        campaign_ids = list(dict.fromkeys(campaign_ids))
        if not campaign_ids or len(campaign_ids) > 50:
            raise WBApiError("WB fullstats принимает от 1 до 50 кампаний за один запрос")
        end = end_at or datetime.now(ZoneInfo("Europe/Moscow")).date()
        begin = started_at or (end - timedelta(days=30))
        if begin > end:
            raise WBApiError("Начальная дата статистики не может быть позже конечной даты")
        if (end - begin).days > 30:
            raise WBApiError("Период fullstats не должен превышать 31 календарный день; запрос разбивается вызывающим сервисом")
        token_key = hashlib.sha256(self.token.encode()).hexdigest()
        stats_lock = self.__class__._stats_locks.setdefault(token_key, asyncio.Lock())
        cache_key = (
            hashlib.sha256(self.token.encode()).hexdigest(),
            tuple(campaign_ids),
            begin.isoformat(),
            end.isoformat(),
        )
        payload = None
        for attempt in range(4):
            try:
                async with stats_lock:
                    cached = self.__class__._stats_cache.get(cache_key)
                    if not refresh and cached and time.monotonic() - cached[0] <= self.__class__._stats_cache_ttl:
                        return dict(cached[1])
                    wait_for = 20.2 - (
                        time.monotonic() - self.__class__._last_stats_at.get(token_key, 0.0)
                    )
                    if wait_for > 0:
                        await asyncio.sleep(wait_for)
                    try:
                        payload = await self._request(
                            "GET",
                            "/adv/v3/fullstats",
                            params={
                                "ids": ",".join(str(value) for value in campaign_ids),
                                "beginDate": begin.isoformat(),
                                "endDate": end.isoformat(),
                            },
                        )
                    finally:
                        self.__class__._last_stats_at[token_key] = time.monotonic()
                break
            except WBApiError as exc:
                if exc.status_code not in {429, 500, 502, 503, 504} or attempt == 3:
                    raise
                # A provider supplied Retry-After is a lower bound, not a
                # suggestion. Never cap it locally: retrying at 60 seconds
                # when WB asks for 90 seconds only creates another 429.
                delay = max(float(exc.retry_after or 0), 20.2)
                await asyncio.sleep(delay)
        if payload is None:
            raise WBApiError("WB fullstats не вернул данные")
        if isinstance(payload, list):
            rows = payload
        elif isinstance(payload, dict):
            rows = payload.get("data") or payload.get("items") or ([payload] if "days" in payload or "advertId" in payload else [])
        else:
            rows = []
        if not isinstance(rows, list) or any(not isinstance(item, dict) for item in rows):
            raise WBApiError("WB fullstats вернул некорректный список наблюдений")
        row_dicts = [self._normalize_stats_node(item) for item in rows]
        if any(self._campaign_id(item) is None for item in row_dicts):
            raise WBApiError("WB fullstats не указал кампанию; статистика не может быть отнесена к тесту")
        identified_rows = [item for item in row_dicts if self._campaign_id(item) is not None]
        if identified_rows:
            requested_ids = set(campaign_ids)
            row_dicts = [item for item in row_dicts if self._campaign_id(item) in requested_ids]
        # WB can return the same campaign/day/placement row more than once.
        # A repeated observation must never become extra spend or impressions.
        # Keep genuinely different periods/placements distinct by including
        # their date and placement dimensions in the fingerprint.
        deduped_rows: list[dict[str, Any]] = []
        seen_rows: dict[tuple[int | None, tuple[tuple[str, str], ...]], str] = {}
        for item in row_dicts:
            if not self._has_any_metric(item):
                continue
            campaign = self._campaign_id(item)
            fingerprint = json.dumps(item, sort_keys=True, ensure_ascii=False, default=str)
            # The same campaign legitimately has several daily/placement
            # rows. Only an identical observation is a duplicate; using the
            # campaign ID alone incorrectly rejects normal multi-day fullstats
            # responses and either freezes or over-counts the test.
            dimension_keys = (
                "date", "day", "appType", "app_type", "nmId", "nmID", "nm_id",
                "placement", "placement_type", "type", "period",
                "variantPosition", "variant_position", "photoPosition", "photo_position",
                "abVariant", "ab_variant", "abTestVariant", "ab_test_variant",
            )
            dimensions = tuple(sorted((key, str(item[key])) for key in dimension_keys if key in item))
            row_key = (campaign, dimensions)
            if row_key in seen_rows:
                if seen_rows[row_key] != fingerprint:
                    raise WBApiError("WB fullstats вернул противоречивые строки одной кампании; статистика не принята")
                continue
            seen_rows[row_key] = fingerprint
            deduped_rows.append(item)
        valid_rows = deduped_rows
        if not valid_rows and not row_dicts and isinstance(payload, dict) and self._has_any_metric(payload):
            valid_rows = [payload]
        available_metrics = {
            "views": bool(valid_rows) and all(self._has_metric(row, "views", aliases=("shows", "impressions")) for row in valid_rows),
            "clicks": bool(valid_rows) and all(self._has_metric(row, "clicks") for row in valid_rows),
            "orders": bool(valid_rows) and all(self._has_metric(row, "orders") for row in valid_rows),
            "sum": bool(valid_rows) and all(self._has_metric(row, "sum", aliases=("spend", "cost")) for row in valid_rows),
        }
        totals = {
            "views": sum(self._metric_total(row, "views", aliases=("shows", "impressions")) for row in valid_rows),
            "clicks": sum(self._metric_total(row, "clicks") for row in valid_rows),
            "orders": sum(self._metric_total(row, "orders") for row in valid_rows),
            "sum": sum(self._metric_total(row, "sum", aliases=("spend", "cost")) for row in valid_rows),
            "_has_data": bool(valid_rows),
            "_available_metrics": available_metrics,
            "_data_complete": {campaign for campaign, _ in seen_rows} == set(campaign_ids) and all(available_metrics[key] for key in ("views", "clicks", "sum")),
            "_observed_at": datetime.now(timezone.utc).isoformat(),
        }
        variant_totals = self._extract_variant_totals(valid_rows)
        totals["_variant_attribution_complete"] = variant_totals is not None
        if variant_totals is not None:
            totals["_variant_totals"] = variant_totals
        if len(self.__class__._stats_cache) >= 256:
            self.__class__._stats_cache.pop(next(iter(self.__class__._stats_cache)))
        self.__class__._stats_cache[cache_key] = (time.monotonic(), totals)
        return dict(totals)

    def cached_fullstats(
        self,
        campaign_id: int | list[int],
        *,
        started_at: date | None = None,
        end_at: date | None = None,
    ) -> dict[str, Any] | None:
        """Return a recent result without reserving another WB API slot."""
        campaign_ids = [int(campaign_id)] if isinstance(campaign_id, int) else [int(value) for value in campaign_id]
        campaign_ids = list(dict.fromkeys(campaign_ids))
        if not campaign_ids or len(campaign_ids) > 50:
            return None
        end = end_at or datetime.now(ZoneInfo("Europe/Moscow")).date()
        begin = started_at or (end - timedelta(days=30))
        if begin > end:
            return None
        if (end - begin).days > 30:
            return None
        cache_key = (
            hashlib.sha256(self.token.encode()).hexdigest(),
            tuple(campaign_ids),
            begin.isoformat(),
            end.isoformat(),
        )
        cached = self.__class__._stats_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] <= self.__class__._stats_cache_ttl:
            return dict(cached[1])
        return None

    @classmethod
    def _normalize_stats_node(cls, node: dict[str, Any]) -> dict[str, Any]:
        """Reject malformed counters; de-duplicate each documented dimension."""
        result = dict(node)
        for metric in ("views", "shows", "impressions", "clicks", "orders", "sum", "sum_price", "spend", "cost"):
            if metric in node:
                value = node[metric]
                try:
                    number = float(str(value).replace(",", "."))
                except (TypeError, ValueError) as exc:
                    raise WBApiError(f"WB fullstats: некорректная метрика {metric}") from exc
                if isinstance(value, bool) or not math.isfinite(number) or number < 0:
                    raise WBApiError(f"WB fullstats: некорректная метрика {metric}")
                if metric in {"views", "shows", "impressions", "clicks", "orders"} and not number.is_integer():
                    raise WBApiError(f"WB fullstats: счётчик {metric} должен быть целым")
        dimensions = {"days": ("date", "day"), "dates": ("date", "day"), "apps": ("appType", "app_type"), "nm": ("nmId", "nmID", "nm_id"), "nms": ("nmId", "nmID", "nm_id")}
        for container, keys in dimensions.items():
            if container not in node:
                continue
            children = node[container]
            if not isinstance(children, list) or any(not isinstance(child, dict) for child in children):
                raise WBApiError(f"WB fullstats: некорректное измерение {container}")
            normalized = []
            seen = {}
            for child in children:
                child = cls._normalize_stats_node(child)
                identity = next((child[key] for key in keys if key in child), None)
                encoded = json.dumps(child, sort_keys=True, default=str)
                marker = str(identity) if identity is not None else encoded
                if marker in seen:
                    if seen[marker] != encoded:
                        raise WBApiError(f"WB fullstats: противоречивые строки {container}")
                    continue
                seen[marker] = encoded
                normalized.append(child)
            result[container] = normalized
        for key in ("stat", "stats", "result", "data"):
            if isinstance(node.get(key), dict):
                result[key] = cls._normalize_stats_node(node[key])
            elif isinstance(node.get(key), list):
                result[key] = [cls._normalize_stats_node(item) for item in node[key] if isinstance(item, dict)]
        return result

    @staticmethod
    def _variant_position(node: dict[str, Any]) -> int | None:
        """Read an explicit photo/stage attribution supplied by the provider."""
        for key in (
            "variantPosition", "variant_position", "photoPosition", "photo_position",
            "abVariant", "ab_variant", "abTestVariant", "ab_test_variant",
        ):
            raw = node.get(key)
            if isinstance(raw, bool):
                continue
            try:
                position = int(raw)
            except (TypeError, ValueError):
                continue
            if 0 <= position <= 5:
                return position
        return None

    @classmethod
    def _extract_variant_totals(cls, rows: list[dict[str, Any]]) -> dict[str, dict[str, float]] | None:
        """Return totals only when every metric row has explicit attribution.

        Campaign-level fullstats normally has no photo identity.  A provider
        adapter may add one of the explicit keys above; mixed attributed and
        unattributed rows are rejected so they can never produce a false CTR.
        """
        found: dict[str, dict[str, float]] = {}
        saw_metric = False

        def walk(node: object) -> bool:
            nonlocal saw_metric
            if isinstance(node, list):
                return all(walk(item) for item in node if isinstance(item, (dict, list)))
            if not isinstance(node, dict):
                return True
            has_metric = any(key in node for key in ("views", "shows", "impressions", "clicks", "sum", "spend", "cost"))
            if not has_metric:
                children = [value for key, value in node.items() if key in {"days", "dates", "apps", "nms", "nm", "data", "items"}]
                return all(walk(child) for child in children)
            saw_metric = True
            position = cls._variant_position(node)
            if position is None:
                return False

            # Attribution is trustworthy only when every metric row carries
            # the explicit variant identity and all metrics required by the
            # winner contract. Missing fields must not silently become zero.
            metric_keys = {
                "views": ("views", "shows", "impressions"),
                "clicks": ("clicks",),
                "orders": ("orders",),
                "sum": ("sum", "spend", "cost"),
            }
            values: dict[str, float] = {}
            for metric, aliases in metric_keys.items():
                if not any(alias in node for alias in aliases):
                    return False
                value = cls._metric_total(node, metric, aliases=aliases[1:])
                if not math.isfinite(value) or value < 0:
                    return False
                values[metric] = value
            if values["clicks"] > values["views"]:
                return False

            entry = found.setdefault(str(position), {"views": 0.0, "clicks": 0.0, "orders": 0.0, "sum": 0.0})
            for metric, value in values.items():
                entry[metric] += value
            return True

        if not walk(rows) or not saw_metric or not found:
            return None
        return {position: {key: round(value, 2) for key, value in values.items()} for position, values in found.items()}

    @classmethod
    def _metric_total(cls, node: Any, key: str, aliases: tuple[str, ...] = ()) -> float:
        if not isinstance(node, dict):
            return 0.0
        for metric_key in (key, *aliases):
            direct = node.get(metric_key)
            if isinstance(direct, (int, float)):
                return float(direct)
            if isinstance(direct, str):
                try:
                    return float(direct.replace(",", "."))
                except ValueError:
                    continue
        for container_key in ("days", "dates"):
            children = node.get(container_key)
            if isinstance(children, list):
                return sum(cls._metric_total(child, key, aliases) for child in children)
        apps = node.get("apps")
        if isinstance(apps, list):
            return sum(cls._metric_total(child, key, aliases) for child in apps)
        media = node.get("nm") or node.get("nms")
        if isinstance(media, list):
            return sum(cls._metric_total(child, key, aliases) for child in media)
        nested = node.get("stat") or node.get("stats") or node.get("result") or node.get("data")
        if isinstance(nested, dict):
            return cls._metric_total(nested, key, aliases)
        if isinstance(nested, list):
            return sum(cls._metric_total(child, key, aliases) for child in nested)
        return 0.0

    @classmethod
    def _has_metric(cls, node: Any, key: str, aliases: tuple[str, ...] = ()) -> bool:
        """Return whether a numeric metric is present, including WB wrappers."""
        if not isinstance(node, dict):
            return False
        for metric_key in (key, *aliases):
            direct = node.get(metric_key)
            if isinstance(direct, (int, float)):
                return True
            if isinstance(direct, str):
                try:
                    float(direct.replace(",", "."))
                    return True
                except ValueError:
                    pass
        for container_key in ("days", "dates", "apps", "nm", "nms"):
            children = node.get(container_key)
            if isinstance(children, list) and children and all(cls._has_metric(child, key, aliases) for child in children):
                return True
        nested = node.get("stat") or node.get("stats") or node.get("result") or node.get("data")
        if isinstance(nested, dict):
            return cls._has_metric(nested, key, aliases)
        if isinstance(nested, list):
            return any(cls._has_metric(child, key, aliases) for child in nested)
        return False

    @classmethod
    def _has_any_metric(cls, node: Any) -> bool:
        return any(
            cls._has_metric(node, key, aliases=aliases)
            for key, aliases in (
                ("views", ("shows", "impressions")),
                ("clicks", ()),
                ("orders", ()),
                ("sum", ("spend", "cost")),
            )
        )

    @staticmethod
    def _campaign_id(node: Any) -> int | None:
        if not isinstance(node, dict):
            return None
        for key in ("advertId", "advert_id", "campaignId", "campaign_id"):
            value = node.get(key)
            try:
                if value is not None and int(value) > 0:
                    return int(value)
            except (TypeError, ValueError):
                continue
        return None
