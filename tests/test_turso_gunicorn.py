"""Gunicorn hang regression test for the Turso HTTP backend.

WHY THIS TEST EXISTS: on 2026-10-29 the native libsql client
(commit f33e92c) deadlocked in production — its Rust/tokio
runtime hung the single gunicorn sync worker on DB-touching
requests ("WORKER TIMEOUT" -> SIGKILL, tokio panic "failed to
join thread: Resource deadlock avoided"), producing HTTP 502
windows while /api/health (no DB) kept answering.

This test reproduces the production shape locally:
  * a fake Turso /v2/pipeline endpoint (tests/fake_turso.py)
    backed by a real sqlite file,
  * the app served by REAL gunicorn — 1 sync worker, DEFAULT
    timeout — with TURSO_DATABASE_URL/TURSO_AUTH_TOKEN set,
then fires DB-touching requests (signup/status/consume POSTs,
admin login POST + dashboard GET) three rounds in a row, each
with a hard 10s client timeout, and asserts:
  * every request completes (fast — nowhere near the worker
    timeout),
  * credits actually move (the DB path really works),
  * the gunicorn log contains no WORKER TIMEOUT / CRITICAL,
  * the worker is still alive and answering at the end.

Run with the venv python (flask + stripe + gunicorn):
  /tmp/lmvenv/bin/python tests/test_turso_gunicorn.py
Exits 0 when every check passes.
"""
import http.cookiejar
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE_PORT = 8431
APP_PORT = 8432
BASE = "http://127.0.0.1:%d" % APP_PORT
ADMIN_PW = "hang_test_pw"
EMAIL = "hang-test@example.com"
MACHINE = "HANG-TEST-MACHINE"
CLIENT_TIMEOUT = 10
SLOW = 5.0  # any single request slower than this is a failure

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS  %s" % name)
    else:
        FAIL += 1
        print("FAIL  %s  %s" % (name, extra))


class Client:
    def __init__(self):
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(
                http.cookiejar.CookieJar()))

    def call(self, method, path, body=None):
        data = (json.dumps(body).encode() if body is not None
                else None)
        req = urllib.request.Request(BASE + path, data=data,
                                     method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        t0 = time.time()
        try:
            with self.opener.open(req, timeout=CLIENT_TIMEOUT) as r:
                raw = r.read().decode()
                status = r.status
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            status = e.code
        elapsed = time.time() - t0
        try:
            parsed = json.loads(raw) if raw else {}
        except ValueError:
            parsed = {}
        return status, parsed, raw, elapsed


def wait_healthy(proc, seconds=40):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(
                    BASE + "/api/health",
                    timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def main():
    tmp = tempfile.mkdtemp(prefix="lm_hang_test_")
    fake_db = os.path.join(tmp, "fake_turso.db")
    gun_log_path = os.path.join(tmp, "gunicorn.log")
    env = dict(os.environ)
    env.update({
        "TURSO_DATABASE_URL": "http://127.0.0.1:%d" % FAKE_PORT,
        "TURSO_AUTH_TOKEN": "hang-test-token",
        "ADMIN_PASSWORD": ADMIN_PW,
        "DATA_DIR": tmp,
        "PYTHONUNBUFFERED": "1",
    })
    env.pop("STRIPE_PLANS_JSON", None)

    fake = subprocess.Popen(
        [sys.executable,
         os.path.join(BASE_DIR, "tests", "fake_turso.py"),
         str(FAKE_PORT), fake_db],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    gun_log = open(gun_log_path, "wb")
    gun = subprocess.Popen(
        [sys.executable, "-m", "gunicorn", "app:app",
         "--bind", "127.0.0.1:%d" % APP_PORT,
         "--workers", "1"],
        cwd=BASE_DIR, env=env, stdout=gun_log,
        stderr=subprocess.STDOUT)
    try:
        check("gunicorn booted + healthy (Turso env set)",
              wait_healthy(gun))
        c = Client()
        s, b, _raw, el = c.call("GET", "/api/plans")
        check("plans served (3 real plans)",
              s == 200 and len(b.get("plans", [])) == 3,
              "status=%s %s" % (s, b))
        check("plans request fast (%.2fs)" % el, el < SLOW)

        expected_credits = 5
        for rnd in (1, 2, 3):
            s, b, _raw, el = c.call(
                "POST", "/api/signup",
                {"email": EMAIL, "machine_id": MACHINE})
            check("round %d signup -> 200, credits %d (%.2fs)"
                  % (rnd, expected_credits, el),
                  s == 200 and b.get("credits") == expected_credits,
                  "status=%s %s" % (s, b))
            s, b, _raw, el = c.call(
                "POST", "/api/consume",
                {"email": EMAIL, "machine_id": MACHINE})
            expected_credits -= 1
            check("round %d consume -> credits_left %d (%.2fs)"
                  % (rnd, expected_credits, el),
                  s == 200 and b.get("credits_left") == expected_credits,
                  "status=%s %s" % (s, b))
            s, b, _raw, el = c.call(
                "GET", "/api/status?email=%s&machine_id=%s"
                % (EMAIL, MACHINE))
            check("round %d status -> credits %d (%.2fs)"
                  % (rnd, expected_credits, el),
                  s == 200 and b.get("credits") == expected_credits,
                  "status=%s %s" % (s, b))
            s, b, _raw, el = c.call(
                "POST", "/admin/login", {"password": ADMIN_PW})
            check("round %d admin login POST -> ok (%.2fs)"
                  % (rnd, el),
                  s == 200 and b.get("ok") is True,
                  "status=%s %s" % (s, b))
            s, _b, raw, el = c.call("GET", "/admin")
            check("round %d admin dashboard (DB reads) -> 200 "
                  "(%.2fs)" % (rnd, el),
                  s == 200 and "Total accounts" in raw,
                  "status=%s" % s)

        s, b, _raw, el = c.call("GET", "/api/health")
        check("worker still alive + healthy at the end",
              s == 200 and b.get("ok") is True and gun.poll() is None)
    finally:
        gun.terminate()
        try:
            gun.wait(timeout=10)
        except subprocess.TimeoutExpired:
            gun.kill()
        gun_log.close()
        fake.terminate()
        try:
            fake.wait(timeout=5)
        except subprocess.TimeoutExpired:
            fake.kill()

    with open(gun_log_path, errors="replace") as f:
        log = f.read()
    check("no WORKER TIMEOUT in gunicorn log",
          "WORKER TIMEOUT" not in log)
    check("no CRITICAL in gunicorn log", "CRITICAL" not in log)

    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
