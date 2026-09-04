from __future__ import annotations

import logging
import secrets
import time
from typing import Any

import httpx

from app.core.config import settings


logger = logging.getLogger(__name__)


class WBApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        payload: Any = None,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload
        self.retry_after = retry_after


def _json_or_text(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


class WBContentClient:
    def __init__(self, token: str):
        self.token = token.strip()
        self.base_url = settings.wb_content_api_url.rstrip("/")

    @property
    def headers(self) -> dict[str, str]:
        # WB HeaderApiKey authorization expects the token itself, not Bearer.
        return {"Authorization": self.token, "Accept": "application/json"}

    @staticmethod
    def _error_message(payload: Any) -> str | None:
        if isinstance(payload, dict):
            for key in ("errorText", "message", "detail", "errorMessage", "description", "error", "errors"):
                value = payload.get(key)
                if value not in (None, "", False, []):
                    if isinstance(value, (dict, list)):
                        nested = WBContentClient._error_message(value)
                        if nested:
                            return nested
                    else:
                        return str(value)[:500]
            for value in payload.values():
                nested = WBContentClient._error_message(value)
                if nested:
                    return nested
        elif isinstance(payload, list):
            values = [WBContentClient._error_message(item) for item in payload]
            messages = [value for value in values if value]
            if messages:
                return "; ".join(messages[:3])[:500]
        elif payload not in (None, "", False):
            return str(payload)[:500]
        return None

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        timeout = max(settings.wb_request_timeout, 30.0)
        started = time.perf_counter()
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(method, f"{self.base_url}{path}", headers=self.headers, **kwargs)
        logger.info(
            "WB Content %s %s status=%s elapsed_ms=%s",
            method,
            path,
            response.status_code,
            int((time.perf_counter() - started) * 1000),
        )
        payload = _json_or_text(response)
        if not 200 <= response.status_code < 300:
            message = self._error_message(payload) or response.reason_phrase
            logger.warning("WB Content error path=%s status=%s message=%s", path, response.status_code, message)
            retry_after = None
            if response.status_code == 429:
                try:
                    retry_after = float(response.headers.get("Retry-After", "0"))
                except ValueError:
                    retry_after = None
            raise WBApiError(
                f"WB Content: {message or response.reason_phrase}",
                status_code=response.status_code,
                payload=payload,
                retry_after=retry_after,
            )
        if isinstance(payload, dict) and payload.get("error"):
            message = self._error_message(payload) or "WB Content вернул ошибку"
            logger.warning("WB Content response error path=%s status=%s error=%s", path, response.status_code, message)
            raise WBApiError(message, status_code=response.status_code, payload=payload)
        return payload

    @staticmethod
    def photo_urls(card: dict[str, Any]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for item in card.get("photos") or []:
            value = item if isinstance(item, str) else (
                item.get("big")
                or item.get("url")
                or item.get("full")
                or item.get("c936x1248")
                or item.get("c516x688")
                or item.get("c246x328")
                if isinstance(item, dict) else None
            )
            value = str(value or "").strip()
            if value and value not in seen:
                seen.add(value)
                result.append(value)
        return result

    @staticmethod
    def normalize_card(card: dict[str, Any]) -> dict[str, Any]:
        photos = WBContentClient.photo_urls(card)
        return {
            "nm_id": int(card.get("nmID") or card.get("nmId") or card.get("nm_id") or 0),
            "vendor_code": card.get("vendorCode") or card.get("vendor_code"),
            "title": card.get("title"),
            "brand": card.get("brand"),
            "main_photo_url": photos[0] if photos else None,
            "photos": photos,
        }

    async def list_cards_page(
        self, *, search: str = "", limit: int = 100, cursor: dict[str, Any] | None = None
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """Load one Content API page and return the cursor for the next page.

        The Content API deliberately returns only the first 100 cards without
        cursor pagination. Keeping the cursor at the client boundary prevents
        the card picker from silently hiding products after the first page.
        """
        search_value = str(search or "").strip()
        numeric_search = search_value.isdigit()
        if numeric_search:
            # WB's nmID search expects a numeric string. Removing leading
            # zeroes also avoids searching for a different text token.
            search_value = str(int(search_value))
        page_limit = 1 if numeric_search else min(max(int(limit), 1), 100)
        settings_body: dict[str, Any] = {
            "sort": {"ascending": False},
            "filter": {"withPhoto": -1, "allowedCategoriesOnly": True},
            "cursor": {**(cursor or {}), "limit": page_limit},
        }
        if search_value:
            settings_body["filter"]["textSearch"] = search_value
        payload = await self._request("POST", "/content/v2/get/cards/list", json={"settings": settings_body})
        response_data = payload.get("data") if isinstance(payload, dict) else None
        cards = payload.get("cards", []) if isinstance(payload, dict) else []
        if (not isinstance(cards, list) or not cards) and isinstance(response_data, dict):
            cards = response_data.get("cards", [])
        next_cursor = payload.get("cursor") if isinstance(payload, dict) else None
        if not isinstance(next_cursor, dict) and isinstance(response_data, dict):
            next_cursor = response_data.get("cursor")
        if not isinstance(next_cursor, dict):
            next_cursor = None
        normalized = [self.normalize_card(card) for card in cards if isinstance(card, dict)]
        if numeric_search:
            normalized = [card for card in normalized if card["nm_id"] == int(search_value)]
        return normalized, next_cursor

    async def list_cards(self, *, search: str = "", limit: int = 100) -> list[dict[str, Any]]:
        cards, _ = await self.list_cards_page(search=search, limit=limit)
        return cards

    async def list_all_cards(self, *, search: str = "", limit: int = 100, max_pages: int = 100) -> list[dict[str, Any]]:
        """Read all available pages with a safety cap for a very large catalog."""
        page_limit = min(max(int(limit), 1), 100)
        result: list[dict[str, Any]] = []
        cursor: dict[str, Any] | None = None
        seen_cursors: set[tuple[tuple[str, str], ...]] = set()
        for _ in range(max(1, max_pages)):
            page, next_cursor = await self.list_cards_page(search=search, limit=page_limit, cursor=cursor)
            result.extend(page)
            if not next_cursor:
                break
            # WB's cursor is based on updatedAt + nmID. If either value is
            # absent, sending the cursor back cannot advance the page safely.
            next_cursor = {
                key: next_cursor[key]
                for key in ("updatedAt", "nmID", "nmId", "limit")
                if key in next_cursor
            }
            if not {"updatedAt", "limit"}.issubset(next_cursor) or not any(
                key in next_cursor for key in ("nmID", "nmId")
            ):
                break
            marker = tuple(sorted((key, str(value)) for key, value in next_cursor.items()))
            if marker in seen_cursors:
                break
            seen_cursors.add(marker)
            cursor = next_cursor
            if len(page) < page_limit:
                break
        return result

    async def get_card(self, nm_id: int) -> dict[str, Any] | None:
        cards = await self.list_cards(search=str(nm_id), limit=100)
        return next((card for card in cards if card["nm_id"] == int(nm_id)), None)

    async def download_image(self, url: str, *, cache_bust: bool = False) -> tuple[bytes, str]:
        request_url = str(url)
        if cache_bust:
            separator = "&" if "?" in request_url else "?"
            request_url = f"{request_url}{separator}wb_optimizer_verify={secrets.token_hex(8)}"
        async with httpx.AsyncClient(timeout=90.0, follow_redirects=True) as client:
            response = await client.get(
                request_url,
                headers={"Cache-Control": "no-cache, no-store", "Pragma": "no-cache"},
            )
        logger.info("WB image download status=%s cache_bust=%s", response.status_code, cache_bust)
        if not 200 <= response.status_code < 300:
            raise WBApiError(f"Не удалось загрузить изображение: HTTP {response.status_code}", status_code=response.status_code)
        content_type = (response.headers.get("content-type") or "image/jpeg").split(";", 1)[0].strip().lower()
        if not content_type.startswith("image/"):
            raise WBApiError("Ссылка не ведёт на изображение")
        if len(response.content) > 32 * 1024 * 1024:
            raise WBApiError("Изображение больше допустимых 32 МБ")
        return response.content, content_type

    async def upload_media_file(
        self, *, nm_id: int, photo_number: int, content: bytes, filename: str, content_type: str
    ) -> Any:
        started = time.perf_counter()
        headers = {**self.headers, "X-Nm-Id": str(nm_id), "X-Photo-Number": str(photo_number)}
        async with httpx.AsyncClient(timeout=90.0) as client:
            response = await client.post(
                f"{self.base_url}/content/v3/media/file",
                headers=headers,
                files={"uploadfile": (filename, content, content_type)},
            )
        logger.info(
            "WB Media upload nm_id=%s photo_number=%s filename=%s bytes=%s status=%s elapsed_ms=%s",
            nm_id,
            photo_number,
            filename,
            len(content),
            response.status_code,
            int((time.perf_counter() - started) * 1000),
        )
        payload = _json_or_text(response)
        if not 200 <= response.status_code < 300:
            message = payload.get("errorText") if isinstance(payload, dict) else str(payload)
            logger.warning(
                "WB Media upload error nm_id=%s photo_number=%s status=%s message=%s",
                nm_id,
                photo_number,
                response.status_code,
                message,
            )
            raise WBApiError(f"WB Media: {message or response.reason_phrase}", status_code=response.status_code, payload=payload)
        if isinstance(payload, dict) and payload.get("error"):
            logger.warning(
                "WB Media response error nm_id=%s photo_number=%s error=%s",
                nm_id,
                photo_number,
                payload.get("errorText"),
            )
            raise WBApiError(str(payload.get("errorText") or "WB Media вернул ошибку"), payload=payload)
        return payload

    async def save_media(self, *, nm_id: int, urls: list[str]) -> Any:
        clean_urls = []
        seen: set[str] = set()
        for raw_url in urls:
            url = str(raw_url).strip()
            if url and url.startswith(("http://", "https://")) and url not in seen:
                seen.add(url)
                clean_urls.append(url)
        if len(clean_urls) > 30:
            raise WBApiError("В карточке Wildberries может быть не больше 30 изображений")
        if not clean_urls:
            raise WBApiError("Нельзя очистить все изображения карточки")
        return await self._request("POST", "/content/v3/media/save", json={"nmId": int(nm_id), "data": clean_urls})
