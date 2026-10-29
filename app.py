# Listing Matcher — License Server (client product)
#
# Flask app that serves the desktop (Windows/macOS) and Android
# clients: signup + 5 trial credits, license activation
# (machine-bound, one device at a time), Stripe webhook ->
# auto-generated licenses, and the app-version endpoint for
# client auto-update.
#
# Data model (SQLite file next to app.py unless DATA_DIR is set;
# or a persistent Turso/libSQL database when TURSO_DATABASE_URL +
# TURSO_AUTH_TOKEN are set — see the database section and
# TURSO_MIGRATION.md):
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
# v1.1.0: admin panel (GET /admin) — the owner sees every
# user and can create a license manually when someone pays
# outside Stripe (cash / bank transfer). Admin auth is the
# ADMIN_PASSWORD env var; when it is unset /admin returns 404.
# Secrets: STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET,
# ADMIN_PASSWORD — env vars only, never in code. No passwords
# are stored (signup is email only). No real owner data
# anywhere in this repo.

import csv
import hmac
import html
import io
import json
import os
import re
import secrets
import sqlite3
import string
from datetime import datetime, timedelta, timezone

from flask import (Flask, Response, abort, jsonify, redirect, request,
                   send_from_directory, session)

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

# Admin panel password (v1.1.0). The owner sets this on Render;
# it is never hardcoded, logged or returned. When unset, /admin
# returns 404 (the feature is off).
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip()

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
#
# Two backends, chosen purely by env at boot:
#   * default — local SQLite file (DB_PATH). Unchanged behaviour.
#   * Turso — when BOTH TURSO_DATABASE_URL and TURSO_AUTH_TOKEN
#     are set. Render's free disk is ephemeral (a redeploy wiped
#     every account/license on 2026-10-29), so production points
#     the server at a persistent Turso database; the SQLite file
#     stays the fallback for local runs/tests.
#
# The Turso backend talks to Turso's HTTP pipeline API
# (POST /v2/pipeline, Hrana over plain HTTPS) with the standard
# library only — NO native libsql client. The native client
# shipped in f33e92c deadlocked in production: its Rust/tokio
# runtime hung the single gunicorn sync worker on DB requests
# ("WORKER TIMEOUT" -> SIGKILL; tokio panic "failed to join
# thread: Resource deadlock avoided"), producing 502 windows
# while /api/health (which touches no DB) kept answering.
# Plain HTTP has no native runtime to deadlock, and every call
# carries a hard 10s timeout plus one retry on network errors,
# so a slow Turso response can never hang a worker indefinitely.
#
# The switch lives ONLY in db() below: _TursoHttpConnection /
# _TursoHttpCursor give the HTTP backend the exact sqlite3
# surface the rest of this file uses — conn.execute(sql,
# params) with ? placeholders, fetchone()/fetchall(),
# cur.lastrowid, executescript(), commit()/close(), and rows
# that support row["col"] and dict(row) like sqlite3.Row (via
# _DictRow). No endpoint code knows which backend served it.
#
# Transaction semantics: the HTTP API is stateless and every
# statement auto-commits, so commit()/rollback() are no-ops on
# this backend. Every multi-statement flow was reviewed for
# autocommit safety (2026-10-29):
#   * webhook: the webhook_events marker is inserted AFTER the
#     license is created, and create_license is idempotent on
#     stripe_session_id (SELECT-first + UNIQUE column), so a
#     crash/retry between the two inserts replays safely.
#   * create_license: SELECT existing -> INSERT -> SELECT by
#     key; statements from one connection run sequentially and
#     Turso reads observe this client's prior writes in order.
#   * signup / consume / activate / revoke: check-then-write
#     pairs with the same shape (and the same single-worker
#     serialisation) as the SQLite path; the schema's UNIQUE
#     constraints still guard the races that matter.
# ------------------------------------------------------------------

import base64
import urllib.error
import urllib.request

TURSO_DATABASE_URL = os.environ.get("TURSO_DATABASE_URL", "").strip()
TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN", "").strip()
USE_TURSO = bool(TURSO_DATABASE_URL and TURSO_AUTH_TOKEN)

_TURSO_HTTP_TIMEOUT = 10  # seconds, per pipeline HTTP call


class _DictRow:
    """sqlite3.Row stand-in built from a result tuple row.

    Supports the two access patterns this codebase uses:
    row["column"] (and row[index]) and dict(row) via keys().
    Iterating yields values, like sqlite3.Row."""

    __slots__ = ("_cols", "_vals", "_map")

    def __init__(self, cols, vals):
        self._cols = tuple(cols)
        self._vals = tuple(vals)
        self._map = dict(zip(self._cols, self._vals))

    def __getitem__(self, key):
        if isinstance(key, str):
            return self._map[key]
        return self._vals[key]

    def keys(self):
        return list(self._cols)

    def __iter__(self):
        return iter(self._vals)

    def __len__(self):
        return len(self._vals)

    def __repr__(self):
        return "_DictRow(%r)" % (self._map,)


def _turso_http_base(url):
    """libsql://<host> -> https://<host>. An http(s):// URL
    passes through unchanged (http:// is used only by the local
    fake-Turso test stub)."""
    u = (url or "").strip()
    if u.startswith("libsql://"):
        u = "https://" + u[len("libsql://"):]
    return u.rstrip("/")


def _turso_encode_arg(value):
    """Python value -> Hrana typed value. Integers travel as
    strings in this protocol."""
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": str(int(value))}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    if isinstance(value, (bytes, bytearray)):
        return {"type": "blob",
                "value": base64.b64encode(
                    bytes(value)).decode("ascii")}
    return {"type": "text", "value": str(value)}


def _turso_decode_cell(cell):
    """Hrana typed cell -> Python value. Integers arrive as
    strings and must become int again (credits math and id
    comparisons depend on it)."""
    cell = cell or {}
    t = cell.get("type")
    v = cell.get("value")
    if t == "null" or v is None:
        return None
    if t == "integer":
        return int(v)
    if t == "float":
        return float(v)
    if t == "blob":
        return base64.b64decode(v)
    return v


def _turso_pipeline(base, token, statements):
    """Run a batch of (sql, params) statements through Turso's
    POST /v2/pipeline endpoint and return the list of
    per-statement result dicts. A statement error raises the
    closest sqlite3 error class (so behaviour matches the
    SQLite backend); a network error is retried exactly once.
    Every attempt has a hard timeout — this call must never be
    able to hang a gunicorn worker."""
    requests = []
    for sql, params in statements:
        requests.append({
            "type": "execute",
            "stmt": {
                "sql": sql,
                "args": [_turso_encode_arg(p) for p in params],
                "want_rows": True,
            },
        })
    requests.append({"type": "close"})
    body = json.dumps({"requests": requests}).encode("utf-8")
    payload = None
    last_exc = None
    for _attempt in (1, 2):
        req = urllib.request.Request(
            base + "/v2/pipeline", data=body, method="POST")
        req.add_header("Authorization", "Bearer " + token)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(
                    req, timeout=_TURSO_HTTP_TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            # Turso answered (auth failure, 5xx, ...): a real
            # response, not a network error — do not retry.
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            raise sqlite3.OperationalError(
                "Turso HTTP %s: %s" % (exc.code, detail))
        except Exception as exc:  # URLError / timeout / reset
            last_exc = exc
    if payload is None:
        raise sqlite3.OperationalError(
            "Turso request failed after one retry: %s" % (last_exc,))
    results = []
    for item in payload.get("results") or []:
        if item.get("type") == "error":
            err = item.get("error") or {}
            msg = err.get("message") or "Turso statement error"
            if "UNIQUE constraint failed" in msg:
                raise sqlite3.IntegrityError(msg)
            raise sqlite3.OperationalError(msg)
        resp_obj = item.get("response") or {}
        if resp_obj.get("type") == "execute":
            results.append(resp_obj.get("result") or {})
    return results


class _TursoHttpCursor:
    """Eager cursor over the HTTP pipeline API: execute() runs
    the statement immediately (one pipeline POST) and buffers
    the decoded rows; fetch* walk the buffer, mirroring the
    sqlite3 cursor surface this file uses."""

    def __init__(self, conn, sql=None, params=()):
        self._conn = conn
        self._rows = []
        self._pos = 0
        self._cols = []
        self._lastrowid = None
        self._rowcount = -1
        if sql is not None:
            self._run(sql, params)

    def _run(self, sql, params):
        results = _turso_pipeline(
            self._conn._base, self._conn._token,
            [(sql, tuple(params))])
        result = results[0] if results else {}
        self._cols = [c.get("name")
                      for c in result.get("cols") or []]
        self._rows = [
            _DictRow(self._cols,
                     [_turso_decode_cell(cell) for cell in row])
            for row in result.get("rows") or []]
        lid = result.get("last_insert_rowid")
        self._lastrowid = int(lid) if lid is not None else None
        if self._cols:
            self._rowcount = -1  # sqlite3 semantics for SELECT
        else:
            arc = result.get("affected_row_count")
            self._rowcount = arc if arc is not None else -1
        self._pos = 0
        return self

    def execute(self, sql, params=()):
        return self._run(sql, params)

    def fetchone(self):
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchall(self):
        rows = list(self._rows[self._pos:])
        self._pos = len(self._rows)
        return rows

    def fetchmany(self, size=None):
        if size is None:
            size = 1  # sqlite3's default arraysize
        rows = list(self._rows[self._pos:self._pos + size])
        self._pos += len(rows)
        return rows

    def __iter__(self):
        return iter(self.fetchall())

    @property
    def lastrowid(self):
        return self._lastrowid

    @property
    def rowcount(self):
        return self._rowcount

    @property
    def description(self):
        if not self._cols:
            return None
        return [(name, None, None, None, None, None, None)
                for name in self._cols]


class _TursoHttpConnection:
    """sqlite3-connection-shaped wrapper over Turso's HTTP
    pipeline API. Stateless by design: commit()/rollback()/
    close() are no-ops because every statement auto-commits
    (see the transaction review in the section header)."""

    def __init__(self, base, token):
        self._base = base
        self._token = token

    def execute(self, sql, params=()):
        return _TursoHttpCursor(self, sql, params)

    def cursor(self):
        return _TursoHttpCursor(self)

    def executescript(self, script):
        # Split into complete statements with the stdlib
        # completeness check (so a semicolon inside a string
        # literal could never split a statement in half) and
        # run them as ONE pipeline batch. Only init_db() uses
        # this, with plain CREATE TABLE IF NOT EXISTS DDL.
        statements = []
        buf = ""
        for line in script.splitlines(True):
            buf += line
            if sqlite3.complete_statement(buf):
                stmt = buf.strip()
                if stmt:
                    statements.append((stmt, ()))
                buf = ""
        tail = buf.strip()
        if tail:
            statements.append((tail, ()))
        if statements:
            _turso_pipeline(self._base, self._token, statements)
        return _TursoHttpCursor(self)

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None


def db():
    if USE_TURSO:
        return _TursoHttpConnection(
            _turso_http_base(TURSO_DATABASE_URL), TURSO_AUTH_TOKEN)
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

# Signed session cookie for the admin panel. SESSION_SECRET is
# optional: when unset, a random key is generated at boot and
# admin sessions simply do not survive a restart (the owner
# logs in again). The cookie is HttpOnly + SameSite=Lax.
app.secret_key = (os.environ.get("SESSION_SECRET", "").strip()
                  or secrets.token_hex(32))
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"


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
<li><a href="/admin">/admin</a> — owner panel: user management
+ manual licenses (needs ADMIN_PASSWORD)</li>
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
# Admin panel — user management + manual licenses (v1.1.0)
#
# GET /admin — dashboard (login form when not authed).
# POST /admin/login — form or JSON {password}.
# GET /admin/logout — clears the session.
# POST /api/admin/create-license {email, plan_id} — manual
#   license (cash / bank-transfer customers). Reuses
#   create_license() with stripe_session_id=None, so the key
#   activates exactly like a Stripe license (email-only
#   activation or key entry in the app).
# POST /api/admin/revoke-license {license_key} — expires a
#   license immediately (sets expires_at to now).
# GET /api/admin/export — CSV of all accounts + their license.
#
# Auth: ADMIN_PASSWORD env var (owner sets it on Render).
# When unset, /admin returns 404 (feature off). Wrong password
# -> 401, no hint. The admin session is a signed Flask session
# cookie. No password is ever logged or returned.
# ------------------------------------------------------------------

def _admin_enabled():
    return bool(ADMIN_PASSWORD)


def _admin_authed():
    return session.get("admin") is True


def _require_admin():
    """None when the caller is the admin; otherwise the Flask
    response to return (404 when the feature is off, 401 when
    not logged in)."""
    if not _admin_enabled():
        abort(404)
    if not _admin_authed():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    return None


def _license_status_label(row):
    """trial (no license) / active (valid + bound) /
    not_activated (valid, never bound) / expired."""
    if row is None:
        return "trial"
    if license_is_valid(row):
        return "active" if row["bound_machine_id"] else "not_activated"
    return "expired"


def _esc(value):
    return html.escape("" if value is None else str(value),
                       quote=True)


_ADMIN_CSS = (
    "<style>"
    "*{box-sizing:border-box}"
    "body{margin:0;background:#0a1628;color:#e8edf5;"
    "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,"
    "Arial,sans-serif}"
    "a{color:#d4af37}"
    ".wrap{max-width:1080px;margin:0 auto;padding:24px 16px 48px}"
    "header{display:flex;flex-wrap:wrap;align-items:baseline;"
    "justify-content:space-between;gap:8px;margin-bottom:20px}"
    "h1{color:#d4af37;margin:0;font-size:28px}"
    "h2{color:#d4af37;margin:0 0 12px;font-size:20px}"
    ".card h2{color:#16233a}"
    "nav a{color:#d4af37;text-decoration:none;font-weight:600;"
    "margin-left:14px}"
    "nav a:first-child{margin-left:0}"
    ".card{background:#fff;color:#16233a;border-radius:14px;"
    "padding:20px;margin-bottom:18px;box-shadow:0 8px 30px rgba(0,0,0,.35)}"
    ".card a{color:#8a6d1b}"
    ".stats{display:grid;grid-template-columns:repeat(4,1fr);"
    "gap:12px;margin-bottom:10px}"
    ".stat{background:#12233f;border:1px solid #24365c;"
    "border-radius:12px;padding:14px}"
    ".stat-label{display:block;color:#9fb0cc;font-size:13px;"
    "margin-bottom:4px}"
    ".stat-value{display:block;color:#d4af37;font-size:26px;"
    "font-weight:700}"
    ".note{color:#9fb0cc;font-size:13px;margin:0 0 18px}"
    "label{font-weight:600}"
    "input,select{padding:10px 12px;border:1px solid #c3cad6;"
    "border-radius:8px;font-size:15px;background:#fff;color:#16233a}"
    "button{background:#d4af37;color:#16233a;border:0;border-radius:8px;"
    "padding:10px 18px;font-size:15px;font-weight:700;cursor:pointer}"
    "button:hover{filter:brightness(1.08)}"
    "button.revoke{background:#fff;color:#5b6472;"
    "border:1px solid #c3cad6;padding:6px 12px;font-size:13px;"
    "font-weight:600}"
    ".table-wrap{overflow-x:auto;border:1px solid #e2e6ee;"
    "border-radius:10px}"
    "table{width:100%;border-collapse:collapse;min-width:640px;"
    "font-size:14px}"
    "th{background:#d4af37;color:#16233a;text-align:left;"
    "padding:10px 12px}"
    "td{padding:10px 12px;border-top:1px solid #eef0f4;"
    "vertical-align:top}"
    "tr:nth-child(even) td{background:#faf6e8}"
    "code{font-family:ui-monospace,Menlo,Consolas,monospace;"
    "background:#f1ead0;padding:2px 6px;border-radius:6px;"
    "word-break:break-all}"
    ".keybox{background:#faf6e8;border:1px solid #d4af37;"
    "border-radius:12px;padding:14px;margin:14px 0}"
    ".keybox h2{color:#16233a;word-break:break-all}"
    ".err{color:#ff6b6b;font-weight:700}"
    ".login-wrap{min-height:100vh;display:flex;align-items:center;"
    "justify-content:center;padding:20px}"
    ".login-card{background:#fff;color:#16233a;border-radius:16px;"
    "padding:28px;width:100%;max-width:420px;"
    "box-shadow:0 12px 40px rgba(0,0,0,.45)}"
    ".login-card h1{color:#16233a;margin-bottom:6px}"
    ".login-card .sub{color:#5b6472;margin:0 0 18px}"
    ".login-card label{display:block;margin-bottom:14px}"
    ".login-card input{width:100%;margin-top:6px}"
    ".login-card button{width:100%}"
    "@media(max-width:640px){"
    ".stats{grid-template-columns:repeat(2,1fr)}"
    "h1{font-size:24px}.wrap{padding:16px 12px 32px}"
    ".card{padding:16px}}"
    "</style>")


def _admin_login_page(error=False):
    msg = ("<p class='err'>Wrong password.</p>"
           if error else "")
    return ("<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,"
            " initial-scale=1'>"
            "<title>Listing Matcher — Admin login</title>"
            + _ADMIN_CSS + "</head><body>"
            "<div class='login-wrap'><div class='login-card'>"
            "<h1>Listing Matcher — Admin</h1>" + msg +
            "<p class='sub'>Sign in to manage users and licenses.</p>"
            "<form method='post' action='/admin/login'>"
            "<label>Admin password<br>"
            "<input type='password' name='password' autofocus>"
            "</label>"
            "<button type='submit'>Sign in</button></form>"
            "</div></div>"
            "</body></html>")


@app.route("/admin")
def admin():
    if not _admin_enabled():
        abort(404)
    if not _admin_authed():
        return _admin_login_page()
    return _admin_dashboard(request.args.get("q", "").strip())


@app.route("/admin/login", methods=["POST"])
def admin_login():
    if not _admin_enabled():
        abort(404)
    data = request.get_json(silent=True) or {}
    pw = data.get("password") or request.form.get("password") or ""
    if not pw or not hmac.compare_digest(pw, ADMIN_PASSWORD):
        if request.is_json:
            return jsonify({"ok": False,
                            "error": "unauthorized"}), 401
        return _admin_login_page(error=True), 401
    session["admin"] = True
    if request.is_json:
        return jsonify({"ok": True})
    return redirect("/admin")


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect("/admin")


def _admin_dashboard(query):
    conn = db()
    try:
        accounts = conn.execute(
            "SELECT * FROM accounts ORDER BY created_at DESC"
            ).fetchall()
        licenses = conn.execute(
            "SELECT * FROM licenses ORDER BY created_at DESC"
            ).fetchall()
    finally:
        conn.close()

    # --- stats -------------------------------------------------
    total_accounts = len(accounts)
    lic_emails = {r["email"] for r in licenses}
    valid_emails = {r["email"] for r in licenses
                    if license_is_valid(r)}
    trial_only = sum(1 for a in accounts
                     if a["email"] not in lic_emails)
    licensed = len(valid_emails)
    # Trial credits used = sum of (5 - credits left). Every
    # account starts at 5 and credits only ever decrease, so
    # this is exact — labelled as such on the page.
    trial_used = sum(max(0, TRIAL_CREDITS - (a["credits"] or 0))
                     for a in accounts)

    # Latest license per email (same rule as
    # find_license_for_email: most recent valid, else most
    # recent of any status). licenses are already newest-first.
    by_email = {}
    for row in licenses:
        by_email.setdefault(row["email"], []).append(row)

    def latest_license(email):
        rows = by_email.get(email, [])
        for row in rows:
            if license_is_valid(row):
                return row
        return rows[0] if rows else None

    # --- users table ------------------------------------------
    q = (query or "").strip().lower()
    shown = [a for a in accounts
             if not q or q in a["email"].lower()][:500]
    user_rows = []
    for a in shown:
        lic = latest_license(a["email"])
        if lic:
            lic_txt = ("%s — %s — expires %s"
                       % (_esc(lic["plan_name"] or lic["plan_id"]),
                          _license_status_label(lic),
                          _esc(lic["expires_at"] or "never")))
        else:
            lic_txt = "trial — no license"
        user_rows.append(
            "<tr><td>%s</td><td>%s<br><small>%s</small></td>"
            "<td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (_esc(a["email"]),
               _esc(a["machine_id"]), _esc(a["device_name"] or ""),
               _esc(a["credits"]), lic_txt,
               _esc(a["created_at"]), _esc(a["updated_at"])))

    # --- licenses table ---------------------------------------
    lic_rows = []
    for row in licenses[:200]:
        status = _license_status_label(row)
        revoke = ("<button class='revoke' data-key='%s'>Revoke"
                  "</button>" % _esc(row["license_key"])
                  if status != "expired" else "—")
        lic_rows.append(
            "<tr><td><code>%s</code></td><td>%s</td><td>%s</td>"
            "<td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (_esc(row["license_key"]), _esc(row["email"]),
               _esc(row["plan_name"] or row["plan_id"]),
               _esc(row["expires_at"] or "never"),
               _esc(row["bound_machine_id"] or "—"),
               _esc(row["created_at"]), revoke))

    # --- create-license form ----------------------------------
    plan_options = "".join(
        "<option value='%s'>%s — AED %s</option>"
        % (_esc(p["id"]), _esc(p.get("name") or p["id"]),
           _esc(p.get("price_aed")))
        for p in load_plans().get("plans", []))

    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,"
        " initial-scale=1'>"
        "<title>Listing Matcher — Admin</title>" + _ADMIN_CSS +
        "</head><body><div class='wrap'>"
        "<header><h1>Listing Matcher — Admin</h1>"
        "<nav><a href='/admin/logout'>Sign out</a>"
        "<a href='/api/admin/export'>Download users CSV</a></nav></header>"
        "<div class='stats'>"
        "<div class='stat'><span class='stat-label'>Total accounts:</span>"
        "<span class='stat-value'>%d</span></div>"
        "<div class='stat'><span class='stat-label'>Trial-only:</span>"
        "<span class='stat-value'>%d</span></div>"
        "<div class='stat'><span class='stat-label'>Licensed:</span>"
        "<span class='stat-value'>%d</span></div>"
        "<div class='stat'><span class='stat-label'>Trial credits used:</span>"
        "<span class='stat-value'>%d</span></div></div>"
        "<p class='note'>(5 − credits left, summed over accounts)</p>"
        "<div class='card'><h2>Create a manual license</h2>"
        "<p>For a customer who paid outside Stripe (cash / bank "
        "transfer). The key activates exactly like a Stripe "
        "license — the customer enters their email (or the key) "
        "in the app.</p>"
        "<form id='mklic'>"
        "<input name='email' type='email' required size='30' "
        "placeholder='customer@email.com'> "
        "<select name='plan_id'>%s</select> "
        "<button type='submit'>Create license</button></form>"
        "<div id='newkey' class='keybox' style='display:none'>"
        "<p>License key — send this to the customer:</p>"
        "<h2 id='keyval' style='font-family:monospace'></h2>"
        "<p id='keymeta'></p><p id='keyerr' style='color:red'></p>"
        "<button type='button' onclick='copyKey()'>Copy key"
        "</button></div>"
        "</div><div class='card'><h2>Users</h2>"
        "<form method='get' action='/admin'>"
        "<input name='q' value='%s' placeholder='search by email'> "
        "<button type='submit'>Search</button></form>"
        "<p>Showing %d of %d accounts (newest first).</p>"
        "<div class='table-wrap'><table>"
        "<tr><th>Email</th><th>Machine / Device</th>"
        "<th>Credits left</th><th>License</th><th>Signed up</th>"
        "<th>Last updated</th></tr>%s</table></div>"
        "</div><div class='card'><h2>Licenses</h2>"
        "<div class='table-wrap'><table>"
        "<tr><th>License key</th><th>Email</th><th>Plan</th>"
        "<th>Expires</th><th>Bound machine</th><th>Created</th>"
        "<th></th></tr>%s</table></div></div></div>"
        "<script>"
        "async function postJSON(path, body) {"
        "  const r = await fetch(path, {method: 'POST',"
        "    headers: {'Content-Type': 'application/json'},"
        "    body: JSON.stringify(body)});"
        "  return r.json();"
        "}"
        "document.getElementById('mklic').addEventListener('submit',"
        " async (e) => {"
        "  e.preventDefault();"
        "  const f = e.target;"
        "  const b = await postJSON('/api/admin/create-license',"
        "    {email: f.email.value, plan_id: f.plan_id.value});"
        "  const box = document.getElementById('newkey');"
        "  box.style.display = 'block';"
        "  if (b.ok) {"
        "    document.getElementById('keyval').textContent ="
        "      b.license_key;"
        "    document.getElementById('keymeta').textContent ="
        "      b.plan_name + ' — expires ' +"
        "      (b.expires_at || 'never') + ' — for ' + b.email;"
        "    document.getElementById('keyerr').textContent = '';"
        "  } else {"
        "    document.getElementById('keyval').textContent = '';"
        "    document.getElementById('keymeta').textContent = '';"
        "    document.getElementById('keyerr').textContent ="
        "      'Error: ' + (b.error || 'failed');"
        "  }"
        "});"
        "function copyKey() {"
        "  const k = document.getElementById('keyval').textContent;"
        "  if (navigator.clipboard) { navigator.clipboard.writeText(k); }"
        "}"
        "document.querySelectorAll('button.revoke').forEach((btn) => {"
        "  btn.addEventListener('click', async () => {"
        "    if (!confirm('Revoke license ' + btn.dataset.key + '?'))"
        "      return;"
        "    const b = await postJSON('/api/admin/revoke-license',"
        "      {license_key: btn.dataset.key});"
        "    if (b.ok) { location.reload(); }"
        "    else { alert('Error: ' + (b.error || 'failed')); }"
        "  });"
        "});"
        "</script>"
        "</body></html>"
        % (total_accounts, trial_only, licensed, trial_used,
           plan_options, _esc(query), len(shown), total_accounts,
           "".join(user_rows), "".join(lic_rows)))


@app.route("/api/admin/create-license", methods=["POST"])
def admin_create_license():
    guard = _require_admin()
    if guard is not None:
        return guard
    data = request.get_json(silent=True) or {}
    email = _norm_email(data.get("email"))
    plan = plan_by_id((data.get("plan_id") or "").strip())
    if not EMAIL_RE.match(email) or plan is None:
        return jsonify({"ok": False,
                        "error": "invalid_request"}), 400
    conn = db()
    try:
        lic = create_license(conn, email, plan,
                             stripe_session_id=None)
    finally:
        conn.close()
    print("INFO: manual license %s (%s) created for %s by admin"
          % (lic["license_key"], plan["id"], email))
    return jsonify({"ok": True, "license_key": lic["license_key"],
                    "plan": lic["plan_id"],
                    "plan_name": lic.get("plan_name"),
                    "expires_at": lic["expires_at"],
                    "email": email})


@app.route("/api/admin/revoke-license", methods=["POST"])
def admin_revoke_license():
    guard = _require_admin()
    if guard is not None:
        return guard
    data = request.get_json(silent=True) or {}
    key = _norm_key(data.get("license_key"))
    if not key:
        return jsonify({"ok": False,
                        "error": "invalid_request"}), 400
    conn = db()
    try:
        row = conn.execute(
            "SELECT * FROM licenses WHERE "
            "REPLACE(REPLACE(UPPER(license_key), '-', ''), ' ', '')"
            " = ?", (key,)).fetchone()
        if row is None:
            return jsonify({"ok": False,
                            "error": "invalid_key"}), 404
        now_iso = _iso(_now())
        conn.execute("UPDATE licenses SET expires_at = ? "
                     "WHERE id = ?", (now_iso, row["id"]))
        conn.commit()
    finally:
        conn.close()
    print("INFO: license %s (%s) revoked by admin"
          % (row["license_key"], row["email"]))
    return jsonify({"ok": True, "license_key": row["license_key"],
                    "email": row["email"], "expires_at": now_iso})


@app.route("/api/admin/export")
def admin_export():
    guard = _require_admin()
    if guard is not None:
        return guard
    conn = db()
    try:
        rows = conn.execute(
            "SELECT * FROM accounts ORDER BY created_at DESC"
            ).fetchall()
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["email", "machine_id", "device_name", "credits",
                    "plan", "plan_name", "expires_at", "status",
                    "created_at", "updated_at"])
        for a in rows:
            lic = find_license_for_email(conn, a["email"])
            if lic:
                plan = lic["plan_id"]
                plan_name = lic["plan_name"] or ""
                exp = lic["expires_at"] or ""
                status = _license_status_label(lic)
            else:
                plan = plan_name = exp = ""
                status = "trial"
            w.writerow([a["email"], a["machine_id"],
                        a["device_name"] or "", a["credits"],
                        plan, plan_name, exp, status,
                        a["created_at"], a["updated_at"]])
        data = out.getvalue()
    finally:
        conn.close()
    fname = ("listing-matcher-users-%s.csv"
             % _now().strftime("%Y-%m-%d"))
    return Response(data, mimetype="text/csv",
                    headers={"Content-Disposition":
                             "attachment; filename=%s" % fname})


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
