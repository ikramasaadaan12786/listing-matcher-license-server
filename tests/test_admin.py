"""Admin panel tests for the Listing Matcher license server.

Run:  python3 tests/test_admin.py

Starts its own servers on scratch DBs (no server needs to be
running first):
  - port 8411, NO ADMIN_PASSWORD -> /admin is 404 (feature off)
  - port 8410, ADMIN_PASSWORD=admin_test_pw -> full admin flow:
    login form, wrong password 401, login ok, dashboard,
    manual license -> activate -> licensed, revoke -> expired,
    CSV export, logout.

Uses only the standard library + the venv python (flask +
stripe installed there). Exits 0 when every check passes.
"""
import http.cookiejar
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN_PORT = 8410
NOPW_PORT = 8411
ADMIN_PW = "admin_test_pw"
PLANS_JSON = json.dumps({"plans": [
    {"id": "3mo", "name": "Test 3 Months", "price_aed": 299,
     "months": 3, "price_id": "price_test_3mo",
     "payment_link": None},
    {"id": "lifetime", "name": "Test Lifetime", "price_aed": 999,
     "months": None, "price_id": "price_test_life",
     "payment_link": None}]})
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
    """HTTP client with a cookie jar (holds the admin session)."""

    def __init__(self, base):
        self.base = base
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj))

    def _req(self, method, path, data=None, headers=None):
        req = urllib.request.Request(self.base + path, data=data,
                                     method=method)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with self.opener.open(req, timeout=10) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()
        except Exception as e:  # noqa: BLE001 - report as failure
            return 0, str(e)

    def get(self, path):
        return self._req("GET", path)

    def get_json(self, path):
        s, text = self._req("GET", path)
        try:
            return s, json.loads(text or "{}")
        except ValueError:
            return s, {"_raw": text}

    def post_json(self, path, body):
        s, text = self._req(
            "POST", path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        try:
            return s, json.loads(text or "{}")
        except ValueError:
            return s, {"_raw": text}

    def post_form(self, path, fields):
        s, text = self._req(
            "POST", path,
            data=urllib.parse.urlencode(fields).encode(),
            headers={"Content-Type":
                     "application/x-www-form-urlencoded"})
        try:
            return s, json.loads(text or "{}")
        except ValueError:
            return s, {"_raw": text}


def start_server(port, data_dir, extra_env):
    shutil.rmtree(data_dir, ignore_errors=True)
    env = dict(os.environ)
    env.update({
        "STRIPE_WEBHOOK_SECRET": "whsec_test_123",
        "STRIPE_PLANS_JSON": PLANS_JSON,
        "DATA_DIR": data_dir,
        "PORT": str(port),
    })
    env.update(extra_env)
    return subprocess.Popen(
        [sys.executable, "app.py"], env=env, cwd=BASE_DIR,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_health(base, timeout=25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/api/health",
                                        timeout=3) as resp:
                if resp.status == 200:
                    return True
        except Exception:  # noqa: BLE001 - still booting
            time.sleep(0.5)
    return False


def main():
    procs = []
    try:
        # --- feature off: no ADMIN_PASSWORD -------------------
        procs.append(start_server(
            NOPW_PORT, "/tmp/lm_server_admin_no_pw", {}))
        if not wait_health("http://127.0.0.1:%d" % NOPW_PORT):
            print("FAIL  no-password server did not boot")
            return 1
        c0 = Client("http://127.0.0.1:%d" % NOPW_PORT)
        s, body = c0.get("/admin")
        check("admin off (no ADMIN_PASSWORD) -> /admin 404",
              s == 404, "%s %s" % (s, body[:120]))
        s, b = c0.post_json("/api/admin/create-license",
                            {"email": "x@y.com", "plan_id": "3mo"})
        check("admin off -> create-license 404",
              s == 404, "%s %s" % (s, b))

        # --- feature on ---------------------------------------
        procs.append(start_server(
            ADMIN_PORT, "/tmp/lm_server_admin_test",
            {"ADMIN_PASSWORD": ADMIN_PW}))
        if not wait_health("http://127.0.0.1:%d" % ADMIN_PORT):
            print("FAIL  admin server did not boot")
            return 1
        c = Client("http://127.0.0.1:%d" % ADMIN_PORT)

        s, body = c.get("/admin")
        check("admin login form shown when not authed",
              s == 200 and "Admin password" in body,
              "%s %s" % (s, body[:120]))
        s, b = c.post_json("/api/admin/create-license",
                           {"email": "x@y.com", "plan_id": "3mo"})
        check("create-license without login -> 401",
              s == 401 and b.get("error") == "unauthorized",
              "%s %s" % (s, b))
        s, body = c.get("/api/admin/export")
        check("export without login -> 401",
              s == 401, "%s %s" % (s, body[:120]))
        s, b = c.post_json("/admin/login",
                           {"password": "wrong-password"})
        check("login wrong password -> 401",
              s == 401 and b.get("error") == "unauthorized",
              "%s %s" % (s, b))
        s, b = c.post_json("/admin/login", {"password": ADMIN_PW})
        check("login correct password -> ok",
              s == 200 and b.get("ok") is True, "%s %s" % (s, b))
        s, body = c.get("/admin")
        check("dashboard after login",
              s == 200 and "Create a manual license" in body
              and "Total accounts" in body, "%s %s" % (s, body[:200]))

        # --- a user signs up ----------------------------------
        s, b = c.post_json("/api/signup",
                           {"email": "test@example.com",
                            "machine_id": "MACHINE-A",
                            "device_name": "Test PC"})
        check("signup -> 5 credits",
              s == 200 and b.get("credits") == 5, "%s %s" % (s, b))
        s, body = c.get("/admin?q=test@example.com")
        check("dashboard search shows the user",
              s == 200 and "test@example.com" in body,
              "%s %s" % (s, body[:200]))

        # --- manual license -----------------------------------
        s, b = c.post_json("/api/admin/create-license",
                           {"email": "test@example.com",
                            "plan_id": "3mo"})
        check("create manual license -> key + expiry",
              s == 200 and b.get("license_key", "").startswith("LM-")
              and b.get("plan") == "3mo" and b.get("expires_at"),
              "%s %s" % (s, b))
        key = b.get("license_key", "")
        s, b = c.post_json("/api/admin/create-license",
                           {"email": "not-an-email",
                            "plan_id": "3mo"})
        check("create license invalid email -> 400",
              s == 400 and b.get("error") == "invalid_request",
              "%s %s" % (s, b))
        s, b = c.post_json("/api/admin/create-license",
                           {"email": "test@example.com",
                            "plan_id": "no-such-plan"})
        check("create license unknown plan -> 400",
              s == 400 and b.get("error") == "invalid_request",
              "%s %s" % (s, b))
        s, body = c.get("/admin")
        check("dashboard lists the manual license key",
              s == 200 and key in body, "%s" % body[:200])
        s, body = c.get("/api/admin/export")
        check("export CSV contains the user",
              s == 200 and "test@example.com" in body
              and body.startswith("email,machine_id"),
              "%s %s" % (s, body[:200]))

        # --- the manual key activates like a Stripe key -------
        s, b = c.post_json("/api/activate",
                           {"email": "test@example.com",
                            "license_key": key,
                            "machine_id": "MACHINE-A"})
        check("manual license activates on machine A",
              s == 200 and b.get("ok") is True
              and b.get("plan") == "3mo", "%s %s" % (s, b))
        s, b = c.get_json("/api/status?email=test@example.com"
                          "&machine_id=MACHINE-A")
        check("status licensed after manual activation",
              s == 200 and b.get("licensed") is True,
              "%s %s" % (s, b))

        # --- revoke -------------------------------------------
        s, b = c.post_json("/api/admin/revoke-license",
                           {"license_key": "LM-XXXX-XXXX-XXXX-XXXX"})
        check("revoke unknown key -> 404",
              s == 404 and b.get("error") == "invalid_key",
              "%s %s" % (s, b))
        s, b = c.post_json("/api/admin/revoke-license",
                           {"license_key": key})
        check("revoke manual license -> ok",
              s == 200 and b.get("ok") is True, "%s %s" % (s, b))
        s, b = c.post_json("/api/activate",
                           {"email": "test@example.com",
                            "license_key": key,
                            "machine_id": "MACHINE-A"})
        check("revoked license activate -> expired",
              s == 403 and b.get("error") == "expired",
              "%s %s" % (s, b))
        s, b = c.get_json("/api/status?email=test@example.com"
                          "&machine_id=MACHINE-A")
        check("status shows license expired after revoke",
              s == 200 and b.get("licensed") is False
              and (b.get("license") or {}).get("status") == "expired",
              "%s %s" % (s, b))

        # --- form login path (HTML form, not JSON) ------------
        c2 = Client("http://127.0.0.1:%d" % ADMIN_PORT)
        s, b = c2.post_form("/admin/login",
                            {"password": "wrong-password"})
        check("form login wrong password -> 401 + login form",
              s == 401 and "Wrong password" in b.get("_raw", ""),
              "%s %s" % (s, str(b)[:120]))
        s, b = c2.post_form("/admin/login", {"password": ADMIN_PW})
        check("form login correct -> dashboard",
              s == 200
              and "Create a manual license" in b.get("_raw", ""),
              "%s %s" % (s, str(b)[:120]))

        # --- logout -------------------------------------------
        s, body = c.get("/admin/logout")
        check("logout redirects to login form",
              s == 200 and "Admin password" in body,
              "%s %s" % (s, body[:120]))
        s, b = c.post_json("/api/admin/create-license",
                           {"email": "x@y.com", "plan_id": "3mo"})
        check("create-license after logout -> 401",
              s == 401 and b.get("error") == "unauthorized",
              "%s %s" % (s, b))
    finally:
        for p in procs:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()

    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
