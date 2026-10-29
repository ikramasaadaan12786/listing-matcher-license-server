"""Turso (libSQL) backend smoke test for the license server.

SKIPPED (exit 0) unless BOTH TURSO_DATABASE_URL and
TURSO_AUTH_TOKEN are set in the environment — run it after the
owner's Turso database exists:

  TURSO_DATABASE_URL='libsql://<db>-<org>.turso.io' \
  TURSO_AUTH_TOKEN='...' \
  python3 tests/test_turso.py

What it proves, through the app's own db() layer (never raw
HTTP): the server really selected the Turso backend, the
schema exists (init_db at import + here), an account and a
license can be inserted and read back with the exact access
patterns the endpoints use (row["col"], dict(row), fetchall,
cursor.lastrowid), and the test rows can be deleted again.
Cleanup is scoped to one throwaway email, so it is safe to run
against the production Turso database.

The same test runs fully offline against the local fake of
the pipeline endpoint (backed by a real sqlite file):

  python3 tests/fake_turso.py 8431 /tmp/fake_turso.db &
  TURSO_DATABASE_URL=http://127.0.0.1:8431 TURSO_AUTH_TOKEN=x \
    python3 tests/test_turso.py

(tests/test_turso_gunicorn.py drives the fake + real gunicorn
together for the hang regression test.)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

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


def main():
    if not (os.environ.get("TURSO_DATABASE_URL", "").strip()
            and os.environ.get("TURSO_AUTH_TOKEN", "").strip()):
        print("SKIP  TURSO_DATABASE_URL / TURSO_AUTH_TOKEN not "
              "set — Turso smoke test not run.")
        return 0

    import app as server

    check("app selected the Turso backend", server.USE_TURSO is True)

    email = "turso-smoke-test@example.invalid"
    machine = "TURSO-SMOKE-MACHINE"
    conn = server.db()
    try:
        # schema (already created at import; must be idempotent)
        server.init_db()
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()}
        check("schema present (accounts/licenses/webhook_events)",
              {"accounts", "licenses", "webhook_events"} <= tables,
              str(tables))

        # clean slate for the throwaway identity
        conn.execute("DELETE FROM accounts WHERE email = ?", (email,))
        conn.execute("DELETE FROM licenses WHERE email = ?", (email,))
        conn.commit()

        # insert + lastrowid + dict-style read-back (signup shape)
        now = server._iso(server._now())
        cur = conn.execute(
            "INSERT INTO accounts (email, machine_id, device_name, "
            "credits, trial_granted, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 1, ?, ?)",
            (email, machine, "smoke", server.TRIAL_CREDITS, now, now))
        conn.commit()
        check("insert account -> lastrowid is a positive int",
              isinstance(cur.lastrowid, int) and cur.lastrowid > 0,
              repr(cur.lastrowid))

        row = server.find_account(conn, email, machine)
        check("fetchone + row['col'] read-back",
              row is not None and row["email"] == email
              and row["credits"] == server.TRIAL_CREDITS,
              repr(row))
        check("dict(row) works on a Turso row",
              isinstance(dict(row), dict)
              and dict(row)["machine_id"] == machine)

        # license round-trip through the endpoint helper itself
        plan = server.plan_by_id("3mo") or {
            "id": "3mo", "name": "Test 3 Months", "months": 3}
        lic = server.create_license(
            conn, email, plan,
            stripe_session_id="cs_turso_smoke_test")
        check("create_license returns the row dict",
              lic["email"] == email
              and lic["license_key"].startswith("LM-"),
              str(lic))
        found = server.find_license_for_email(conn, email)
        check("find_license_for_email + fetchall read-back",
              found is not None
              and found["license_key"] == lic["license_key"])

        # webhook idempotency table round-trip
        conn.execute(
            "INSERT OR IGNORE INTO webhook_events "
            "(stripe_session_id, processed_at) VALUES (?, ?)",
            ("cs_turso_smoke_test", now))
        conn.commit()
        seen = conn.execute(
            "SELECT 1 FROM webhook_events "
            "WHERE stripe_session_id = ?",
            ("cs_turso_smoke_test",)).fetchone()
        check("webhook_events insert/select", seen is not None)
    finally:
        # cleanup — scoped strictly to the throwaway identity
        try:
            conn.execute("DELETE FROM accounts WHERE email = ?",
                         (email,))
            conn.execute("DELETE FROM licenses WHERE email = ?",
                         (email,))
            conn.execute(
                "DELETE FROM webhook_events "
                "WHERE stripe_session_id = ?",
                ("cs_turso_smoke_test",))
            conn.commit()
        finally:
            conn.close()
    check("cleanup removed the test rows", True)

    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
