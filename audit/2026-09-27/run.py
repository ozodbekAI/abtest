"""Repeatable acceptance run: disposable PostgreSQL + HTTP backend + synthetic WB.

Run from repository root: venv/bin/python audit/2026-09-27/run.py
AUDIT_CASES optionally selects comma-separated scenario function names.
No business methods, counters, verification attempts or delays are replaced.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import uuid

import jwt
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parent
SRC = ROOT.parents[1]
H = ROOT / "harness"
OUT = ROOT / "results" / time.strftime("run-%Y%m%d-%H%M%S")
OUT.mkdir(parents=True)
RUNTIME = Path(tempfile.mkdtemp(prefix="abtest-audit-"))
PG = Path("/usr/lib/postgresql/16/bin")
PY = sys.executable
ENV = {**os.environ, "ABTEST_SRC": str(SRC), "PYTHONPATH": str(H) + ":" + str(SRC / "backend"),
       "PYTHONDONTWRITEBYTECODE": "1", "APP_ENV": "test", "AUTO_CREATE_TABLES": "false",
       "JWT_SECRET_KEY": "synthetic-audit-only", "FERNET_KEY": Fernet.generate_key().decode(),
       "MEDIA_SIGNING_SECRET": "synthetic-media-only", "SMTP_HOST": "", "ADMIN_EMAIL": "", "ADMIN_PASSWORD": "",
       "WB_CONTENT_API_URL": "http://127.0.0.1:18901/c", "WB_ADVERT_API_URL": "http://127.0.0.1:18901/p",
       "WB_COMMON_API_URL": "http://127.0.0.1:18901/common", "WB_ANALYTICS_API_URL": "http://127.0.0.1:18901/a",
       "WB_STATISTICS_API_URL": "http://127.0.0.1:18901/s", "AB_TEST_SCHEDULER_INTERVAL_SEC": "36000",
       "AB_TEST_SCHEDULER_PER_TEST_TIMEOUT_SEC": "900", "RUN": str(OUT), "NO_PROXY": "127.0.0.1,localhost"}
os.environ["RUN"] = str(OUT)
sys.path.insert(0, str(H))
import e2e_lib as e

PROCS = []
BACK = None
CURRENT = OUT
DB = "postgres"
CASE_ENV = ENV
TRACE = []
TOKEN = jwt.encode({"sid": "S1", "s": 66, "exp": int(time.time()) + 86400}, "audit-token-only")
TOKEN2 = jwt.encode({"sid": "S1", "s": 66, "exp": int(time.time()) + 86400, "id": "manager"}, "audit-token-only")


def write(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def sql(query, database=None):
    result = subprocess.run([str(PG / "psql"), "-h", "127.0.0.1", "-p", "55439", "-U", "audit", "-d", database or DB,
                             "-At", "-v", "ON_ERROR_STOP=1", "-c", query], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def stop(proc):
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def ready(url, proc):
    for _ in range(60):
        if proc.poll() is not None:
            raise RuntimeError("Service exited; see backend/mock log")
        try:
            if e.C.get(url).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(.5)
    raise RuntimeError("Service did not become ready")


def setup(name, nm=92000):
    global CURRENT, DB, BACK, CASE_ENV, TRACE
    stop(BACK)
    TRACE = []
    CURRENT = OUT / name
    CURRENT.mkdir()
    DB = "audit_" + name.lower()
    sql("CREATE DATABASE " + DB, "postgres")
    CASE_ENV = {**ENV, "DATABASE_URL": f"postgresql+asyncpg://audit@127.0.0.1:55439/{DB}",
                "MEDIA_ROOT": str(CURRENT / "media"), "RUN": str(CURRENT)}
    e.RUN = CURRENT
    with (CURRENT / "alembic.log").open("w") as log:
        subprocess.run([PY, "-m", "alembic", "-c", str(SRC / "backend/alembic.ini"), "upgrade", "head"],
                       cwd=SRC, env=CASE_ENV, stdout=log, stderr=subprocess.STDOUT, check=True)
    e.mock("/_ctl/reset")
    e.mock("/_ctl/setup", {"sellers": {"S1": {"balance": 1000000, "net": 0, "bonus": 0}},
                          "tokens": {token: {"seller": "S1", "content": "allowed", "promotion": "allowed"} for token in (TOKEN, TOKEN2)},
                          "ctr": {"ORIG": .02, "P2": .03, "P3": .06, "P4": .025, "UPL": .04, "EXT": .01},
                          "cards": {str(nm): {"seller": "S1", "labels": ["ORIG", "P2", "P3", "P4"]}}})
    BACK = subprocess.Popen([PY, str(H / "run_backend.py")], cwd=CURRENT, env=CASE_ENV,
                            stdout=(CURRENT / "backend.log").open("w"), stderr=subprocess.STDOUT)
    PROCS.append(BACK)
    ready("http://127.0.0.1:18900/health", BACK)
    time.sleep(1)


def user(tag, token=TOKEN):
    u = e.User(tag + "@example.com").register()
    c = u.connect(token, "Synthetic S1")
    return u, c["id"]


def prepared(u, c, nm=92000, views=300):
    t = u.create_test(c, nm, views_per_variant=views, skip_current_photo=True, keep_winner_as_main=False, delete_test_media=False)
    urls = u.card_photo_urls(c, nm)
    assert u.set_source(t["id"], 1, urls[2]).status_code == 200
    r = u.upload(t["id"], 2, e.photo_bytes("UPL"))
    assert r.status_code == 200, r.text
    return t["id"]


def launch(u, tid, **extra):
    t = u.test(tid)
    r = u.start(tid, draft_fingerprint=t["draft_fingerprint"], **extra)
    TRACE.append({"action": "start", "status": r.status_code, "body": r.json()})
    if r.status_code == 409 and isinstance(r.json().get("detail"), dict) and r.json()["detail"].get("code") == "confirmation_outdated":
        # The server intentionally recalculates the protected budget after
        # minimum-bid discovery. Re-read and explicitly confirm that new draft.
        t = u.test(tid)
        r = u.start(tid, draft_fingerprint=t["draft_fingerprint"], **extra)
        TRACE.append({"action": "start_after_budget_reconfirmation", "status": r.status_code, "body": r.json()})
    return r


def snap(label, u, tid):
    data = {"api": u.test(tid), "mock": e.mock_state(), "events": e.mock_events(),
            "db_tests": json.loads(sql("SELECT coalesce(json_agg(t),'[]'::json) FROM ab_tests t")),
            "db_operations": json.loads(sql("SELECT coalesce(json_agg(t),'[]'::json) FROM ab_test_operations t"))}
    write(CURRENT / (label + ".json"), data)
    write(CURRENT / "timeline.json", TRACE)
    return data


def tick(label):
    with (CURRENT / (label + ".log")).open("w") as log:
        subprocess.run([PY, str(H / "tick.py")], cwd=CURRENT, env=CASE_ENV, stdout=log,
                       stderr=subprocess.STDOUT, timeout=1200, check=True)


def control():
    setup("control")
    u, c = user("control")
    tid = prepared(u, c)
    r = launch(u, tid)
    assert r.status_code == 200, r.text
    e.mock("/_ctl/advance", {"views": 300})
    tick("transition")
    s = snap("second_photo", u, tid)
    assert s["api"]["current_variant_order"] == 2, s["api"]
    assert s["mock"]["cards"]["92000"]["slots"][0] == "UPL"
    e.mock("/_ctl/advance", {"views": 300})
    tick("finish")
    s = snap("finished", u, tid)
    assert s["api"]["status"] == "finished", s["api"]
    assert s["mock"]["cards"]["92000"]["slots"] == ["ORIG", "P2", "P3", "P4"]
    assert s["api"]["winner_variant_order"] is None  # aggregate mock supplies no per-photo finality


def stale_confirmation():
    setup("stale_confirmation")
    u, c = user("stale")
    tid = prepared(u, c, views=2000)
    before = u.test(tid)
    urls = u.card_photo_urls(c, 92000)
    assert u.set_source(tid, 3, urls[1]).status_code == 200
    r = u.start(tid, draft_fingerprint=before["draft_fingerprint"])
    assert r.status_code == 409, r.text
    s = snap("rejected", u, tid)
    assert not s["mock"]["campaigns"]


def overlap():
    setup("overlap")
    u, c = user("owner")
    tid = prepared(u, c)
    v, d = user("manager", TOKEN2)
    r = v.post("/ab-tests", {"connection_id": d, "nm_id": 92000, "title": "Concurrent", "views_per_variant": 300, "cpm_rub": 300, "budget_rub": 1200})
    assert r.status_code == 409, r.text
    snap("blocked", u, tid)


def repeat_start():
    setup("repeat_start")
    u, c = user("repeat")
    tid = prepared(u, c)
    e.mock("/_ctl/flags", {"start_fail": True})
    r = launch(u, tid)
    assert r.status_code >= 400
    snap("refused", u, tid)
    e.mock("/_ctl/flags", {"start_fail": False})
    r = u.post(f"/ab-tests/{tid}/reconcile")
    assert r.status_code == 200, r.text
    r = launch(u, tid)
    assert r.status_code == 200, r.text
    s = snap("resumed", u, tid)
    assert len(s["mock"]["campaigns"]) == 1
    assert len([ev for ev in s["events"] if ev["kind"] == "deposit"]) == 1


def pause_failure():
    setup("pause_failure")
    u, c = user("pause")
    tid = prepared(u, c)
    r = launch(u, tid)
    assert r.status_code == 200, r.text
    e.mock("/_ctl/flags", {"pause_ignore": True})
    e.mock("/_ctl/advance", {"views": 300})
    tick("pause_failed")
    tick("safety_sweep")
    s = snap("safe", u, tid)
    assert all(cam["status"] != 9 for cam in s["mock"]["campaigns"].values())


def external_edit():
    setup("external_edit")
    u, c = user("edit")
    tid = prepared(u, c)
    r = launch(u, tid)
    assert r.status_code == 200, r.text
    e.mock("/_ctl/card_edit", {"nm": 92000, "slot": 3, "label": "EXT"})
    r = u.post(f"/ab-tests/{tid}/stop")
    s = snap("conflict", u, tid)
    assert s["mock"]["cards"]["92000"]["slots"][2] == "EXT"
    assert s["api"]["status"] == "failed"
    assert all(cam["status"] != 9 for cam in s["mock"]["campaigns"].values())


def retry_photo():
    setup("retry_photo")
    u, c = user("retry")
    tid = prepared(u, c)
    r = launch(u, tid)
    assert r.status_code == 200, r.text
    e.mock("/_ctl/flags", {"media_mode": "ignore"})
    e.mock("/_ctl/advance", {"views": 300})
    tick("ignored_upload")
    s = snap("waiting", u, tid)
    assert s["api"]["media_status"] == "waiting_image_reupload", s["api"]
    tick("not_due")
    assert u.test(tid)["current_variant_order"] == 1
    e.mock("/_ctl/flags", {"media_mode": "apply"})
    # Only the stored one-hour deadline is advanced; ordinary verification delays remain.
    state = json.loads(sql(f"SELECT media_state FROM ab_tests WHERE id={tid}"))
    state["pending"]["verification"]["next_retry_at"] = time.time() - 1
    encoded = json.dumps(state).replace("'", "''")
    sql(f"UPDATE ab_tests SET media_state='{encoded}'::json WHERE id={tid}")
    tick("retry_due")
    e.mock("/_ctl/advance", {"views": 20})
    tick("after_retry")
    s = snap("recovered", u, tid)
    assert s["api"]["current_variant_order"] == 2, s["api"]
    assert s["mock"]["cards"]["92000"]["slots"][0] == "UPL"
    r = u.post(f"/ab-tests/{tid}/stop")
    assert r.status_code == 200, r.text


if __name__ == "__main__":
    results = {}
    pg_started = False
    try:
        with (OUT / "initdb.log").open("w") as log:
            subprocess.run([str(PG / "initdb"), "-D", str(RUNTIME / "pg"), "-U", "audit", "-A", "trust", "--no-locale", "-E", "UTF8"], stdout=log, stderr=subprocess.STDOUT, check=True)
        subprocess.run([str(PG / "pg_ctl"), "-D", str(RUNTIME / "pg"), "-l", str(RUNTIME / "postgres.log"), "-o", f"-h 127.0.0.1 -p 55439 -k {RUNTIME}", "-w", "start"], check=True)
        pg_started = True
        mock = subprocess.Popen([PY, "-m", "uvicorn", "mock_wb:app", "--host", "127.0.0.1", "--port", "18901"], cwd=H, env=ENV, stdout=(OUT / "mock.log").open("w"), stderr=subprocess.STDOUT)
        PROCS.append(mock)
        ready("http://127.0.0.1:18901/_ctl/state", mock)
        names = os.environ.get("AUDIT_CASES", "control,stale_confirmation,overlap,repeat_start,pause_failure,external_edit,retry_photo").split(",")
        for name in names:
            print("RUN", name, flush=True)
            try:
                globals()[name]()
                results[name] = {"passed": True}
            except Exception as exc:
                results[name] = {"passed": False, "error": repr(exc), "traceback": traceback.format_exc()}
                print(traceback.format_exc(), flush=True)
                if CURRENT != OUT:
                    write(CURRENT / "timeline.json", TRACE)
            write(OUT / "execution.json", results)
    finally:
        for proc in reversed(PROCS):
            stop(proc)
        if pg_started:
            subprocess.run([str(PG / "pg_ctl"), "-D", str(RUNTIME / "pg"), "-m", "fast", "-w", "stop"], check=True)
        write(OUT / "cleanup.json", {"processes": [{"pid": proc.pid, "returncode": proc.poll()} for proc in PROCS], "postgres_stopped": not (RUNTIME / "pg/postmaster.pid").exists()})
        if (RUNTIME / "postgres.log").exists():
            shutil.copy2(RUNTIME / "postgres.log", OUT / "postgres.log")
        print("RESULTS", OUT, json.dumps(results), flush=True)
    sys.exit(0 if results and all(row["passed"] for row in results.values()) else 1)
