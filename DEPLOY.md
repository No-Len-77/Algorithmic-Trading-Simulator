# Deployment Guide — Render (public demo)

This deploys the whole platform as **one same-origin service** (the FastAPI app
serves the built React SPA, the REST API, and the WebSocket on a single domain),
backed by a managed **PostgreSQL** and a managed **Redis (Key Value)**. Same
origin means the httpOnly refresh cookie, the WebSocket, and CORS all work with
zero cross-origin configuration.

```
                         ┌──────────────────────────────────────┐
   browser  ── https ──▶ │  algorithmic-trading-simulator (web) │
   (SPA + API + WS,      │  Docker: Dockerfile.web              │
    one origin)          │   • Vite SPA  (/, /quant, …)         │
                         │   • REST API  (/api/v1/*)            │
                         │   • WebSocket (/api/v1/ws)           │
                         └───────────────┬───────────┬──────────┘
                                         │           │
                              ┌──────────▼──┐   ┌────▼────────┐
                              │ hft-postgres│   │ hft-redis   │
                              │ PostgreSQL  │   │ Key Value   │
                              └─────────────┘   └─────────────┘
```

---

## Option A — One-click Blueprint (recommended)

The repo ships [`render.yaml`](./render.yaml), which provisions all three
resources at once.

1. Push to GitHub (the blueprint reads from your default branch).
2. Go to **[dashboard.render.com](https://dashboard.render.com)** → **New +** →
   **Blueprint**.
3. Connect the repository **`Akshatx-io/Algorithmic-Trading-Simulator`** and
   select the branch. Render detects `render.yaml`.
4. Review the plan (3 free resources: `hft-postgres`, `hft-redis`, `algorithmic-trading-simulator`) and
   click **Apply**.
5. Wait for the first build (~4–7 min: it builds the SPA, installs Python deps,
   then runs `alembic upgrade head` on boot). When `algorithmic-trading-simulator` is **Live**,
   open its URL: `https://algorithmic-trading-simulator.onrender.com`.

That's it — click **"Explore the live demo"** for a one-click populated account, or register your own, and explore the Quant Lab.

> **Render deploy button** (add to README once the repo is public):
> `https://render.com/deploy?repo=https://github.com/Akshatx-io/Algorithmic-Trading-Simulator`

---

## Option B — Manual (dashboard, no blueprint)

Create the three resources by hand, in this order, all in the **same region**:

1. **PostgreSQL** — New → Postgres → name `hft-postgres`, plan **Free** → Create. Copy
   the **Internal Database URL**.
2. **Key Value (Redis)** — New → Key Value → name `hft-redis`, plan **Free**,
   *Maxmemory policy* `allkeys-lru` → Create. Copy the **Internal URL**.
3. **Web Service** — New → Web Service → connect the repo →
   - **Runtime:** Docker
   - **Dockerfile path:** `./Dockerfile.web`
   - **Health check path:** `/health`
   - **Plan:** Free
   - **Environment variables:** see the table below.

---

## Environment variables

| Key | Value | Notes |
|---|---|---|
| `ENVIRONMENT` | `production` | Enables prod guards (real Redis, strong JWT). |
| `JWT_SECRET_KEY` | *(generate)* | Must be **≥ 32 chars**. Blueprint uses `generateValue`. Manually: `openssl rand -hex 32`. |
| `DATABASE_URL` | *from `hft-postgres`* | The app coerces `postgres://` → `postgresql://` automatically. |
| `REDIS_URL` | *from `hft-redis`* | Internal `redis://…` URL. |
| `ALLOWED_HOSTS` | `["*.onrender.com", "localhost", "127.0.0.1"]` | **JSON array** (the app JSON-parses list envs — *not* comma-separated). The `*.onrender.com` wildcard accepts the service subdomain (even if Render suffixes it); `localhost`/`127.0.0.1` keep the health probes passing. |
| `USE_SYNTHETIC_MARKET` | `true` | Deterministic 24/7 market; no external data deps. |
| `USE_FAKE_REDIS` | `false` | Use the real managed Redis. |
| `LOG_LEVEL` | `INFO` | |

> ⚠️ **List-type env vars are JSON.** `ALLOWED_HOSTS` and `CORS_ORIGINS` are
> parsed as JSON arrays. Use `["https://example.com"]`, never
> `https://example.com,https://other.com`.

---

## Post-deploy verification

```bash
# 1. Health (DB connectivity)
curl https://algorithmic-trading-simulator.onrender.com/health
# -> {"status":"healthy","database":"connected","environment":"production",...}

# 2. API docs
open https://algorithmic-trading-simulator.onrender.com/docs

# 3. A quant endpoint (auth-free analytics)
curl "https://algorithmic-trading-simulator.onrender.com/api/v1/vol/surface?s=100&base_vol=0.22"
# -> {"status":"success", ...}
```

Then in the browser: click **"Explore the live demo"** (one-click, pre-seeded), or **register → log in → place a trade → open Quant Lab**.
The WebSocket connects to `wss://<host>/api/v1/ws` automatically (derived from
the page origin).

---

## Test the production image locally (optional)

```bash
docker build -f Dockerfile.web -t hft-web .
docker run --rm -p 8000:8000 \
  -e ENVIRONMENT=production \
  -e JWT_SECRET_KEY="$(openssl rand -hex 32)" \
  -e DATABASE_URL="sqlite:////tmp/hft.db" \
  -e USE_FAKE_REDIS=true \
  -e ALLOWED_HOSTS='["*"]' \
  hft-web
# open http://localhost:8000  (SPA + API + WS, one origin)
```

*(SQLite + fakeredis here just to smoke-test the image without external infra;
production uses managed Postgres + Redis.)*

---

## Notes & gotchas

- **Free tier sleeps.** A free Render web service spins down after ~15 min idle;
  the first request then cold-starts (~30–60 s). Fine for a demo.
- **Free Postgres expires.** Render's free Postgres is time-limited (~30 days),
  after which the dashboard shows **"Suspended by Render"**. A suspended free
  instance cannot be resumed — see
  [Recovering from a suspended database](#recovering-from-a-suspended-database).
- **Single worker by design.** The container starts one uvicorn worker (the
  start command is inlined in `Dockerfile.web`) — the background
  market/candle/signal engines live in the FastAPI lifespan, so multiple
  workers would duplicate them. Scale vertically, not by workers.
- **Migrations run on boot.** The `Dockerfile.web` start command runs
  `python -m app.core.db_bootstrap`, which waits for the database, adopts a
  pre-Alembic schema by stamping `0001_baseline` once (databases built by the
  old `create_all` fallback have no `alembic_version` row, so `upgrade head`
  would otherwise fail on `DuplicateTable` every boot), then runs
  `alembic upgrade head` — still falling back to `Base.metadata.create_all` if
  that fails. A valid schema is always present, with no manual step.
- **Startup waits for the database.** Both the bootstrap and the app lifespan
  retry with backoff for `DATABASE_STARTUP_TIMEOUT` seconds (default `90`)
  before giving up, so a database that is restarting or waking from idle no
  longer turns into a permanently failed service. Once the window is spent the
  app still exits — an instance that cannot reach its database must not serve.
- **Host allow-listing is already on.** The blueprint sets
  `ALLOWED_HOSTS=["*.onrender.com", "localhost", "127.0.0.1"]`, which accepts
  your service subdomain (even if Render appends a suffix) plus the health
  probes, while rejecting arbitrary `Host` headers. Narrow it to your exact
  host if you prefer.
- **One-click guest demo.** Visitors can hit **"Explore the live demo"** on the
  login page (`POST /api/v1/auth/demo`) to land in a freshly-seeded, populated
  account — no signup. The demo account is sandboxed and reset on each use.

---

## Recovering from a suspended database

Symptom, in the Render dashboard:

| Service | Status |
| --- | --- |
| `hft-postgres` | **Suspended by Render** |
| `algorithmic-trading-simulator` | **Failed service** |

The web service is failed *because of* the database, not independently: the
free Postgres passed its 30-day limit and stopped accepting connections, and
the app refuses to boot without one (by design — an instance that cannot reach
its database must not take traffic). Logs show
`Database connectivity check failed`.

A free instance in this state cannot be resumed or restored; Render's own
recovery path is to replace it. Any data in it is gone — for this demo that is
only user accounts and simulated trades, which the schema bootstrap and the
guest-demo seeder recreate.

1. **Export anything you need first.** Dashboard → `hft-postgres` → *Backups*
   (the free plan offers no automated backups, so there may be nothing there).
   Skip this step if the demo data is disposable — it normally is.
2. **Delete the suspended instance.** Dashboard → `hft-postgres` → *Settings* →
   *Delete Database*. Render allows only one free Postgres per workspace, so
   the replacement cannot be created until this one is gone.
3. **Re-sync the blueprint.** Dashboard → *Blueprints* → your blueprint →
   **Sync**. `render.yaml` recreates `hft-postgres` and rewires the web
   service's `DATABASE_URL` to it automatically.

   > If you provisioned manually (Option B), instead create a new Postgres,
   > copy its **Internal Database URL**, and set it as `DATABASE_URL` on the
   > web service.

4. **Redeploy the web service** (*Manual Deploy → Deploy latest commit*) if the
   sync did not trigger one. On boot, `db_bootstrap` finds an empty database
   and runs `alembic upgrade head` to build the schema from scratch.
5. **Verify:**

   ```bash
   curl https://algorithmic-trading-simulator.onrender.com/health
   # -> {"status":"healthy","database":"connected",...}
   ```

**Keep the name in sync.** The blueprint's database name must match the
instance in the dashboard. `render.yaml` declared `hft-db` while the live
instance was `hft-postgres`; every sync therefore tried to create a *second*
free Postgres, which Render refuses, so the web service was never rewired.
Both now read `hft-postgres`.

---

## Other platforms

The same `Dockerfile.web` runs anywhere that hosts a Docker container with
`$PORT` injected (Fly.io, Railway, Cloud Run). Provide `DATABASE_URL`,
`REDIS_URL`, `JWT_SECRET_KEY`, and `ENVIRONMENT=production`; point the platform
at `Dockerfile.web` and a `/health` check.
