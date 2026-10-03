"""Общие функции сквозного прогона: API приложения + управление поддельным WB."""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path

import httpx

APP = "http://127.0.0.1:18900/api"
MOCK = "http://127.0.0.1:18901"
RUN = Path(os.environ.get("SERGEY_E2E_RUN", str(Path(__file__).resolve().parent.parent / ".sergey-e2e")))
C = httpx.Client(timeout=900, trust_env=False)


def mock(path: str, body=None):
    r = C.post(MOCK + path, json=body or {}) if body is not None or path.startswith("/_ctl/reset") else C.get(MOCK + path)
    r.raise_for_status()
    return r.json()


def mock_state():
    return C.get(MOCK + "/_ctl/state").json()


def mock_events(kind: str | None = None):
    ev = C.get(MOCK + "/_ctl/events").json()
    return [e for e in ev if kind is None or e["kind"] == kind]


def photo_bytes(label: str) -> bytes:
    return C.get(f"{MOCK}/_ctl/photo/{label}").content


class User:
    def __init__(self, email: str, password: str = "Passw0rd!e2e"):
        self.email = email
        self.password = password
        self.token = None

    def h(self, **extra):
        return {"Authorization": f"Bearer {self.token}", **extra}

    def register(self):
        r = C.post(f"{APP}/auth/register/start", json={
            "email": self.email, "password": self.password, "confirm_password": self.password,
            "first_name": "Тест", "last_name": "Приёмка"})
        assert r.status_code in (200, 202), r.text
        code = None
        for _ in range(20):
            log = (RUN / "backend.log").read_text(errors="ignore")
            found = re.findall(r"code for " + re.escape(self.email) + r": (\d{6})", log)
            if found:
                code = found[-1]
                break
            time.sleep(0.5)
        assert code, "код подтверждения не найден в логе"
        r = C.post(f"{APP}/auth/register/verify", json={"email": self.email, "code": code})
        assert r.status_code == 200, r.text
        self.token = r.json()["access_token"]
        return self

    def login(self):
        r = C.post(f"{APP}/auth/login", json={"email": self.email, "password": self.password})
        assert r.status_code == 200, r.text
        self.token = r.json()["access_token"]
        return self

    def _retry(self, fn):
        r = fn()
        if r.status_code == 401:
            self.login()
            r = fn()
        return r

    def get(self, path, **kw):
        return self._retry(lambda: C.get(APP + path, headers=self.h(), **kw))

    def post(self, path, json_body=None, headers=None, **kw):
        return self._retry(lambda: C.post(APP + path, headers=self.h(**(headers or {})), json=json_body, **kw))

    def delete(self, path):
        return C.delete(APP + path, headers=self.h())

    def connect(self, wb_token: str, name: str):
        r = self.post("/wb/connections", {"token": wb_token, "store_name": name})
        assert r.status_code == 200, r.text
        return r.json()

    def create_test(self, conn_id: int, nm: int, **params):
        body = {"connection_id": conn_id, "nm_id": nm, "title": f"E2E {nm}", "views_per_variant": 300,
                "cpm_rub": 300, "budget_rub": 1200, **params}
        r = self.post("/ab-tests", body)
        assert r.status_code == 201, r.text
        return r.json()

    def card_photo_urls(self, conn_id: int, nm: int):
        r = self.get(f"/ab-tests/cards/{nm}", params={"connection_id": conn_id})
        assert r.status_code == 200, r.text
        return r.json()["photos"]

    def set_source(self, test_id: int, pos: int, url: str):
        return self.post(f"/ab-tests/{test_id}/variants/{pos}/source", {"source_url": url})

    def upload(self, test_id: int, pos: int, data: bytes, name="v.jpg"):
        return C.post(f"{APP}/ab-tests/{test_id}/variants/{pos}", headers=self.h(),
                      files={"file": (name, data, "image/jpeg")})

    def start(self, test_id: int, key: str | None = None, reconfirm: bool = True, **body):
        flag = RUN / "start_in_progress"
        flag.touch()
        try:
            return self._start(test_id, key, reconfirm, **body)
        finally:
            flag.unlink(missing_ok=True)

    def _start(self, test_id, key, reconfirm, **body):
        fp = self.test(test_id).get("draft_fingerprint")
        payload = {"auto_deposit": True, "funding_source": "account", "draft_fingerprint": fp, **body}
        r = self.post(f"/ab-tests/{test_id}/start", payload,
                      headers={"X-Idempotency-Key": key or f"start-{uuid.uuid4()}"})
        # Как в интерфейсе: сервер пересчитал безопасный бюджет -> пользователь видит новую сумму и подтверждает её.
        if r.status_code == 409 and '"confirmation_outdated"' in r.text and reconfirm:
            detail = r.json()["detail"]
            self.last_reconfirmed_budget = detail.get("recalculated_budget")
            payload["draft_fingerprint"] = detail["draft_fingerprint"]
            r = self.post(f"/ab-tests/{test_id}/start", payload,
                          headers={"X-Idempotency-Key": f"start-{uuid.uuid4()}"})
        return r

    def test(self, test_id: int):
        return self.get(f"/ab-tests/{test_id}").json()


def brief(t: dict) -> dict:
    keys = ["status", "operation_state", "campaign_state", "media_status", "stats_quality", "current_variant_order",
            "winner_variant_order", "winner_decision", "total_views", "total_clicks", "total_spend_rub",
            "unallocated_views", "unallocated_clicks", "incident_id", "last_error", "wb_campaign_id"]
    out = {k: t.get(k) for k in keys}
    out["variants"] = [{k: v.get(k) for k in ("position", "source_type", "views", "clicks", "ctr", "spend_rub", "is_winner")}
                       for v in t.get("variants", [])]
    return out


def sql(query: str) -> str:
    import subprocess
    return subprocess.run(["psql", "-h", "127.0.0.1", "-p", "55432", "-U", "postgres", "-d", "wb_optimizer",
                           "-At", "-c", query], capture_output=True, text=True).stdout.strip()


def dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1, default=str)


def wb_token(seller: str, write: bool = True) -> str:
    """Токен в формате WB (JWT): sid продавца и битовая маска прав `s`; бит 30 = только чтение."""
    import base64
    def b64(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    perms = 0b111111111110 | (0 if write else (1 << 30))
    return f"{b64({'alg': 'ES256', 'typ': 'JWT'})}.{b64({'sid': seller, 's': perms, 'id': 'e2e-' + seller})}.c2lnbmF0dXJl"
