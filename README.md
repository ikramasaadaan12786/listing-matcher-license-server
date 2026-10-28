# Listing Matcher — License Server

Flask license server for the Listing Matcher client product
(Windows / macOS / Android). Signup + 5 trial credits, license
activation (machine-bound, one device at a time), Stripe
webhook → auto-generated licenses, and the app-version endpoint
for client auto-update.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | info page |
| GET | `/api/health` | Render health check |
| GET | `/api/version` | `{version, module}` |
| GET | `/api/plans` | subscription plans (Price IDs + payment links) |
| GET | `/api/app-version` | `{version, windows_url, mac_url, mac_arm64_url, android_url}` |
| GET | `/releases/<file>` | download a release file from `releases/` |
| POST | `/api/signup` | `{email, machine_id, device_name?}` → 5 trial credits |
| GET | `/api/status?email=&machine_id=` | `{credits, license, licensed}` |
| POST | `/api/consume` | deduct 1 trial credit (licensed users: no deduction) |
| POST | `/api/activate` | `{email, license_key, machine_id}` → bind to one machine |
| POST | `/api/stripe-webhook` | Stripe `checkout.session.completed` → license |
| GET | `/success?session_id=` | payment success page, shows the license key |
| GET | `/admin` | owner panel: user management + manual licenses |
| POST | `/admin/login` | admin sign-in (form or JSON `{password}`) |
| GET | `/admin/logout` | sign out |
| POST | `/api/admin/create-license` | manual license `{email, plan_id}` → `{license_key, ...}` |
| POST | `/api/admin/revoke-license` | expire a license `{license_key}` |
| GET | `/api/admin/export` | CSV download of all accounts + licenses |

Rules (SPEC §3–§6): 1 credit per **chargeable** match only —
the app must never call `/api/consume` for a zero result. A
license means unlimited matches for its validity. One trial
per machine. A license binds to ONE machine; activating the
same key on a different machine is rejected
(`already_bound_other_device`).

## Render deploy (free)

Repo: `ikramasaadaan12786/listing-matcher-license-server`
(private). `render.yaml` is a Blueprint (free web service,
Singapore, health check `/api/health`).

1. Render Dashboard → **New +** → **Blueprint** → connect this
   repo → **Apply**. (First deploy takes ~1–2 min.)
2. Dashboard → the service → **Environment** → add:
   - `STRIPE_SECRET_KEY` — the restricted `rk_live_...` key
     (same key the owner made for App 26; needs read access to
     Checkout Sessions + Customers for the restore path).
     Required for the restore-from-Stripe path; without it the
     server still works but a wiped DB cannot re-create paid
     licenses automatically.
   - `STRIPE_WEBHOOK_SECRET` — the `whsec_...` secret from the
     Stripe Dashboard webhook endpoint (see below). Required
     for licenses to auto-generate on payment.
   - `STRIPE_PLANS_JSON` — optional. Only if the plans should
     come from an env var instead of `stripe_plans.json` in the
     repo (same JSON shape).
   - `ADMIN_PASSWORD` — the admin panel password (owner picks
     it, e.g. a long random string). Required for `/admin` to
     work at all — without it the panel returns 404. Set it on
     Render the same way as the keys above. **Never share it
     in chat.**
   - `SESSION_SECRET` — optional. Signs the admin session
     cookie; when unset a random key is generated at boot and
     admin sessions do not survive a restart (just log in
     again).
   - No other env vars are needed. `PORT` is set by Render.
3. Stripe Dashboard → Developers → Webhooks → **Add endpoint**:
   URL `https://<your-service>.onrender.com/api/stripe-webhook`,
   event `checkout.session.completed`. Copy the signing secret
   (`whsec_...`) into `STRIPE_WEBHOOK_SECRET` above and redeploy.
4. Verify live:
   ```bash
   curl https://<your-service>.onrender.com/api/health
   curl https://<your-service>.onrender.com/api/plans
   curl https://<your-service>.onrender.com/api/version
   ```

## Stripe plans

`stripe_plans.json` (in this repo) holds the 3 one-time plans
with their Price IDs + payment links (already created by the
owner on the Stripe Dashboard — Alt Softs Solutions FZ-LLC):

| Plan | AED | Price ID |
|---|---|---|
| 3 Months | 299 | `price_1UVYvbCAFAZd2QaFmO4b1f0H` |
| 6 Months | 499 | `price_1UVYwiCAFAZd2QaFjSJJJpp9` |
| Lifetime | 999 | `price_1UVYxeCAFAZd2QaF5i3YvYW3` |

Payment links: https://buy.stripe.com/28E9AS35q6Zf9fz1M70kE05 (3mo),
https://buy.stripe.com/cNi9ASbBW1EVdvP9ez0kE06 (6mo),
https://buy.stripe.com/00waEW5dy3N3gI18av0kE07 (lifetime).
The server reads `STRIPE_PLANS_JSON` env first, then this
file, then built-in defaults (null Price IDs). If the file is
absent it does NOT crash — webhooks log a warning and skip
license creation until the Price IDs are configured.

## Releases / auto-update

New client builds go in `releases/` and `releases/app-version.json`
is updated with the new version + download URLs. The apps poll
`GET /api/app-version`. URLs may be absolute (e.g. GitHub
Release assets) or relative `/releases/<file>` paths served by
this server. License + trial credits survive updates — they live
on this server, keyed by email + machine_id, not in the install.

## Admin panel (v1.1.0) — user management + manual licenses

Open `https://<your-service>.onrender.com/admin` and sign in
with the `ADMIN_PASSWORD` you set on Render. (Without that env
var the page returns 404.)

- **Users:** every account that signed up — email, machine /
  device, credits left, license (plan + expiry + status:
  `trial` / `active` / `not_activated` / `expired`), signup
  date, last update. Search box filters by email. Newest first,
  capped at 500 rows on screen.
- **Stats row:** total accounts, trial-only accounts,
  licensed accounts, trial credits used (5 − credits left,
  summed over accounts).
- **Manual license:** for a customer who pays outside Stripe
  (cash / bank transfer). Enter their email + plan → the panel
  creates a license with `create_license()` (no Stripe session)
  and shows the key BIG with a Copy button — send it to the
  customer. The key activates exactly like a Stripe license:
  the customer enters their email (or the key) in the app.
  No account needs to exist first; the license appears in the
  Licenses table either way.
- **Revoke:** a Revoke button per license (or
  `POST /api/admin/revoke-license {license_key}`) expires it
  immediately — `/api/activate` and `/api/status` then report
  `expired`.
- **Export:** "Download users CSV" (`GET /api/admin/export`)
  downloads all accounts + their license. **This CSV is the
  backup for manual licenses** — see the warning below.

**Manual-license warning:** a Stripe license survives a DB
wipe because Stripe is the source of truth. A **manual
license does NOT** — it lives only in the server DB. If the
Render free disk wipes, manual licenses are gone and must be
re-created (with a NEW key — keys cannot be re-issued with
the same value) and sent to the customers again. Export the
CSV regularly so you know who holds one.

## Ephemeral-disk warning (read this)

Render **free** disks are ephemeral: a redeploy/restart can wipe
`licenses.db`. Paid licenses survive because **Stripe is the
source of truth** — when a status/activate call finds no license
for an email, the server re-creates it from Stripe (needs
`STRIPE_SECRET_KEY`). Trial credits do NOT survive a wipe; that
is by design (they are a free trial). For a durable store, move
the DB to Render's persistent disk (paid) or an external
database later — the code only needs `DATA_DIR` pointed at it.

## Local run

```bash
pip install -r requirements.txt
python3 app.py            # PORT=5000, db licenses.db next to app.py
# tests (fresh scratch DB):
STRIPE_WEBHOOK_SECRET=whsec_test_123 \
STRIPE_PLANS_JSON='{"plans": [{"id": "3mo", "name": "Test", "price_aed": 299, "months": 3, "price_id": "price_test_3mo", "payment_link": null}]}' \
DATA_DIR=/tmp/lm_server_test PORT=8399 python3 app.py
# in another shell:
STRIPE_WEBHOOK_SECRET=whsec_test_123 python3 tests/test_api.py
# expect: 32 passed, 0 failed
# admin tests (start their own servers on scratch DBs):
python3 tests/test_admin.py
# expect: 25 passed, 0 failed
```

## Files

- `app.py` — the whole server (no other modules).
- `requirements.txt` — flask, stripe, gunicorn.
- `render.yaml` — Render Blueprint.
- `stripe_plans.json` — plans + Price IDs + payment links.
- `releases/app-version.json` — current client releases.
- `tests/test_api.py` — local endpoint tests (32 checks).
- `tests/test_admin.py` — admin panel tests (25 checks;
  starts its own servers on scratch DBs).
- `VERSION` — server version (1.1.0).
