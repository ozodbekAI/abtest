"""Повторная проверка пакета 30.09 (final). Настройки приложения ПО УМОЛЧАНИЮ, встроенный планировщик.
Все тесты одновременно, у каждого свой продавец и карточка. Скорость показов 100/мин у всех
(кроме F06 — 600/мин для проверки запаса бюджета), задержка статистики WB 4 мин.
Запуск: python rr2.py F01 F02 ... ; логи в e2e4/<ID>.log
"""
import json
import sys
import threading
import time
import traceback
import uuid

import e2e_lib
from e2e_lib import User, brief, dump, mock, mock_events, mock_state, photo_bytes, wb_token

RUN = e2e_lib.RUN
SELLERS = [f"S{i}" for i in range(1, 13)]
CTR = {"ORIG": 0.02, "P2": 0.03, "P3": 0.06, "P4": 0.025, "UPL": 0.03, "EXT": 0.01}
BASE_NM = 3100


def setup_all():
    mock("/_ctl/reset")
    cards = {str(BASE_NM + i): {"seller": s, "labels": ["ORIG", "P2", "P3", "P4"]} for i, s in enumerate(SELLERS, start=1)}
    mock("/_ctl/setup", {
        "sellers": {s: {"balance": 1_000_000, "net": 0, "bonus": 0} for s in SELLERS},
        "tokens": {wb_token(s): {"seller": s, "content": "allowed", "promotion": "allowed"} for s in SELLERS},
        "ctr": CTR,
        "similar": {
            "ORIGS": {"base": "ORIG", "box": [620, 40, 860, 200], "ctr": 0.05},
            "P3S": {"base": "P3", "box": [760, 1040, 880, 1180], "ctr": 0.02},
        },
        "cards": cards,
    })
    mock("/_ctl/flags", {"views_per_min": 100, "stats_lag_sec": 240})


class Log:
    def __init__(self, sid):
        self.f = open(RUN / f"{sid}.log", "w")
        self.t0 = time.time()

    def __call__(self, *a):
        print(f"[{time.time() - self.t0:6.0f}s]", *a, file=self.f, flush=True)


def seller_of(nm):
    return SELLERS[nm - BASE_NM - 1]


def truth(log, nm):
    st = mock_state()
    card = st["cards"].get(str(nm))
    camps = {cid: {k: c[k] for k in ("status", "budget", "spent", "views", "clicks", "by_label")}
             for cid, c in st["campaigns"].items() if c["nms"] == [nm]}
    log("ПРАВДА WB: карточка", nm, card["slots"] if card else None)
    log("ПРАВДА WB: кампании", json.dumps(camps, ensure_ascii=False))
    log("ПРАВДА WB: пополнения", [(e["id"], e["amount"]) for e in mock_events("deposit") if str(e["id"]) in camps])
    return st, camps


def watch(log, u, tid, seconds, stop_when=("finished", "stopped", "failed"), track_sync=False):
    end = time.time() + seconds
    last = None
    syncs = []
    while time.time() < end:
        raw = u.test(tid)
        cur = brief(raw)
        if track_sync and raw.get("last_synced_at") and (not syncs or syncs[-1][1] != raw.get("last_synced_at")):
            syncs.append((round(time.time() - log.t0), raw.get("last_synced_at")))
        snap = (cur["status"], cur["current_variant_order"], cur["campaign_state"], cur["media_status"],
                cur["operation_state"], cur["stats_quality"])
        if snap != last:
            log(f"status={cur['status']} op={cur['operation_state']} var={cur['current_variant_order']} "
                f"camp={cur['campaign_state']} media={cur['media_status']} stats={cur['stats_quality']} "
                f"views={cur['total_views']} synced={raw.get('last_synced_at')} err={(cur['last_error'] or '')[:220]}")
            last = snap
        if stop_when and cur["status"] in stop_when:
            break
        time.sleep(10)
    if track_sync:
        gaps = [b[0] - a[0] for a, b in zip(syncs, syncs[1:])]
        log("обновления статистики (с от старта):", [s[0] for s in syncs])
        log("интервалы между обновлениями, с:", gaps, "максимум:", max(gaps) if gaps else None)
    return u.test(tid)


def setup_test(tag, nm, variants=(("card", 3), ("upload", "UPL")), **params):
    u = User(f"{tag}-{uuid.uuid4().hex[:8]}@example.com").register()
    conn = u.connect(wb_token(seller_of(nm)), f"Магазин {tag}")
    t = u.create_test(conn["id"], nm, **params)
    photos = u.card_photo_urls(conn["id"], nm)
    for pos, (kind, what) in enumerate(variants, start=1):
        r = u.set_source(t["id"], pos, photos[what - 1]) if kind == "card" else u.upload(t["id"], pos, photo_bytes(what))
        assert r.status_code == 200, r.text
    return u, conn, t["id"]


def result_block(log, u, tid, nm):
    raw = u.test(tid)
    log("ИТОГ ПРИЛОЖЕНИЯ:", dump({k: raw.get(k) for k in (
        "status", "operation_state", "campaign_state", "media_status", "stats_quality",
        "winner_variant_order", "winner_decision", "total_views", "total_clicks",
        "unallocated_views", "unallocated_clicks", "last_error")}))
    for v in raw.get("variants", []):
        log(f"  вариант {v['position']} ({v['source_type']}): показы={v['views']} клики={v['clicks']} CTR={v.get('ctr')} "
            f"точность={v.get('attribution') or v.get('ctr_quality') or v.get('stats_confidence')} победитель={v.get('is_winner')}")
    extra = {k: raw[k] for k in raw if any(w in k for w in ("winner", "attribution", "reconcil", "decision", "similar", "unresolved"))}
    log("ДОП. ПОЛЯ:", dump(extra)[:3000])
    return truth(log, nm)


# ----------------------------------------------------------------------------- scenarios
def F01(log):
    """Штатный тест: P3 (6 %) против UPL (3 %), по 1000 показов. Сравнить цифры с правдой, посмотреть решение о победителе."""
    nm = BASE_NM + 1
    u, conn, tid = setup_test("f01", nm, views_per_variant=1000)
    log("старт:", u.start(tid).status_code)
    watch(log, u, tid, 5400)
    result_block(log, u, tid, nm)


def F02(log):
    """Поздние клики: 40 % кликов приходят на 20 мин позже (дольше окна сверки). P3 против UPL."""
    nm = BASE_NM + 2
    mock("/_ctl/flags", {"nm": nm, "late_click_share": 0.4, "late_click_delay_sec": 1200})
    u, conn, tid = setup_test("f02", nm, views_per_variant=1000)
    log("старт:", u.start(tid).status_code)
    watch(log, u, tid, 5400)
    result_block(log, u, tid, nm)


def F03(log):
    """Похожее фото при переходе A → B: вариант 1 = P3, вариант 2 = P3S (P3 + плашка, pHash 6).
    Перед переходом WB начинает игнорировать запись ТОЛЬКО главного слота 1. Ожидается: B не объявлен
    установленным, реклама не идёт на старом фото под видом B."""
    nm = BASE_NM + 3
    u, conn, tid = setup_test("f03", nm, variants=(("card", 3), ("upload", "P3S")), views_per_variant=1000)
    log("старт:", u.start(tid).status_code)
    raw = u.test(tid)
    log("предупреждение о похожих фото в API:", raw.get("similar_variant_positions"))
    while brief(u.test(tid))["total_views"] < 300:
        time.sleep(10)
    mock("/_ctl/flags", {"nm": nm, "media_ignore_slots": [1]})
    log("включено: WB игнорирует запись главного слота 1 (остальные слоты применяются)")
    watch(log, u, tid, 2700, stop_when=("finished", "stopped", "failed"))
    result_block(log, u, tid, nm)
    log("загрузки:", [(e["slot"], e["label"], e["applied"]) for e in mock_events("media_upload") if e["nm"] == nm])


def F04(log):
    """Пауза не подтверждается (регресс)."""
    nm = BASE_NM + 4
    u, conn, tid = setup_test("f04", nm, views_per_variant=1000)
    log("старт:", u.start(tid).status_code)
    mock("/_ctl/flags", {"nm": nm, "pause_ignore": True})
    watch(log, u, tid, 2400)
    result_block(log, u, tid, nm)


def F05(log):
    """«Стоп» во время сверки статистики: время ответа и что происходит дальше."""
    nm = BASE_NM + 5
    u, conn, tid = setup_test("f05", nm, views_per_variant=1000)
    log("старт:", u.start(tid).status_code)
    while True:
        cur = brief(u.test(tid))
        if cur["campaign_state"] == "paused" or cur["status"] != "running":
            break
        time.sleep(5)
    time.sleep(90)
    log("кампания на паузе, идёт сверка. Нажимаем «Стоп».")
    t = time.time()
    r = u.post(f"/ab-tests/{tid}/stop")
    log(f"стоп: HTTP {r.status_code} за {time.time() - t:.1f} с; ответ: status={r.json().get('status')} "
        f"op={r.json().get('operation_state')} camp={r.json().get('campaign_state')} err={(r.json().get('last_error') or '')[:200]}")
    watch(log, u, tid, 1800)
    result_block(log, u, tid, nm)


def F06(log):
    """Высокий трафик 600/мин при задержке статистики 4 мин: хватает ли бюджета на второй этап,
    сохраняются ли данные второго этапа при прерывании."""
    nm = BASE_NM + 6
    mock("/_ctl/flags", {"nm": nm, "views_per_min": 600})
    u, conn, tid = setup_test("f06", nm, views_per_variant=1000)
    log("старт:", u.start(tid).status_code)
    watch(log, u, tid, 5400)
    result_block(log, u, tid, nm)


def F07(log):
    """Длинный этап 3000 показов: как часто планировщик обновляет этот тест, пока другие ждут фото и сверяют статистику."""
    nm = BASE_NM + 7
    u, conn, tid = setup_test("f07", nm, views_per_variant=3000)
    log("старт:", u.start(tid).status_code)
    watch(log, u, tid, 3000, stop_when=("finished", "stopped", "failed"), track_sync=True)
    raw = brief(u.test(tid))
    if raw["status"] == "running":
        r = u.post(f"/ab-tests/{tid}/stop")
        log("стоп (конец наблюдения):", r.status_code)
        watch(log, u, tid, 900)
    result_block(log, u, tid, nm)


def F08(log):
    """Два пользователя, один магазин, одна карточка (регресс)."""
    nm = BASE_NM + 8
    u1, c1, t1 = setup_test("f08a", nm, views_per_variant=1000)
    u2 = User(f"f08b-{uuid.uuid4().hex[:8]}@example.com").register()
    c2 = u2.connect(wb_token(seller_of(nm)), "тот же магазин")
    r = u2.post("/ab-tests", {"connection_id": c2["id"], "nm_id": nm, "title": "второй", "views_per_variant": 1000,
                              "cpm_rub": 300, "budget_rub": 1200})
    log("второй пользователь, та же карточка:", r.status_code, r.text[:300], "| test_id в ответе:", "test_id" in r.text)
    log("чужой тест по номеру:", u2.get(f"/ab-tests/{t1}").status_code)


def F09(log):
    """Медленный старт: WB не применяет фото варианта 1 (все слоты). Проверка, что это не тормозит другие тесты,
    и что реклама не запускается."""
    nm = BASE_NM + 9
    u, conn, tid = setup_test("f09", nm, variants=(("upload", "ORIGS"), ("card", 3)), views_per_variant=1000)
    mock("/_ctl/flags", {"nm": nm, "media_mode": "ignore"})
    t = time.time()
    r = u.start(tid)
    log(f"старт: HTTP {r.status_code} за {time.time() - t:.0f} с", r.text[:250])
    watch(log, u, tid, 900, stop_when=("failed", "stopped", "finished"))
    r = u.post(f"/ab-tests/{tid}/stop")
    log("стоп:", r.status_code)
    time.sleep(30)
    result_block(log, u, tid, nm)


def main(ids):
    setup_all()
    threads = []
    for sid in ids:
        log = Log(sid)
        log(f"### {sid}: {globals()[sid].__doc__}")

        def run(fn=globals()[sid], log=log):
            try:
                fn(log)
            except Exception as exc:  # noqa: BLE001
                log("СЦЕНАРИЙ УПАЛ:", repr(exc))
                log(traceback.format_exc())

        th = threading.Thread(target=run)
        th.start()
        threads.append(th)
        time.sleep(3)
    for th in threads:
        th.join()


if __name__ == "__main__":
    main(sys.argv[1:])
