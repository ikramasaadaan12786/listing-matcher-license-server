# Listing Matcher — License Server (client product)
#
# Flask app that serves the desktop (Windows/macOS) and Android
# clients: signup + 5 trial credits, license activation
# (machine-bound, one device at a time), Stripe webhook ->
# auto-generated licenses, and the app-version endpoint for
# client auto-update.
#
# Data model (SQLite, file next to app.py unless DATA_DIR is set):
#   accounts: one row per (email, machine_id) signup. Trial
#     credits live here only — they are NOT restored from Stripe.
#   licenses: one row per paid purchase. Keyed by license_key,
#     idempotent on stripe_session_id. bound_machine_id is NULL
#     until first activation.
#   webhook_events: processed Stripe session ids (idempotency).
#
# Resilience (SPEC §5): Render free disks are EPHEMERAL — a
# redeploy/restart can wipe this database. Paid licenses survive
# because Stripe is the source of truth: when a status/activate
# call finds no license for an email, the server re-creates it
# from Stripe (needs STRIPE_SECRET_KEY). Trial credits do NOT
# survive a wipe — that is by design (they are a free trial).
#
# Secrets: STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET — env vars
# only, never in code. No passwords are stored (signup is email
# only). No real owner data anywhere in this repo.

import json
import os
import re
import secrets
import sqlite3
import string
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, request, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ------------------------------------------------------------------
# version
# ------------------------------------------------------------------

def _read_version():
    try:
        with open(os.path.join(BASE_DIR, "VERSION")) as f:
            return f.read().strip() or "1.0.0"
    except OSError:
        return "1.0.0"


VERSION = _read_version()

# ------------------------------------------------------------------
# config: Stripe keys (env only), plans file, database path
# ------------------------------------------------------------------

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()

DATA_DIR = os.environ.get("DATA_DIR", "").strip()
if DATA_DIR:
    os.makedirs(DATA_DIR, exist_ok=True)
    DB_PATH = os.path.join(DATA_DIR, "licenses.db")
else:
    DB_PATH = os.path.join(BASE_DIR, "licenses.db")

RELEASES_DIR = os.path.join(BASE_DIR, "releases")

TRIAL_CREDITS = 5

# Built-in plan catalogue. The owner fills in price_id +
# payment_link after creating the Stripe products (see
# stripe_plans.json). A plan with price_id == None cannot be
# bought yet — the webhook simply cannot map it.
DEFAULT_PLANS = {
    "plans": [
        {"id": "3mo", "name": "Listing Matcher — 3 Months",
         "price_aed": 299, "months": 3,
         "price_id": None, "payment_link": None},
        {"id": "6mo", "name": "Listing Matcher — 6 Months",
         "price_aed": 499, "months": 6,
         "price_id": None, "payment_link": None},
        {"id": "lifetime", "name": "Listing Matcher — Lifetime",
         "price_aed": 999, "months": None,
         "price_id": None, "payment_link": None},
    ]
}


def load_plans():
    """Plans from STRIPE_PLANS_JSON env (a JSON string) first,
    then stripe_plans.json next to app.py, then the built-in
    defaults. NEVER crashes if the file is absent — returns the
    defaults and logs the fact."""
    raw = os.environ.get("STRIPE_PLANS_JSON", "").strip()
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("plans"):
                return data
        except (ValueError, TypeError) as exc:
            print("WARN: STRIPE_PLANS_JSON is not valid JSON: %s" % exc)
    path = os.path.join(BASE_DIR, "stripe_plans.json")
    if os.path.exists(path):
        try:
            with open(path) as f:
                data = json.load(f)
            if isinstance(data, dict) and data.get("plans"):
                return data
        except (ValueError, OSError) as exc:
            print("WARN: stripe_plans.json unreadable: %s" % exc)
    print("INFO: no Stripe plans configured yet — serving defaults "
          "(price_id/payment_link empty until the owner creates "
          "the products).")
    return DEFAULT_PLANS


def plan_by_price_id(price_id):
    for plan in load_plans().get("plans", []):
        if plan.get("price_id") and plan["price_id"] == price_id:
            return plan
    return None


def plan_by_id(plan_id):
    for plan in load_plans().get("plans", []):
        if plan.get("id") == plan_id:
            return plan
    return None


# ------------------------------------------------------------------
# database
# ------------------------------------------------------------------

def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            machine_id TEXT NOT NULL,
            device_name TEXT,
            credits INTEGER NOT NULL DEFAULT 5,
            trial_granted INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(email, machine_id)
        );
        CREATE TABLE IF NOT EXISTS licenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            license_key TEXT NOT NULL UNIQUE,
            email TEXT NOT NULL,
            plan_id TEXT NOT NULL,
            plan_name TEXT,
            months INTEGER,
            expires_at TEXT,
            stripe_session_id TEXT UNIQUE,
            bound_machine_id TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS webhook_events (
            stripe_session_id TEXT PRIMARY KEY,
            processed_at TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# ------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _norm_email(email):
    return (email or "").strip().lower()


def _norm_machine(machine_id):
    return (machine_id or "").strip()


_KEY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I


def generate_license_key():
    parts = ["".join(secrets.choice(_KEY_ALPHABET) for _ in range(4))
             for _ in range(4)]
    return "LM-" + "-".join(parts)


def _norm_key(key):
    return re.sub(r"[^A-Z0-9]", "", (key or "").upper())


def license_is_valid(row):
    """A license row is valid when it has not expired. Lifetime
    rows have expires_at NULL and never expire."""
    if row is None:
        return False
    exp = _parse_iso(row["expires_at"])
    if exp is None:
        return True
    return exp > _now()


def _license_public(row):
    return {
        "plan": row["plan_id"],
        "plan_name": row["plan_name"],
        "expires_at": row["expires_at"],
        "status": "active" if license_is_valid(row) else "expired",
    }


def find_account(conn, email, machine_id):
    return conn.execute(
        "SELECT * FROM accounts WHERE email = ? AND machine_id = ?",
        (email, machine_id)).fetchone()


def find_license_for_email(conn, email):
    """Most recent valid license for an email, else the most
    recent license of any status (for display)."""
    rows = conn.execute(
        "SELECT * FROM licenses WHERE email = ? "
        "ORDER BY created_at DESC", (email,)).fetchall()
    for row in rows:
        if license_is_valid(row):
            return row
    return rows[0] if rows else None


def find_license_bound_to_machine(conn, email, machine_id):
    row = find_license_for_email(conn, email)
    if row and license_is_valid(row) and row["bound_machine_id"] == machine_id:
        return row
    return None


# ------------------------------------------------------------------
# Stripe restore path (Stripe is the source of truth for PAID
# licenses). Called when the local DB has no license for an email.
# ------------------------------------------------------------------

def restore_license_from_stripe(email):
    """Look up paid Checkout Sessions for this email in Stripe
    and re-create any missing license locally. Returns the
    license row or None. No-op (with a log line) when no
    STRIPE_SECRET_KEY is configured. Trial credits are NOT
    restored — they live only in the server DB."""
    if not STRIPE_SECRET_KEY:
        print("INFO: restore skipped for %s — no STRIPE_SECRET_KEY."
              % email)
        return None
    try:
        import stripe
    except ImportError:
        print("WARN: stripe package missing — cannot restore.")
        return None
    stripe.api_key = STRIPE_SECRET_KEY
    try:
        sessions = []
        # Path 1: find the Stripe Customer by email, then their
        # Checkout Sessions.
        customers = stripe.Customer.list(email=email, limit=5)
        for customer in customers.data:
            for s in stripe.checkout.Session.list(
                    customer=customer.id, limit=20).data:
                sessions.append(s.to_dict())
        # Path 2: recent sessions whose customer_details email
        # matches (covers Payment Links, which may not attach a
        # pre-existing Customer the same way).
        if not sessions:
            for s in stripe.checkout.Session.list(limit=100).data:
                s = s.to_dict()
                details = s.get("customer_details") or {}
                if (details.get("email") or "").lower() == email:
                    sessions.append(s)
    except Exception as exc:  # Stripe error: log, do not crash
        print("WARN: Stripe restore lookup failed for %s: %s"
              % (email, exc))
        return None

    restored = None
    conn = db()
    try:
        for session in sessions:
            if session.get("payment_status") != "paid":
                continue
            session_id = session.get("id")
            if conn.execute(
                    "SELECT 1 FROM licenses WHERE stripe_session_id = ?",
                    (session_id,)).fetchone():
                continue
            try:
                items = stripe.checkout.Session.list_line_items(
                    session_id, limit=10)
            except Exception as exc:
                print("WARN: line items failed for %s: %s"
                      % (session_id, exc))
                continue
            for item in items.data:
                item = item.to_dict()
                price = (item.get("price") or {})
                price_id = price.get("id")
                plan = plan_by_price_id(price_id) if price_id else None
                if not plan:
                    continue
                lic = create_license(
                    conn, email, plan, stripe_session_id=session_id)
                print("INFO: restored license %s (%s) for %s from "
                      "Stripe session %s"
                      % (lic["license_key"], plan["id"], email,
                         session_id))
                restored = lic
                break
        conn.commit()
    finally:
        conn.close()
    return restored


def create_license(conn, email, plan, stripe_session_id=None):
    """Insert a license row (idempotent on stripe_session_id when
    given). Returns the row dict."""
    if stripe_session_id:
        existing = conn.execute(
            "SELECT * FROM licenses WHERE stripe_session_id = ?",
            (stripe_session_id,)).fetchone()
        if existing:
            return dict(existing)
    months = plan.get("months")
    expires_at = None
    if months:
        expires_at = _iso(_now() + timedelta(days=30 * int(months)))
    key = generate_license_key()
    now = _iso(_now())
    conn.execute(
        "INSERT INTO licenses (license_key, email, plan_id, "
        "plan_name, months, expires_at, stripe_session_id, "
        "bound_machine_id, created_at) VALUES (?, ?, ?, ?, ?, ?, "
        "?, NULL, ?)",
        (key, email, plan["id"], plan.get("name"), months,
         expires_at, stripe_session_id, now))
    conn.commit()
    row = conn.execute(
        "SELECT * FROM licenses WHERE license_key = ?",
        (key,)).fetchone()
    return dict(row)


# ------------------------------------------------------------------
# app
# ------------------------------------------------------------------

app = Flask(__name__)


@app.route("/")
def index():
    return """<!doctype html><html><head><meta charset="utf-8">
<title>Listing Matcher — License Server</title></head><body>
<h1>Listing Matcher — License Server</h1>
<p>Signup, trial credits, license activation and Stripe webhooks
for the Listing Matcher desktop / Android apps.</p>
<ul>
<li><a href="/api/plans">GET /api/plans</a> — subscription plans</li>
<li><a href="/api/version">GET /api/version</a> — server version</li>
<li><a href="/api/app-version">GET /api/app-version</a> — app releases (auto-update)</li>
<li><a href="/api/health">GET /api/health</a></li>
</ul>
<p>Clients: Windows, macOS, Android. No data is stored here
except accounts, trial credits and licenses.</p>
</body></html>"""


@app.route("/api/health")
def health():
    return jsonify({"ok": True, "module": "license-server",
                    "version": VERSION})


@app.route("/api/version")
def version():
    return jsonify({"version": VERSION,
                    "module": "listing-matcher-license-server"})


@app.route("/api/plans")
def plans():
    return jsonify(load_plans())


@app.route("/api/app-version")
def app_version():
    """Current app releases for client auto-update. Reads
    releases/app-version.json:
      {"version": "1.0.0", "windows_url": "...", "mac_url": "...",
       "mac_arm64_url": "...", "android_url": "..."}
    URLs may be absolute, or relative /releases/<file> paths
    served by this server. When no file exists yet (no builds
    shipped), returns version 0.0.0 with null URLs — the apps
    treat that as 'no update available'."""
    path = os.path.join(RELEASES_DIR, "app-version.json")
    if os.path.exists(path):
        try:
            with open(path) as f:
                data = json.load(f)
            if isinstance(data, dict) and data.get("version"):
                return jsonify(data)
        except (ValueError, OSError) as exc:
            print("WARN: app-version.json unreadable: %s" % exc)
    return jsonify({"version": "0.0.0", "windows_url": None,
                    "mac_url": None, "mac_arm64_url": None,
                    "android_url": None})


@app.route("/releases/<path:filename>")
def release_file(filename):
    """Serve a release file from releases/ (installers uploaded
    by the owner/build agent). Guards against path traversal."""
    safe = os.path.basename(filename)
    full = os.path.join(RELEASES_DIR, safe)
    if not os.path.isfile(full):
        return jsonify({"error": "not found"}), 404
    return send_from_directory(RELEASES_DIR, safe, as_attachment=True)


# ------------------------------------------------------------------
# signup / status / consume / activate
# ------------------------------------------------------------------

@app.route("/api/signup", methods=["POST"])
def signup():
    data = request.get_json(silent=True) or {}
    email = _norm_email(data.get("email"))
    machine_id = _norm_machine(data.get("machine_id"))
    device_name = (data.get("device_name") or "").strip() or None
    if not EMAIL_RE.match(email):
        return jsonify({"ok": False,
                        "error": "invalid_email"}), 400
    if not machine_id:
        return jsonify({"ok": False,
                        "error": "machine_id_required"}), 400

    conn = db()
    try:
        # One trial per machine: a machine that already used its
        # trial never gets 5 again, even with a new email.
        by_machine = conn.execute(
            "SELECT * FROM accounts WHERE machine_id = ? "
            "ORDER BY created_at ASC LIMIT 1",
            (machine_id,)).fetchone()
        if by_machine:
            return jsonify({
                "account_id": by_machine["id"],
                "email": by_machine["email"],
                "credits": by_machine["credits"],
                "trial_used": True})
        existing = conn.execute(
            "SELECT * FROM accounts WHERE email = ? "
            "ORDER BY created_at ASC LIMIT 1",
            (email,)).fetchone()
        if existing:
            # Same email, new machine: the account (and its
            # credits) travel with the email, no new trial.
            return jsonify({
                "account_id": existing["id"],
                "email": existing["email"],
                "credits": existing["credits"],
                "trial_used": True})
        now = _iso(_now())
        cur = conn.execute(
            "INSERT INTO accounts (email, machine_id, device_name, "
            "credits, trial_granted, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 1, ?, ?)",
            (email, machine_id, device_name, TRIAL_CREDITS, now, now))
        conn.commit()
        return jsonify({"account_id": cur.lastrowid, "email": email,
                        "credits": TRIAL_CREDITS,
                        "trial_used": False})
    finally:
        conn.close()


@app.route("/api/status")
def status():
    email = _norm_email(request.args.get("email"))
    machine_id = _norm_machine(request.args.get("machine_id"))
    if not EMAIL_RE.match(email):
        return jsonify({"ok": False,
                        "error": "invalid_email"}), 400
    conn = db()
    try:
        account = conn.execute(
            "SELECT * FROM accounts WHERE email = ? "
            "ORDER BY created_at ASC LIMIT 1", (email,)).fetchone()
        credits = account["credits"] if account else 0
        lic = find_license_for_email(conn, email)
        if lic is None and STRIPE_SECRET_KEY:
            # Resilience: the DB may have been wiped (Render
            # free is ephemeral) — re-create from Stripe.
            conn.close()
            restored = restore_license_from_stripe(email)
            conn = db()
            lic = restored or find_license_for_email(conn, email)
        licensed = False
        lic_out = None
        if lic:
            lic_out = _license_public(lic)
            if (license_is_valid(lic)
                    and lic["bound_machine_id"]
                    and machine_id
                    and lic["bound_machine_id"] == machine_id):
                licensed = True
            elif (license_is_valid(lic)
                    and lic["bound_machine_id"]
                    and machine_id
                    and lic["bound_machine_id"] != machine_id):
                lic_out["status"] = "bound_other_device"
            elif license_is_valid(lic) and not lic["bound_machine_id"]:
                lic_out["status"] = "not_activated"
        return jsonify({"credits": credits, "license": lic_out,
                        "licensed": licensed})
    finally:
        conn.close()


@app.route("/api/consume", methods=["POST"])
def consume():
    """Deduct 1 trial credit. The app calls this ONLY for a
    chargeable match (score >= 40 with size/price evidence) —
    never for a zero result. Licensed users are never charged."""
    data = request.get_json(silent=True) or {}
    email = _norm_email(data.get("email"))
    machine_id = _norm_machine(data.get("machine_id"))
    if not EMAIL_RE.match(email) or not machine_id:
        return jsonify({"ok": False,
                        "error": "invalid_request"}), 400
    conn = db()
    try:
        # Licensed on THIS machine -> no deduction, ever.
        if find_license_bound_to_machine(conn, email, machine_id):
            return jsonify({"ok": True, "licensed": True,
                            "credits_left": None})
        account = conn.execute(
            "SELECT * FROM accounts WHERE email = ? "
            "ORDER BY created_at ASC LIMIT 1", (email,)).fetchone()
        if account is None:
            return jsonify({"ok": False,
                            "error": "no_account"}), 404
        if account["credits"] <= 0:
            return jsonify({"ok": False, "error": "no_credits",
                            "licensed": False, "credits_left": 0})
        conn.execute(
            "UPDATE accounts SET credits = credits - 1, "
            "updated_at = ? WHERE id = ?",
            (_iso(_now()), account["id"]))
        conn.commit()
        left = account["credits"] - 1
        return jsonify({"ok": True, "licensed": False,
                        "credits_left": left})
    finally:
        conn.close()


@app.route("/api/activate", methods=["POST"])
def activate():
    """Bind a license key to one machine. First activation binds;
    the same machine may re-activate; a different machine is
    rejected. license_key may be empty when the client activates
    by email only ('I already paid') — then the latest valid
    license for that email is used."""
    data = request.get_json(silent=True) or {}
    email = _norm_email(data.get("email"))
    machine_id = _norm_machine(data.get("machine_id"))
    key = _norm_key(data.get("license_key"))
    if not EMAIL_RE.match(email) or not machine_id:
        return jsonify({"ok": False,
                        "error": "invalid_request"}), 400
    conn = db()
    try:
        lic = None
        if key:
            row = conn.execute(
                "SELECT * FROM licenses WHERE "
                "REPLACE(REPLACE(UPPER(license_key), '-', ''), ' ', '') = ?",
                (key,)).fetchone()
            if row is None:
                return jsonify({"ok": False,
                                "error": "invalid_key"}), 404
            if row["email"] != email:
                return jsonify({"ok": False,
                                "error": "invalid_key"}), 404
            lic = row
        else:
            lic = find_license_for_email(conn, email)
            if lic is None and STRIPE_SECRET_KEY:
                conn.close()
                restored = restore_license_from_stripe(email)
                conn = db()
                lic = restored or find_license_for_email(conn, email)
            if lic is None:
                return jsonify({"ok": False,
                                "error": "invalid_key"}), 404
        if not license_is_valid(lic):
            return jsonify({"ok": False, "error": "expired"}), 403
        bound = lic["bound_machine_id"]
        if bound and bound != machine_id:
            return jsonify(
                {"ok": False,
                 "error": "already_bound_other_device"}), 403
        if not bound:
            conn.execute(
                "UPDATE licenses SET bound_machine_id = ? "
                "WHERE id = ?", (machine_id, lic["id"]))
            conn.commit()
        return jsonify({"ok": True, "plan": lic["plan_id"],
                        "plan_name": lic["plan_name"],
                        "expires_at": lic["expires_at"]})
    finally:
        conn.close()


# ------------------------------------------------------------------
# Stripe webhook + success page
# ------------------------------------------------------------------

@app.route("/api/stripe-webhook", methods=["POST"])
def stripe_webhook():
    if not STRIPE_WEBHOOK_SECRET:
        print("WARN: webhook received but STRIPE_WEBHOOK_SECRET "
              "is not configured.")
        return jsonify({"error": "webhook not configured"}), 503
    try:
        import stripe
    except ImportError:
        return jsonify({"error": "stripe package missing"}), 500
    payload = request.get_data(as_text=True)
    sig = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(
            payload, sig, STRIPE_WEBHOOK_SECRET).to_dict()
    except ValueError:
        return jsonify({"error": "invalid payload"}), 400
    except Exception:
        return jsonify({"error": "invalid signature"}), 400

    if event.get("type") != "checkout.session.completed":
        return jsonify({"received": True, "ignored": event.get("type")})

    session = event["data"]["object"]
    session_id = session.get("id")
    details = session.get("customer_details") or {}
    email = _norm_email(details.get("email")
                        or session.get("customer_email"))
    if not email or not EMAIL_RE.match(email):
        print("WARN: webhook session %s has no usable email; "
              "no license created." % session_id)
        return jsonify({"received": True, "error": "no_email"})

    conn = db()
    try:
        if conn.execute(
                "SELECT 1 FROM webhook_events "
                "WHERE stripe_session_id = ?",
                (session_id,)).fetchone():
            print("INFO: webhook session %s already processed "
                  "(idempotent replay)." % session_id)
            return jsonify({"received": True, "duplicate": True})
        # Find the Price ID for this purchase. First the session's
        # own metadata (a manual override the owner can set on a
        # Payment Link), then the session's line items from Stripe.
        price_id = (session.get("metadata") or {}).get("price_id")
        if not price_id:
            try:
                stripe.api_key = STRIPE_SECRET_KEY
                items = stripe.checkout.Session.list_line_items(
                    session_id, limit=10)
                for item in items.data:
                    item = item.to_dict()
                    price = item.get("price") or {}
                    if price.get("id"):
                        price_id = price["id"]
                        break
            except Exception as exc:
                print("WARN: line items failed for %s: %s"
                      % (session_id, exc))
        plan = plan_by_price_id(price_id) if price_id else None
        if not plan:
            print("WARN: webhook session %s: price %s is not in "
                  "stripe_plans.json — no license created. "
                  "Owner: add the Price ID to stripe_plans.json."
                  % (session_id, price_id))
            conn.execute(
                "INSERT OR IGNORE INTO webhook_events "
                "(stripe_session_id, processed_at) VALUES (?, ?)",
                (session_id, _iso(_now())))
            conn.commit()
            return jsonify({"received": True,
                            "error": "unknown_price"})
        lic = create_license(conn, email, plan,
                             stripe_session_id=session_id)
        conn.execute(
            "INSERT OR IGNORE INTO webhook_events "
            "(stripe_session_id, processed_at) VALUES (?, ?)",
            (session_id, _iso(_now())))
        conn.commit()
        print("INFO: license %s (%s) created for %s from session %s"
              % (lic["license_key"], plan["id"], email, session_id))
        return jsonify({"received": True,
                        "license_key": lic["license_key"]})
    finally:
        conn.close()


@app.route("/success")
def success():
    """Page shown after payment. Displays the license key for
    that purchase (looked up by Stripe session id; if the
    webhook has not fired yet and Stripe is configured, it
    tries the restore path first)."""
    session_id = (request.args.get("session_id") or "").strip()
    key = None
    email = None
    plan_name = None
    if session_id:
        conn = db()
        try:
            row = conn.execute(
                "SELECT * FROM licenses WHERE stripe_session_id = ?",
                (session_id,)).fetchone()
            if row:
                key = row["license_key"]
                email = row["email"]
                plan_name = row["plan_name"]
        finally:
            conn.close()
        if key is None and STRIPE_SECRET_KEY:
            # Webhook may not have fired yet — pull the session
            # from Stripe and create the license now.
            try:
                import stripe
                stripe.api_key = STRIPE_SECRET_KEY
                session = stripe.checkout.Session.retrieve(
                    session_id).to_dict()
                details = session.get("customer_details") or {}
                em = _norm_email(details.get("email")
                                 or session.get("customer_email"))
                if em and EMAIL_RE.match(em):
                    items = stripe.checkout.Session.list_line_items(
                        session_id, limit=10)
                    for item in items.data:
                        item = item.to_dict()
                        price = item.get("price") or {}
                        plan = (plan_by_price_id(price.get("id"))
                                if price.get("id") else None)
                        if plan:
                            conn = db()
                            try:
                                lic = create_license(
                                    conn, em, plan,
                                    stripe_session_id=session_id)
                                key = lic["license_key"]
                                email = em
                                plan_name = plan.get("name")
                            finally:
                                conn.close()
                            break
            except Exception as exc:
                print("WARN: /success restore failed for %s: %s"
                      % (session_id, exc))
    if key:
        body = ("<h1>Payment successful</h1>"
                "<p>Plan: %s</p>"
                "<p>Your license key (save it — you will enter it "
                "in the app):</p>"
                "<h2 style='font-family:monospace'>%s</h2>"
                "<p>Open the Listing Matcher app and choose "
                "'I already paid — activate', then enter your email "
                "(%s) or paste this key.</p>"
                % (plan_name or "", key, email or ""))
    else:
        body = ("<h1>Payment received</h1>"
                "<p>Your license is being created. If this page "
                "does not show a key, open the Listing Matcher app "
                "and choose 'I already paid — activate' with the "
                "email you paid with — the app will find your "
                "license.</p>")
    return ("<!doctype html><html><head><meta charset='utf-8'>"
            "<title>Listing Matcher — Payment</title></head>"
            "<body>%s</body></html>" % body)


# ------------------------------------------------------------------
# run
# ------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", "5000"))
    print("Listing Matcher license server v%s on port %d "
          "(db: %s)" % (VERSION, port, DB_PATH))
    app.run(host="0.0.0.0", port=port, debug=False)
else:
    init_db()
