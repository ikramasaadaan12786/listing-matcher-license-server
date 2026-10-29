# Turso migration — license server database

**Why:** the license server runs on Render's free plan, whose disk
is ephemeral. The 2026-10-29 redeploy wiped the local SQLite file
and with it every account and license (paid licenses could be
re-created from Stripe, manual licenses could not). The database
now lives in Turso (libSQL), which is persistent and free at this
scale. The SQLite file remains the default whenever the Turso
env vars are absent (local dev, tests).

**How it works:** `app.py`'s `db()` is the only switch point.
When BOTH env vars below are set it connects to Turso via the
`libsql` package (a thin shim gives its tuple rows the sqlite3
`row["col"]` / `dict(row)` surface the code uses); otherwise it
opens the local SQLite file exactly as before. No endpoint knows
which backend served it.

## The switch (owner, on Render)

Service: `listing-matcher-license-server` → Environment → add:

| Variable | Value |
|---|---|
| `TURSO_DATABASE_URL` | the Turso database URL, `libsql://<db-name>-<org>.turso.io` |
| `TURSO_AUTH_TOKEN` | the Turso auth token for that database |

Then save — Render redeploys automatically. No code change, no
other setting. (While either variable is missing the server keeps
running on the local SQLite file, unchanged.)

## What happens on first boot

`init_db()` runs at startup and creates the three tables
(`accounts`, `licenses`, `webhook_events`) in the empty Turso
database with the same `CREATE TABLE IF NOT EXISTS` schema as
SQLite. Nothing else is seeded — plans still come from
`stripe_plans.json`, releases from `releases/app-version.json`.

## What does NOT migrate

**The current Render SQLite data is NOT carried over.** Anything
still only in the ephemeral file (accounts, trial credits, and
any manual licenses created after the 2026-10-29 wipe) stays
behind. Concretely after the switch:

- The owner re-creates his own manual license once in `/admin`
  (same as after the wipe) and re-activates the app with the new
  key.
- Any client who paid via Stripe is healed automatically: their
  license is re-created from Stripe on their next status/activate
  call (the existing restore path).
- Trial accounts/credits start fresh — they were never
  recoverable across a wipe, by design.

## Post-switch verification

1. Open `/admin` — it loads (empty user list is expected at this
   point).
2. Create the owner's manual license in `/admin`, activate the
   desktop app with the new key, confirm the matcher opens as
   licensed.
3. **Survival test:** trigger one more redeploy (Render →
   Manual Deploy, or any push to this repo), wait for it to go
   live, then check `/admin` again — the license must still be
   there, and the app must still show licensed. That is the
   proof the database is no longer on the ephemeral disk.
4. Optional deep check (from any machine with the two env vars):
   `python3 tests/test_turso.py` — 9 checks against the live
   Turso DB through the app's own DB layer, self-cleaning.

## Rollback

Remove the two env vars on Render and save. The server falls
back to the local SQLite file on the next boot (empty on the
free plan — i.e. rollback means starting over, so only do it if
Turso itself is the problem).
