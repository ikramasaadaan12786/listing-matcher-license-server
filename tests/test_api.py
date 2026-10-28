"""Local API tests for the Listing Matcher license server.

Run:  STRIPE_WEBHOOK_SECRET=whsec_test STRIPE_PLANS_JSON='{"plans": \
  [{"id":"3mo","name":"Test 3 Months","price_aed":299,"months":3, \
  "price_id":"price_test_3mo","payment_link":null}]}' \
  DATA_DIR=/tmp/lm_server_test PORT=8399 python3 tests/test_api.py

Starts the app on a scratch DB, exercises every endpoint, and
prints PASS/FAIL per check. The webhook test builds a real
Stripe-style signed payload (HMAC-SHA256, the same scheme
Stripe uses) so signature verification is genuinely exercised —
no live Stripe account needed. The restore-from-Stripe path
cannot be tested without real Stripe credentials; that is noted
in BUILD_REPORT.md.
"""
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8399"
WHSEC = os.environ.get("STRIPE_WEBHOOK_SECRET", "whsec_test")
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


def call(method, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except Exception as e:  # noqa: BLE001 - report as failure
        return 0, {"_exc": str(e)}


def get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=10) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:  # noqa: BLE001
        return 0, str(e)


def stripe_sig(payload, secret):
    ts = int(time.time())
    sig = hmac.new(secret.encode(), ("%d.%s" % (ts, payload)).encode(),
                   hashlib.sha256).hexdigest()
    return "t=%d,v1=%s" % (ts, sig)


def main():
    # --- public endpoints -------------------------------------
    s, b = call("GET", "/api/health")
    check("health ok", s == 200 and b.get("ok") is True, str(b))
    s, b = call("GET", "/api/version")
    check("version 1.1.0", s == 200 and b.get("version") == "1.1.0",
          str(b))
    s, b = call("GET", "/api/plans")
    # NOTE: the test server runs with STRIPE_PLANS_JSON env set to
    # a single test plan, so /api/plans returns exactly that.
    check("plans served (env override: 1 test plan)",
          s == 200 and len(b.get("plans", [])) == 1
          and b["plans"][0]["price_id"] == "price_test_3mo", str(b))
    plans_file = json.load(open(
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "..", "stripe_plans.json")))
    check("repo stripe_plans.json has the 3 real plans "
          "(299/499/999 with real price_ids)",
          [p["price_aed"] for p in plans_file.get("plans", [])]
          == [299, 499, 999]
          and all(p.get("price_id", "").startswith("price_")
                  for p in plans_file["plans"])
          and all(p.get("payment_link", "").startswith(
              "https://buy.stripe.com/")
              for p in plans_file["plans"]), str(plans_file))
    s, b = call("GET", "/api/app-version")
    check("app-version 0.0.0 (no builds yet)",
          s == 200 and b.get("version") == "0.0.0"
          and b.get("windows_url") is None, str(b))

    # --- signup ----------------------------------------------
    s, b = call("POST", "/api/signup",
                {"email": "test@example.com", "machine_id": "MACHINE-A",
                 "device_name": "Test PC"})
    check("signup new -> 5 credits",
          s == 200 and b.get("credits") == 5, str(b))
    s, b = call("POST", "/api/signup",
                {"email": "test@example.com", "machine_id": "MACHINE-A"})
    check("signup again same machine -> still 5, no new trial",
          s == 200 and b.get("credits") == 5, str(b))
    s, b = call("POST", "/api/signup",
                {"email": "other@example.com", "machine_id": "MACHINE-A"})
    check("same machine, new email -> NO second trial",
          s == 200 and b.get("credits") == 5
          and b.get("email") == "test@example.com", str(b))
    s, b = call("POST", "/api/signup",
                {"email": "test@example.com", "machine_id": "MACHINE-B"})
    check("same email, new machine -> same account, no new trial",
          s == 200 and b.get("credits") == 5, str(b))
    s, b = call("POST", "/api/signup",
                {"email": "not-an-email", "machine_id": "MACHINE-C"})
    check("signup invalid email -> 400",
          s == 400 and b.get("error") == "invalid_email", str(b))

    # --- consume x5 then blocked ------------------------------
    for i in range(5):
        s, b = call("POST", "/api/consume",
                    {"email": "test@example.com",
                     "machine_id": "MACHINE-A"})
        check("consume #%d -> credits_left %d" % (i + 1, 4 - i),
              s == 200 and b.get("ok") is True
              and b.get("credits_left") == 4 - i, str(b))
    s, b = call("POST", "/api/consume",
                {"email": "test@example.com", "machine_id": "MACHINE-A"})
    check("consume #6 -> blocked no_credits",
          s == 200 and b.get("ok") is False
          and b.get("error") == "no_credits", str(b))
    s, b = call("GET", "/api/status?email=test@example.com"
                      "&machine_id=MACHINE-A")
    check("status: 0 credits, not licensed",
          s == 200 and b.get("credits") == 0
          and b.get("licensed") is False, str(b))

    # --- webhook: bad signature rejected ----------------------
    event = {"id": "evt_test_1", "type": "checkout.session.completed",
             "data": {"object": {
                 "id": "cs_test_1",
                 "customer_details": {"email": "test@example.com"},
                 "metadata": {"price_id": "price_test_3mo"}}}}
    payload = json.dumps(event)
    s, b = call("POST", "/api/stripe-webhook", event,
                {"Stripe-Signature": "t=1,v1=deadbeef"})
    check("webhook bad signature -> 400",
          s == 400 and b.get("error") == "invalid signature", str(b))

    # --- webhook: valid signature, unknown price ---------------
    event2 = {"id": "evt_test_2", "type": "checkout.session.completed",
              "data": {"object": {
                  "id": "cs_test_unknown",
                  "customer_details": {"email": "test@example.com"},
                  "metadata": {"price_id": "price_does_not_exist"}}}}
    payload2 = json.dumps(event2)
    s, b = call("POST", "/api/stripe-webhook", event2,
                {"Stripe-Signature": stripe_sig(payload2, WHSEC)})
    check("webhook unknown price -> 200, no license, no crash",
          s == 200 and b.get("error") == "unknown_price", str(b))

    # --- webhook: valid signature, known price -----------------
    s, b = call("POST", "/api/stripe-webhook", event,
                {"Stripe-Signature": stripe_sig(payload, WHSEC)})
    check("webhook valid -> license created",
          s == 200 and b.get("license_key", "").startswith("LM-"),
          str(b))
    key = b.get("license_key", "")

    # idempotent replay
    s, b = call("POST", "/api/stripe-webhook", event,
                {"Stripe-Signature": stripe_sig(payload, WHSEC)})
    check("webhook replay -> duplicate, no second license",
          s == 200 and b.get("duplicate") is True, str(b))
    s, b = call("GET", "/api/status?email=test@example.com"
                      "&machine_id=MACHINE-A")
    check("status shows license not_activated",
          s == 200 and (b.get("license") or {}).get("status")
          == "not_activated", str(b))

    # --- success page ------------------------------------------
    s, body = get("/success?session_id=cs_test_1")
    check("success page shows the key",
          s == 200 and bool(key) and key in body, body[:200])

    # --- activate ----------------------------------------------
    s, b = call("POST", "/api/activate",
                {"email": "test@example.com", "license_key": key,
                 "machine_id": "MACHINE-A"})
    check("activate on machine A -> ok, plan 3mo, expiry set",
          s == 200 and b.get("ok") is True and b.get("plan") == "3mo"
          and b.get("expires_at"), str(b))
    s, b = call("POST", "/api/activate",
                {"email": "test@example.com", "license_key": key,
                 "machine_id": "MACHINE-A"})
    check("re-activate same machine -> ok",
          s == 200 and b.get("ok") is True, str(b))
    s, b = call("POST", "/api/activate",
                {"email": "test@example.com", "license_key": key,
                 "machine_id": "MACHINE-B"})
    check("same key on machine B -> REJECTED already_bound_other_device",
          s == 403 and b.get("error") == "already_bound_other_device",
          str(b))
    s, b = call("POST", "/api/activate",
                {"email": "test@example.com",
                 "license_key": "LM-XXXX-XXXX-XXXX-XXXX",
                 "machine_id": "MACHINE-A"})
    check("activate invalid key -> invalid_key",
          s == 404 and b.get("error") == "invalid_key", str(b))
    # expired license (inserted directly into the test DB)
    import sqlite3
    _db = sqlite3.connect("/tmp/lm_server_test/licenses.db")
    _db.execute(
        "INSERT INTO licenses (license_key, email, plan_id, "
        "plan_name, months, expires_at, stripe_session_id, "
        "bound_machine_id, created_at) VALUES "
        "('LM-EXPI-RED0-0000', 'test@example.com', '3mo', "
        "'Test 3 Months', 3, '2020-01-01T00:00:00Z', "
        "'cs_test_expired', NULL, '2020-01-01T00:00:00Z')")
    _db.commit()
    _db.close()
    s, b = call("POST", "/api/activate",
                {"email": "test@example.com",
                 "license_key": "LM-EXPI-RED0-0000",
                 "machine_id": "MACHINE-A"})
    check("activate expired license -> expired",
          s == 403 and b.get("error") == "expired", str(b))
    s, b = call("POST", "/api/activate",
                {"email": "test@example.com", "machine_id": "MACHINE-A"})
    check("activate by email only -> ok (latest license)",
          s == 200 and b.get("ok") is True, str(b))

    # --- consume while licensed --------------------------------
    s, b = call("POST", "/api/consume",
                {"email": "test@example.com", "machine_id": "MACHINE-A"})
    check("consume while licensed -> ok, licensed, NO deduction",
          s == 200 and b.get("ok") is True
          and b.get("licensed") is True, str(b))
    s, b = call("GET", "/api/status?email=test@example.com"
                      "&machine_id=MACHINE-A")
    check("status: licensed true, credits still 0",
          s == 200 and b.get("licensed") is True
          and b.get("credits") == 0, str(b))
    s, b = call("GET", "/api/status?email=test@example.com"
                      "&machine_id=MACHINE-B")
    check("status on machine B: licensed false (bound elsewhere)",
          s == 200 and b.get("licensed") is False, str(b))

    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
