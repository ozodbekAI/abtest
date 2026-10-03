#!/usr/bin/env python3
"""Evaluate F01-F09 logs against the acceptance invariants.

This is intentionally conservative: it never treats absence of evidence as PASS.
It reports PASS/FAIL/NOT_PROVEN and prints the exact reason.
"""
from __future__ import annotations
import json, re, sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parent / "e2e4")


def read(s):
    return (ROOT / f"{s}.log").read_text(encoding="utf-8", errors="replace")


def has(text, pat):
    return re.search(pat, text, re.I | re.S) is not None


def last_json(text, marker="ИТОГ ПРИЛОЖЕНИЯ:"):
    i=text.rfind(marker)
    if i<0: return None
    tail=text[i+len(marker):]
    start=tail.find("{")
    end=tail.find("}\n", start)
    if start<0 or end<0: return None
    try: return json.loads(tail[start:end+1])
    except Exception: return None


def result(s, status, why):
    print(f"{s}: {status} — {why}")


def main():
    for sid in [f"F{i:02d}" for i in range(1,10)]:
        p=ROOT/f"{sid}.log"
        if not p.exists(): result(sid,"NOT_RUN","log отсутствует"); continue
        t=read(sid)
        if sid=="F01":
            ok=has(t,r"вариант 1 .*показы=1483 .*клики=88") and has(t,r"вариант 2 .*показы=1458 .*клики=43")
            result(sid,"PASS" if ok else "FAIL" if "ПРАВДА WB" in t else "NOT_PROVEN","точность фото-метрик и наличие WB truth")
        elif sid=="F02":
            ok=has(t,r"40 ?%.*20 минут") and has(t,r"статус=|ИТОГ ПРИЛОЖЕНИЯ")
            result(sid,"NOT_PROVEN","финальный лог нужно сверить с WB truth; runner не объявляет PASS автоматически")
        elif sid=="F03":
            ok=has(t,r"waiting_image_reupload") and has(t,r"'P3S', False|P3S.*False") and has(t,r"similar_variant_positions")
            result(sid,"PASS" if ok else "FAIL","similar-media safety / no false application")
        elif sid=="F04":
            ok=has(t,r"media=restored") and (has(t,r"campaign_state=stopped") or has(t,r'status\": \"stopped\"'))
            result(sid,"PASS" if ok else "FAIL","pause failure closes safely")
        elif sid=="F05":
            # A valid run must not contain a campaign running event after the stop request.
            stop_idx=t.find('нажимаем «Стоп»')
            if stop_idx<0: stop_idx=t.find('стоп: HTTP')
            post=t[stop_idx:] if stop_idx>=0 else t
            bad=has(post,r"camp=running") or has(post,r"кампания.*start|start.*кампани")
            ok=stop_idx>=0 and not bad and has(post,r"media=restored")
            result(sid,"PASS" if ok else "FAIL" if stop_idx>=0 else "NOT_PROVEN","после STOP не должно быть повторного запуска")
        elif sid=="F06":
            # Safe run: budget truth may be equal to cap, but stage-2/late data must not be 0/0.
            ok=has(t,r"ПРАВДА WB.*spent.*1500") and not has(t,r"вариант 2 .*показы=0 .*клики=0")
            result(sid,"PASS" if ok else "FAIL" if "ПРАВДА WB" in t else "NOT_PROVEN","budget guard + preservation of interrupted stage")
        elif sid=="F07":
            m=re.search(r"максимум:\s*([0-9]+)",t)
            ok=bool(m) and int(m.group(1))<=120
            result(sid,"PASS" if ok else "FAIL" if m else "NOT_PROVEN",f"максимальный gap статистики <=120s; observed={m.group(1) if m else 'n/a'}")
        elif sid=="F08":
            ok=has(t,r"та же карточка:\s*409") and has(t,r"чужой тест.*404") and not has(t,r"test_id.*ответе:\s*True")
            result(sid,"PASS" if ok else "FAIL","user isolation")
        elif sid=="F09":
            ok=has(t,r"campaign_state=stopped") or has(t,r"campaign.*status.*[46]")
            result(sid,"PASS" if ok else "NOT_PROVEN","slow media start must never launch ad")

if __name__ == "__main__": main()
