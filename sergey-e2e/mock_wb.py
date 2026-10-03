"""Поддельный Wildberries для приёмочного прогона WB Optimizer.

Один процесс изображает Content API (/c), Promotion API (/p), Analytics (/a),
Statistics (/s) и CDN картинок (/cdn). Состояние карточек, кампаний, балансов
и все внешние действия хранятся здесь, отдельно от приложения, и доступны
через /_ctl/*. Ничего из этого не ходит в настоящий WB.
"""
from __future__ import annotations

import io
import threading
import time
from typing import Any

import imagehash
from fastapi import FastAPI, Header, Request, UploadFile, File
from fastapi.responses import JSONResponse, Response
from PIL import Image, ImageDraw

app = FastAPI()
LOCK = threading.Lock()
BASE = "http://127.0.0.1:18901"


def make_photo(label: str, seed: int) -> bytes:
    im = Image.new("RGB", (900, 1200), ((seed * 53) % 255, (seed * 97) % 255, (seed * 31) % 255))
    d = ImageDraw.Draw(im)
    for i in range(8):
        x = (seed * 71 + i * 113) % 700
        y = (i * 150 + seed * 17) % 1100
        d.rectangle([x, y, x + 180, y + 120], fill=((seed * 19 + i * 40) % 255, (i * 60) % 255, (seed * 7 + i * 25) % 255))
        d.ellipse([y % 800, x % 1000, y % 800 + 90, x % 1000 + 90], fill=((i * 90) % 255, (seed * 11) % 255, 200))
    d.text((40, 40), label, fill=(255, 255, 255))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=92)
    return out.getvalue()


import hashlib
LINEAGE: dict[str, str] = {}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def flag(name: str, nm: int | None = None):
    if nm is not None:
        override = S.get("card_flags", {}).get(int(nm), {})
        if name in override:
            return override[name]
    return S["flags"][name]


def phash(data: bytes):
    return imagehash.phash(Image.open(io.BytesIO(data)))


def to_webp(data: bytes) -> bytes:
    im = Image.open(io.BytesIO(data)).convert("RGB")
    out = io.BytesIO()
    im.save(out, "WEBP", quality=72)
    return out.getvalue()


def fresh_state() -> dict[str, Any]:
    return {
        "tokens": {},        # token -> {seller, content, promotion}
        "sellers": {},       # seller -> {balance, net, bonus}
        "cards": {},         # nm_id -> {seller, photos:[bytes], video:bool, updated}
        "labels": {},        # label -> {hash, ctr}
        "campaigns": {},     # id -> {...}
        "next_campaign": 700001 + (int(time.time()) % 10000) * 100,
        "events": [],
        "card_flags": {},
        "flags": {
            "media_mode": "apply",       # apply | ignore
            "start_fail": False,         # /adv/v0/start -> 400
            "pause_ignore": False,       # /adv/v0/pause -> 200 без смены статуса
            "stop_ignore": False,
            "stats_mode": "normal",      # normal | empty | dup | incomplete
            "traffic_per_call": 150,
            "min_bid_rub": 200,
            "views_per_min": 300,      # непрерывный трафик активной кампании
            "stats_lag_sec": 0,        # статистика WB запаздывает на N секунд
            "late_click_share": 0.0,   # доля кликов, приходящих с дополнительной задержкой
            "late_click_delay_sec": 0,
            "media_ignore_slots": [],  # WB отвечает 200, но эти слоты не меняет
        },
    }


S = fresh_state()


def ev(kind: str, **data: Any) -> None:
    S["events"].append({"t": round(time.time(), 3), "kind": kind, **data})


def seller_for(auth: str | None, category: str) -> tuple[str | None, JSONResponse | None]:
    tok = S["tokens"].get((auth or "").strip())
    if not tok:
        return None, JSONResponse({"title": "unauthorized"}, status_code=401)
    if tok.get(category) != "allowed":
        return None, JSONResponse({"title": "forbidden"}, status_code=403)
    return tok["seller"], None


def label_of(data: bytes) -> str:
    known = LINEAGE.get(sha(data))
    if known:
        return known
    h = phash(data)
    best, dist = "unknown", 99
    for name, meta in S["labels"].items():
        d = h - meta["hash"]
        if d < dist:
            best, dist = name, d
    return best if dist <= 8 else "unknown"


# ---------------------------------------------------------------- control
@app.post("/_ctl/reset")
async def ctl_reset():
    global S
    with LOCK:
        S = fresh_state()
    return {"ok": True}


@app.post("/_ctl/setup")
async def ctl_setup(req: Request):
    """{"sellers": {...}, "tokens": {...}, "cards": {nm: {seller, labels:[..]}}, "ctr": {label: ctr}}"""
    body = await req.json()
    with LOCK:
        S["sellers"].update(body.get("sellers", {}))
        S["tokens"].update(body.get("tokens", {}))
        for seed, (label, ctr) in enumerate(body.get("ctr", {}).items(), start=1):
            data = make_photo(label, seed * 7 + len(label))
            S["labels"][label] = {"hash": phash(data), "ctr": float(ctr), "bytes": data}
            LINEAGE[sha(data)] = label
        for label, spec in body.get("similar", {}).items():
            base = S["labels"][spec["base"]]["bytes"]
            im = Image.open(io.BytesIO(base)).convert("RGB")
            d = ImageDraw.Draw(im)
            x, y, w, h = spec.get("box", [620, 40, 860, 200])
            d.rectangle([x, y, x + w - x, y + h - y], fill=tuple(spec.get("color", [230, 30, 30])))
            out = io.BytesIO(); im.save(out, "JPEG", quality=92); data = out.getvalue()
            S["labels"][label] = {"hash": phash(data), "ctr": float(spec["ctr"]), "bytes": data}
            LINEAGE[sha(data)] = label
        for nm, card in body.get("cards", {}).items():
            S["cards"][int(nm)] = {
                "seller": card["seller"],
                "photos": [S["labels"][lb]["bytes"] for lb in card["labels"]],
                "video": bool(card.get("video")),
                "updated": time.time(),
            }
    return {"ok": True}


@app.get("/_ctl/photo/{label}")
async def ctl_photo(label: str):
    return Response(S["labels"][label]["bytes"], media_type="image/jpeg")


@app.post("/_ctl/flags")
async def ctl_flags(req: Request):
    body = await req.json()
    with LOCK:
        nm = body.pop("nm", None)
        if nm is not None:
            S["card_flags"].setdefault(int(nm), {}).update(body)
            body["nm"] = nm
        else:
            S["flags"].update(body)
        ev("ctl_flags", **body)
    return S["flags"]


@app.post("/_ctl/card_edit")
async def ctl_card_edit(req: Request):
    """Внешнее изменение карточки продавцом: {"nm": .., "slot": n, "label": ..}"""
    body = await req.json()
    with LOCK:
        card = S["cards"][int(body["nm"])]
        data = S["labels"][body["label"]]["bytes"]
        slot = int(body["slot"])
        if slot <= len(card["photos"]):
            card["photos"][slot - 1] = data
        else:
            card["photos"].append(data)
        ev("external_card_edit", nm=int(body["nm"]), slot=slot, label=body["label"])
    return {"ok": True}


@app.post("/_ctl/campaign_status")
async def ctl_campaign_status(req: Request):
    body = await req.json()
    with LOCK:
        S["campaigns"][int(body["id"])]["status"] = int(body["status"])
        ev("external_campaign_status", id=int(body["id"]), status=int(body["status"]))
    return {"ok": True}


@app.get("/_ctl/state")
async def ctl_state():
    with LOCK:
        cards = {
            nm: {"seller": c["seller"], "slots": [label_of(p) for p in c["photos"]], "video": c["video"]}
            for nm, c in S["cards"].items()
        }
        camps = {
            cid: {k: v for k, v in c.items() if k not in ("by_label", "history", "acc_by_label")} | {"by_label": dict(c["by_label"])}
            for cid, c in S["campaigns"].items()
        }
        return {"cards": cards, "campaigns": camps, "sellers": S["sellers"], "flags": S["flags"]}


@app.get("/_ctl/events")
async def ctl_events():
    return S["events"]


# ---------------------------------------------------------------- ping
for prefix, cat in (("/c", "content"), ("/p", "promotion"), ("/a", "analytics"), ("/s", "statistics")):
    def _make(cat: str):
        async def ping(authorization: str | None = Header(None)):
            tok = S["tokens"].get((authorization or "").strip())
            if not tok:
                return JSONResponse({"title": "unauthorized"}, status_code=401)
            if tok.get(cat, "allowed") != "allowed":
                return JSONResponse({"title": "forbidden"}, status_code=403)
            return {"TS": time.time(), "Status": "OK"}
        return ping
    app.get(f"{prefix}/ping")(_make(cat))


# ---------------------------------------------------------------- content
def card_json(nm: int, card: dict) -> dict:
    stamp = int(card["updated"] * 1000)
    return {
        "nmID": nm,
        "vendorCode": f"VC-{nm}",
        "title": f"Товар {nm}",
        "brand": "Test",
        "subjectID": 1,
        "updatedAt": "2026-09-26T10:00:00Z",
        "photos": [
            {"big": f"{BASE}/cdn/{nm}/{i}.webp?v={stamp}", "c246x328": f"{BASE}/cdn/{nm}/{i}.webp?v={stamp}&s=small"}
            for i in range(1, len(card["photos"]) + 1)
        ],
        **({"video": f"{BASE}/cdn/{nm}/video.mp4"} if card["video"] else {}),
    }


@app.post("/c/content/v2/get/cards/list")
async def cards_list(req: Request, authorization: str | None = Header(None)):
    seller, err = seller_for(authorization, "content")
    if err:
        return err
    body = await req.json()
    flt = (body.get("settings") or {}).get("filter") or {}
    search = str(flt.get("textSearch") or "")
    with LOCK:
        cards = [card_json(nm, c) for nm, c in S["cards"].items() if c["seller"] == seller]
    if search:
        cards = [c for c in cards if str(c["nmID"]) == search or search.lower() in c["title"].lower()]
    return {"cards": cards, "cursor": {"total": len(cards)}}


@app.get("/cdn/{nm}/{slot}.webp")
async def cdn(nm: int, slot: int):
    card = S["cards"].get(nm)
    if not card or slot > len(card["photos"]):
        return Response(status_code=404)
    src = card["photos"][slot - 1]
    out = to_webp(src)
    LINEAGE.setdefault(sha(out), label_of(src))
    return Response(out, media_type="image/webp")


@app.post("/c/content/v3/media/file")
async def media_file(
    uploadfile: UploadFile = File(...),
    authorization: str | None = Header(None),
    x_nm_id: str = Header(...),
    x_photo_number: str = Header(...),
):
    seller, err = seller_for(authorization, "content")
    if err:
        return err
    nm, slot = int(x_nm_id), int(x_photo_number)
    data = await uploadfile.read()
    with LOCK:
        card = S["cards"].get(nm)
        if not card or card["seller"] != seller:
            return JSONResponse({"error": True, "errorText": "nm not found"}, status_code=400)
        applied = flag("media_mode", nm) == "apply" and slot not in (flag("media_ignore_slots", nm) or [])
        if applied:
            if slot <= len(card["photos"]):
                card["photos"][slot - 1] = data
            else:
                card["photos"].append(data)
            card["updated"] = time.time()
        ev("media_upload", nm=nm, slot=slot, label=label_of(data), applied=applied)
    return {"data": None, "error": False, "errorText": "", "additionalErrors": None}


@app.post("/c/content/v3/media/save")
async def media_save(req: Request, authorization: str | None = Header(None)):
    seller, err = seller_for(authorization, "content")
    if err:
        return err
    body = await req.json()
    ev("media_save", nm=body.get("nmId"), urls=body.get("data"))
    return {"data": None, "error": False, "errorText": ""}


# ---------------------------------------------------------------- promotion
def camp_for(seller: str, cid: int) -> dict | None:
    c = S["campaigns"].get(int(cid))
    return c if c and c["seller"] == seller else None


@app.get("/common/api/v1/seller-info")
async def seller_info(authorization: str | None = Header(None)):
    tok = S["tokens"].get((authorization or "").strip())
    if not tok:
        return JSONResponse({"title": "unauthorized"}, status_code=401)
    return {"sid": tok["seller"], "name": f"Seller {tok['seller']}", "tradeMark": "Test"}


@app.get("/common/ping")
async def common_ping(authorization: str | None = Header(None)):
    return {"TS": time.time(), "Status": "OK"}


@app.post("/p/adv/v2/supplier/nms")
async def supplier_nms(authorization: str | None = Header(None)):
    seller, err = seller_for(authorization, "promotion")
    if err:
        return err
    return [{"nm": nm, "subject": 1} for nm, c in S["cards"].items() if c["seller"] == seller]


@app.post("/p/adv/v2/seacat/save-ad")
async def save_ad(req: Request, authorization: str | None = Header(None)):
    seller, err = seller_for(authorization, "promotion")
    if err:
        return err
    body = await req.json()
    with LOCK:
        cid = S["next_campaign"]
        S["next_campaign"] += 1
        S["campaigns"][cid] = {
            "seller": seller, "nms": list(body.get("nms") or []), "status": 4, "bid_kop": 0,
            "budget": 0.0, "spent": 0.0, "views": 0, "clicks": 0, "by_label": {},
            "bid_type": body.get("bid_type"), "payment_type": body.get("payment_type"), "name": body.get("name"),
            "history": [(time.time(), 0, 0.0, 0.0)], "acc_clicks": 0.0,
        }
        ev("campaign_created", id=cid, nms=body.get("nms"), seller=seller)
    return cid


@app.post("/p/api/advert/v1/bids/min")
async def bids_min(req: Request, authorization: str | None = Header(None)):
    seller, err = seller_for(authorization, "promotion")
    if err:
        return err
    body = await req.json()
    kop = int(S["flags"]["min_bid_rub"]) * 100
    return {"bids": [{"nm_id": nm, "bids": [{"type": "combined", "value": kop}]} for nm in body.get("nm_ids", [])]}


@app.patch("/p/api/advert/v1/bids")
async def bids_set(req: Request, authorization: str | None = Header(None)):
    seller, err = seller_for(authorization, "promotion")
    if err:
        return err
    body = await req.json()
    with LOCK:
        for b in body.get("bids", []):
            c = camp_for(seller, b["advert_id"])
            if c:
                for nb in b.get("nm_bids", []):
                    c["bid_kop"] = int(nb["bid_kopecks"])
                ev("bid_set", id=b["advert_id"], bid_kop=c["bid_kop"])
    return {"bids": body.get("bids", [])}


@app.get("/p/adv/v1/balance")
async def balance(authorization: str | None = Header(None)):
    seller, err = seller_for(authorization, "promotion")
    if err:
        return err
    b = S["sellers"][seller]
    return {"balance": b["balance"], "net": b["net"], "bonus": b["bonus"], "cashbacks": []}


@app.get("/p/adv/v1/budget")
async def budget(id: int, authorization: str | None = Header(None)):
    seller, err = seller_for(authorization, "promotion")
    if err:
        return err
    c = camp_for(seller, id)
    if not c:
        return JSONResponse({"error": "not found"}, status_code=400)
    return {"cash": 0, "netting": 0, "total": int(max(c["budget"] - c["spent"], 0))}


@app.post("/p/adv/v1/budget/deposit")
async def deposit(req: Request, id: int, authorization: str | None = Header(None)):
    seller, err = seller_for(authorization, "promotion")
    if err:
        return err
    body = await req.json()
    amount, src = int(body["sum"]), int(body.get("type", 0))
    key = {0: "balance", 1: "net", 3: "bonus"}[src]
    with LOCK:
        c = camp_for(seller, id)
        if not c:
            return JSONResponse({"error": "not found"}, status_code=400)
        if S["sellers"][seller][key] < amount:
            return JSONResponse({"error": "insufficient"}, status_code=400)
        S["sellers"][seller][key] -= amount
        c["budget"] += amount
        ev("deposit", id=id, amount=amount, source=key, seller=seller)
    return {"total": int(c["budget"] - c["spent"])}


@app.get("/p/adv/v0/start")
async def start(id: int, authorization: str | None = Header(None)):
    seller, err = seller_for(authorization, "promotion")
    if err:
        return err
    with LOCK:
        c = camp_for(seller, id)
        if not c:
            return JSONResponse({"error": "not found"}, status_code=400)
        if flag("start_fail", c["nms"][0]):
            ev("start_rejected", id=id)
            return JSONResponse({"error": "start rejected (mock flag)"}, status_code=400)
        if c["status"] not in (4, 11) or c["budget"] - c["spent"] <= 0:
            ev("start_refused_state", id=id, status=c["status"])
            return JSONResponse({"error": f"cannot start from {c['status']}"}, status_code=400)
        c["status"] = 9
        ev("start", id=id)
    return Response(status_code=200)


@app.get("/p/adv/v0/pause")
async def pause(id: int, authorization: str | None = Header(None)):
    seller, err = seller_for(authorization, "promotion")
    if err:
        return err
    with LOCK:
        c = camp_for(seller, id)
        if not c:
            return JSONResponse({"error": "not found"}, status_code=400)
        if flag("pause_ignore", c["nms"][0]):
            ev("pause_ignored", id=id)
            return Response(status_code=200)
        if c["status"] == 9:
            c["status"] = 11
        ev("pause", id=id, status=c["status"])
    return Response(status_code=200)


@app.get("/p/adv/v0/stop")
async def stop(id: int, authorization: str | None = Header(None)):
    seller, err = seller_for(authorization, "promotion")
    if err:
        return err
    with LOCK:
        c = camp_for(seller, id)
        if not c:
            return JSONResponse({"error": "not found"}, status_code=400)
        if flag("stop_ignore", c["nms"][0]):
            ev("stop_ignored", id=id)
            return Response(status_code=200)
        c["status"] = 7
        ev("stop", id=id)
    return Response(status_code=200)


@app.get("/p/adv/v1/promotion/count")
async def promo_count(authorization: str | None = Header(None)):
    seller, err = seller_for(authorization, "promotion")
    if err:
        return err
    groups: dict[int, list] = {}
    with LOCK:
        for cid, c in S["campaigns"].items():
            if c["seller"] == seller:
                groups.setdefault(c["status"], []).append({"advertId": cid, "changeTime": "2026-09-26T10:00:00Z"})
    return {"adverts": [{"type": 9, "status": st, "count": len(lst), "advert_list": lst} for st, lst in groups.items()], "all": sum(len(v) for v in groups.values())}


@app.get("/p/api/advert/v2/adverts")
async def adverts(ids: str, authorization: str | None = Header(None)):
    seller, err = seller_for(authorization, "promotion")
    if err:
        return err
    out = []
    for raw in ids.split(","):
        c = camp_for(seller, int(raw))
        if c:
            out.append({
                "id": int(raw), "status": c["status"], "bid_type": c["bid_type"],
                "settings": {"payment_type": c["payment_type"], "name": c["name"]},
                "nm_settings": [{"nm_id": nm} for nm in c["nms"]],
            })
    return {"adverts": out}


TICK_SEC = 5.0


def generate_traffic(dt: float = TICK_SEC) -> None:
    """Непрерывный трафик: каждые 5 с активная кампания получает показы по ставке
    views_per_min. Клики — по «настоящему» CTR фото, которое СЕЙЧАС стоит главным.
    Это правда, с которой сравниваются выводы приложения. История накопительных
    счётчиков нужна, чтобы отдавать статистику с запаздыванием, как у WB."""
    now = time.time()
    for cid, c in S["campaigns"].items():
        nm = c["nms"][0]
        if c["status"] == 9 and c["budget"] - c["spent"] > 0:
            bid_rub = (c["bid_kop"] or 20000) / 100
            c["acc_views"] = c.get("acc_views", 0.0) + float(flag("views_per_min", nm)) * dt / 60.0
            views = int(c["acc_views"]); c["acc_views"] -= views
            cost = views * bid_rub / 1000
            remaining = c["budget"] - c["spent"]
            if cost > remaining:
                views = int(remaining * 1000 / bid_rub); cost = views * bid_rub / 1000
            if views > 0:
                card = S["cards"].get(nm)
                label = label_of(card["photos"][0]) if card and card["photos"] else "unknown"
                ctr = S["labels"].get(label, {}).get("ctr", 0.02)
                acc = c.setdefault("acc_by_label", {})
                acc[label] = acc.get(label, 0.0) + views * ctr
                clicks = int(acc[label]); acc[label] -= clicks
                c["views"] += views; c["clicks"] += clicks; c["spent"] += cost
                bl = c["by_label"].setdefault(label, {"views": 0, "clicks": 0, "spent": 0.0})
                bl["views"] += views; bl["clicks"] += clicks; bl["spent"] += cost
        c.setdefault("history", []).append((now, c["views"], float(c["clicks"]), c["spent"]))
        if len(c["history"]) > 5000:
            del c["history"][:1000]


def value_at(c: dict, t: float) -> tuple[int, float, float]:
    hist = c.get("history") or [(0, 0, 0.0, 0.0)]
    best = (0, 0.0, 0.0)
    for ts, v, cl, sp in hist:
        if ts <= t:
            best = (v, cl, sp)
        else:
            break
    return best


async def _traffic_loop():
    import asyncio
    while True:
        with LOCK:
            generate_traffic()
        await asyncio.sleep(TICK_SEC)


@app.on_event("startup")
async def _start_traffic():
    import asyncio
    asyncio.get_event_loop().create_task(_traffic_loop())


@app.get("/p/adv/v3/fullstats")
async def fullstats(ids: str, beginDate: str, endDate: str, authorization: str | None = Header(None)):
    seller, err = seller_for(authorization, "promotion")
    if err:
        return err
    mode = S["flags"]["stats_mode"]
    now = time.time()
    with LOCK:
        rows = []
        for raw in ids.split(","):
            c = camp_for(seller, int(raw))
            if not c:
                continue
            nm = c["nms"][0]
            lag = float(flag("stats_lag_sec", nm))
            v, cl, sp = value_at(c, now - lag)
            share = float(flag("late_click_share", nm))
            if share > 0:
                _, cl_old, _ = value_at(c, now - lag - float(flag("late_click_delay_sec", nm)))
                cl = (1 - share) * cl + share * cl_old
            row = {"advertId": int(raw), "views": int(v), "clicks": int(cl), "sum": round(sp, 2), "orders": 0}
            if mode == "incomplete":
                row.pop("sum")
            rows.append(row)
        if mode == "dup":
            rows = rows + [dict(r) for r in rows]
        if mode == "empty":
            rows = []
        ev("fullstats", ids=ids, mode=mode, rows=len(rows), reported=[(r["advertId"], r["views"], r["clicks"]) for r in rows])
    return rows
