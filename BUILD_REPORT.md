# License Server — Build Report (v1.0.0, 2026-10-28)

## What was built

Flask license server for the Listing Matcher client product
(Windows / macOS / Android), per `~/workspace/listing-matcher-client/SPEC.md`
§3, §5, §6, §7, §9.

- Repo (private): https://github.com/ikramasaadaan12786/listing-matcher-license-server
  — commit `c0b58f5`, branch `master`. Render-ready:
  `render.yaml` (Blueprint, free web service, Singapore,
  health check `/api/health`), `requirements.txt`
  (flask, stripe, gunicorn), `README.md`.
- Files: `app.py` (the whole server), `stripe_plans.json`
  (the 3 real plans with Price IDs + payment links),
  `releases/app-version.json` (0.0.0, no builds yet),
  `tests/test_api.py` (32 checks), `VERSION` (1.0.0).

## API implemented (SPEC §5 — all of it)

- `POST /api/signup` — email + machine_id → 5 bonus credits.
  One trial per machine (a machine that already trialed never
  gets 5 again, even with a new email); same email on a new
  machine returns the existing account (no new trial).
- `GET /api/status` — `{credits, license, licensed}`.
- `POST /api/consume` — deducts 1 trial credit ONLY when the
  caller has no valid license AND credits > 0. Licensed users
  are never charged (`{ok: true, licensed: true}`). 0 credits +
  no license → `{ok: false, error: "no_credits"}`.
- `POST /api/activate` — binds a license key to ONE machine on
  first activation. Same machine may re-activate. Different
  machine → `{ok: false, error: "already_bound_other_device"}`.
  Also supports email-only activation ("I already paid").
  Errors: `invalid_key`, `expired`.
- `POST /api/stripe-webhook` — Stripe signature-verified
  (`STRIPE_WEBHOOK_SECRET`). `checkout.session.completed` →
  creates a license key + plan (from Price ID) + expiry (3/6
  months; null for lifetime). Idempotent on the Stripe session
  id (replay → `duplicate: true`, no second license). Unknown
  Price ID → logs a warning, no license, no crash. No webhook
  secret configured → 503.
- `GET /success?session_id=` — shows the license key; if the
  webhook has not fired yet it pulls the session from Stripe
  (when `STRIPE_SECRET_KEY` is set) and creates the license.
- `GET /api/version`, `GET /api/plans` (serves
  `stripe_plans.json` / `STRIPE_PLANS_JSON` env / built-in
  defaults — never crashes if the plans file is absent),
  `GET /api/app-version` (reads `releases/app-version.json`;
  0.0.0 + null URLs until builds ship),
  `GET /releases/<file>`, `GET /api/health`, `GET /`.

## Stripe plans (SPEC §6 — already live)

The owner already created the products/payment links (verified
28 Oct via the Stripe connector). `stripe_plans.json` holds:

| Plan | AED | Price ID | Payment link |
|---|---|---|---|
| 3 Months | 299 | `price_1UVYvbCAFAZd2QaFmO4b1f0H` | https://buy.stripe.com/28E9AS35q6Zf9fz1M70kE05 |
| 6 Months | 499 | `price_1UVYwiCAFAZd2QaFjSJJJpp9` | https://buy.stripe.com/cNi9ASbBW1EVdvP9ez0kE06 |
| Lifetime | 999 | `price_1UVYxeCAFAZd2QaF5i3YvYW3` | https://buy.stripe.com/00waEW5dy3N3gI18av0kE07 |

## Local test results — 32/32 PASS

Run: `STRIPE_WEBHOOK_SECRET=whsec_test_123
STRIPE_PLANS_JSON='{"plans": [... price_test_3mo ...]}'
DATA_DIR=/tmp/lm_server_test PORT=8399 python3 app.py`
then `STRIPE_WEBHOOK_SECRET=whsec_test_123 python3 tests/test_api.py`.

Passed: health, version 1.0.0, plans served, repo plans file
has the 3 real plans, app-version 0.0.0, signup → 5 credits,
signup repeat (no new trial), same machine + new email (no
second trial), same email + new machine (no new trial),
invalid email → 400, consume #1–#5 (4→0), consume #6 →
`no_credits`, status 0 credits/not licensed, webhook bad
signature → 400, webhook unknown price → 200 no license no
crash, webhook valid (real HMAC-SHA256 Stripe-style signature)
→ license `LM-...` created, webhook replay → duplicate (no
second license), status → license `not_activated`, success
page shows the key, activate on machine A → ok (3mo, expiry
set), re-activate same machine → ok, same key on machine B →
REJECTED `already_bound_other_device`, invalid key →
`invalid_key`, expired license → `expired`, activate by email
only → ok, consume while licensed → ok/licensed/no deduction,
status licensed true + credits still 0, status on machine B →
licensed false.

Extra checks passed:
- No-plans-file path: server started with no
  `stripe_plans.json` and no env → `/api/plans` returns the 3
  built-in defaults (null Price IDs), no crash; webhook
  without secret → 503.
- Gunicorn boot (the Render start command):
  `gunicorn app:app --bind 0.0.0.0:8401` serves
  `/api/health` + `/api/version` correctly.
- No real secrets in the repo (only placeholder values and the
  fake `whsec_test` test secret). No real owner data anywhere.

Bugs found + fixed during testing:
- Stripe SDK v16 returns `Event`/`Session` objects, not
  dicts — `.get()` on them raises `AttributeError`. Fixed by
  converting with `.to_dict()` everywhere Stripe objects are
  read (webhook event, sessions, line items).

## Render deploy — NOT DONE (blocker, needs the parent)

The server is Render-ready (repo + Blueprint + README with
exact steps), but **it is not deployed yet**. Reason: there is
no Render API key in this environment (searched the workspace,
`~/.config`, env — nothing), and a subagent cannot use the
live browser to connect the Blueprint in the Render dashboard.
The previous App 27 deploys were done exactly that way (browser
dashboard), so this is the same step, not a new problem.

**To finish (parent agent, ~2 minutes, browser task):**
1. Render Dashboard → **New +** → **Blueprint** → connect repo
   `ikramasaadaan12786/listing-matcher-license-server` → Apply.
2. Service → **Environment** → add:
   - `STRIPE_SECRET_KEY` — the owner's `rk_live_...`
     restricted key (he already has it from App 26; he sets it
     himself in the dashboard — never in chat).
   - `STRIPE_WEBHOOK_SECRET` — from the Stripe Dashboard
     webhook endpoint (step 3); the owner sets it himself.
3. Stripe Dashboard → Developers → Webhooks → **Add endpoint**:
   `https://<service>.onrender.com/api/stripe-webhook`, event
   `checkout.session.completed`. Copy the `whsec_...` secret to
   Render (step 2) and redeploy.
4. Verify: `curl <service>/api/health`,
   `curl <service>/api/plans`, `curl <service>/api/version`.
   Then write the live URL into `server/URL.txt`.

Alternative: create a Render API key (Dashboard → Account
Settings → API Keys) and hand it to a subagent to finish the
deploy via the Render API — the dashboard route above is
simpler.

## What I could NOT verify (honest gaps)

- **Live Render deploy + live endpoint tests** — blocked as
  above (no API key, no browser). All endpoint tests above
  are local; the code path is identical (same repo, same
  start command), but live verification is still pending.
- **Real Stripe webhook with the live `whsec_...` secret** —
  tested with a real HMAC-SHA256 Stripe-style signature and a
  test secret, which exercises the same verification code, but
  a live Stripe event (real payment link purchase) has not
  been run through it yet. Do a live test purchase after
  deploy (the owner can refund it) before giving the links to
  clients.
- **Restore-from-Stripe path with a real `STRIPE_SECRET_KEY`** —
  implemented (Customer lookup by email + recent-session
  fallback, idempotent on session id) but untestable without
  real Stripe credentials. It is a fallback only; the primary
  webhook path is fully tested.
- **`/api/app-version` with real installers** — returns 0.0.0
  until the desktop/APK builds ship into `releases/` and
  `app-version.json` is updated (the other subagents' job).

## Notes for the desktop / APK subagents

- Base URL: the live Render URL (in `server/URL.txt` once
  deployed). Until then, use the local server or wait.
- `GET /api/plans` is the source of the payment links — do not
  hardcode them in the apps.
- Signup is email + machine_id; machine_id = first MAC, or a
  UUID stored in the app's user-data dir (Android: ANDROID_ID).
- Call `/api/consume` ONLY for a chargeable match; never for a
  zero result. Licensed users are never charged by the server,
  but the app must still not call consume when licensed (the
  server returns `{ok: true, licensed: true}` without deducting
  if it is called).
- Trial credits live only in the server DB (Render free is
  ephemeral — a wipe resets trial credits; paid licenses are
  restored from Stripe).
