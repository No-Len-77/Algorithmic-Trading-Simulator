"""
FastAPI application entrypoint — HFT Trading Platform.

Responsibilities:
- Construct the FastAPI app with middleware (CORS, trusted host, rate limit).
- Configure global exception handlers.
- Manage the application lifespan: DB connectivity check, engine supervisor
  start/stop (the supervisor pattern arrives in Phase 2.5; until then we keep
  the legacy `asyncio.create_task` shape but flagged with TODOs).
- Mount the API router.

Audit fixes in this revision (Phase 2.0):
- 3.9  CORS reads `settings.cors_origins` (was hardcoded).
- 3.10 Trusted host reads `settings.allowed_hosts` (was placeholder).
- 12.2 Emoji-theater docstrings replaced with substantive ones.
- 12.7 Emoji-prefixed log lines replaced with bracketed tags.

Still outstanding (deferred to later phases, marked TODO):
- 3.8  `safe_task` restart-storm pattern → Phase 2.5 supervisor.
- 4.2  Three duplicate broadcasters → Phase 2.5 event bus.
"""

from __future__ import annotations

import asyncio
import inspect
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Awaitable, Callable, List

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# Built frontend (Vite output). Present in the production image; absent in dev
# (where the Vite server serves the SPA). Drives same-origin SPA hosting below.
_FRONTEND_DIST = Path(__file__).resolve().parent / "frontend" / "dist"

from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.api.routes import router as api_router
from app.api.v1 import router as api_v1_router
from app.core.config import settings
from app.core.database import (
    check_database_connection,
    create_tables,
    dispose_engines,
    wait_for_database,
)
from app.core.logger import logger
from app.infra.redis_client import close_redis, init_redis

# Engines — TODO Phase 2.5: replace direct imports with EngineSupervisor.register()
from app.market.candle_engine import start_candle_engine
from app.market.market_data_engine import start_market_data_engine
from app.portfolio.equity_snapshot_engine import start_equity_snapshot_engine
from app.quant.signal_engine import start_signal_engine
from app.websocket.market_stream import start_market_stream


# -----------------------------------------------------------------------------
# Rate limiter
# -----------------------------------------------------------------------------
from app.core.rate_limit import limiter


# -----------------------------------------------------------------------------
# Background task wrapper
# -----------------------------------------------------------------------------
async def _supervise(name: str, coro_fn: Callable[[], Awaitable[Any]]) -> None:
    """
    Run a background engine coroutine forever with bounded backoff on crash.

    TODO (Phase 2.5): replace with app.infra.supervisor.EngineSupervisor.
    The current implementation is a stopgap — see audit finding 3.8.
    """
    backoff = 1.0
    max_backoff = 30.0

    while True:
        try:
            logger.info("[engine] starting %s", name)
            result = coro_fn()
            if inspect.isawaitable(result):
                await result
            else:
                # Defensive: if the engine returned a non-awaitable, treat as
                # one-shot and exit cleanly rather than busy-looping.
                logger.warning(
                    "[engine] %s returned a non-awaitable; treating as one-shot",
                    name,
                )
                return

            # Coroutine completed without exception. Don't restart eagerly.
            logger.info("[engine] %s completed", name)
            return

        except asyncio.CancelledError:
            logger.info("[engine] %s cancelled", name)
            raise

        except Exception:
            logger.exception("[engine] %s crashed; restarting in %.1fs", name, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)


# -----------------------------------------------------------------------------
# Application lifespan
# -----------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("[startup] HFT platform starting (env=%s)", settings.environment)

    # ---------------------------------------------------------------------
    # Test environment short-circuit
    # ---------------------------------------------------------------------
    # In ENVIRONMENT=test (CI + local pytest), the conftest provides its
    # own in-memory SQLite and overrides DB dependencies. We skip the
    # production startup probes (real DB + Redis ping + background engines)
    # because those rely on infra the tests don't intend to exercise.
    if settings.is_test:
        logger.info("[startup] test environment — skipping infra probes and engines")
        app.state.engine_tasks = []
        try:
            yield
        finally:
            logger.info("[shutdown] complete (test)")
        return

    # ---------------------------------------------------------------------
    # Database
    # Phase 2.2: `Base.metadata.create_all` is no longer called at startup
    # (audit 6.2). Alembic migrations are the source of truth — run
    # `alembic upgrade head` before deploy.
    # ---------------------------------------------------------------------
    # Wait, with backoff, rather than probing once. A managed database that is
    # restarting or waking from idle refuses connections for tens of seconds;
    # a single probe turns that into a dead service the platform never retries.
    # Once the grace window is spent we still fail fast — an instance that
    # cannot reach its database must not take traffic.
    if not await wait_for_database():
        raise RuntimeError(
            "Database connectivity check failed after "
            f"{settings.database_startup_timeout:.0f}s — check DATABASE_URL and "
            "that the database instance is running (a suspended free-tier "
            "instance never answers; see DEPLOY.md)"
        )
    logger.info("[startup] database ready")
    if settings.is_development and settings.database_url.startswith("sqlite"):
        # Dev convenience: bootstrap schema for the local SQLite file
        # without forcing the developer to run alembic.
        await asyncio.to_thread(create_tables)
        logger.info("[startup] dev SQLite schema ensured")

    # ---------------------------------------------------------------------
    # Redis — required by auth_service (refresh-token store) and by
    # Phase 2.4+ for idempotency, Phase 2.5+ for pub/sub.
    # ---------------------------------------------------------------------
    # init_redis() degrades to an in-memory fakeredis client in non-production
    # when a real Redis is unreachable, so this only returns False in
    # production — where a missing token store is a genuine outage.
    if not await init_redis():
        raise RuntimeError("Redis connectivity check failed (production)")
    logger.info("[startup] redis ready")

    # ---------------------------------------------------------------------
    # Background engines (Phase 2.5 moves these to a structured supervisor)
    # ---------------------------------------------------------------------
    tasks: List[asyncio.Task] = [
        asyncio.create_task(_supervise("market_feed",  start_market_data_engine)),
        asyncio.create_task(_supervise("candle",       start_candle_engine)),
        asyncio.create_task(_supervise("signal",       start_signal_engine)),
        asyncio.create_task(_supervise("market_stream", start_market_stream)),
        asyncio.create_task(_supervise("equity_snapshot", start_equity_snapshot_engine)),
    ]
    app.state.engine_tasks = tasks
    logger.info("[startup] %d background engines started", len(tasks))

    try:
        yield
    finally:
        logger.info("[shutdown] cancelling background engines")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await close_redis()
        await dispose_engines()
        logger.info("[shutdown] complete")


# -----------------------------------------------------------------------------
# FastAPI app
# -----------------------------------------------------------------------------
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Quantitative trading platform simulator. See ARCHITECTURE.md.",
    debug=settings.debug,
    lifespan=lifespan,
)

# Rate limiter wiring
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# CORS — audit fix 3.9: reads from settings instead of hardcoded list.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=settings.cors_allow_credentials,
    allow_methods=settings.cors_allow_methods,
    allow_headers=settings.cors_allow_headers,
)

# Trusted host — audit fix 3.10: reads from settings.allowed_hosts.
# Skipped when the list is empty (dev convenience).
if settings.is_production and settings.allowed_hosts:
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=settings.allowed_hosts,
    )


# -----------------------------------------------------------------------------
# Global exception handlers
# -----------------------------------------------------------------------------
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    logger.warning("[validation] %s %s — %s", request.method, request.url.path, exc.errors())
    return JSONResponse(
        status_code=422,
        content={"detail": exc.errors(), "message": "Validation error"},
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    logger.warning("[http] %s %s -> %d %s", request.method, request.url.path,
                   exc.status_code, exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("[unhandled] %s %s — %r", request.method, request.url.path, exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


# -----------------------------------------------------------------------------
# Routes
#
# v1 routers (modular per-domain) take precedence — they MUST be mounted
# before the legacy aggregate router, since FastAPI resolves overlapping
# paths in registration order.
# -----------------------------------------------------------------------------
app.include_router(api_v1_router, prefix="/api/v1")
app.include_router(api_router,    prefix="/api/v1")


@app.get("/")
@limiter.limit("60/minute")
async def root(request: Request):
    # In production the built SPA is served from the same origin as the API.
    if _FRONTEND_DIST.is_dir():
        return FileResponse(_FRONTEND_DIST / "index.html", headers={"Cache-Control": "no-cache"})
    return {
        "name":    settings.app_name,
        "version": settings.app_version,
        "env":     settings.environment,
        "docs":    "/docs",
        "health":  "/health",
    }


@app.get("/health")
@limiter.limit("30/minute")
async def health(request: Request):
    """Readiness: is this instance able to serve traffic? Touches the database.

    This is the endpoint a load balancer or platform health check should use
    (Render's `healthCheckPath`), because an instance that cannot reach its
    database should be taken out of rotation.
    """
    db_ok = await check_database_connection()
    return {
        "status":      "healthy" if db_ok else "unhealthy",
        "database":    "connected" if db_ok else "disconnected",
        "environment": settings.environment,
        "version":     settings.app_version,
    }


@app.get("/health/live")
async def liveness():
    """Liveness: is this process alive? No I/O, no rate limit.

    Deliberately separate from /health. High-frequency probes — the container
    HEALTHCHECK, nginx upstream checks, a CDN origin check — must not each cost
    a database round-trip, and must not restart a healthy API process just
    because the database blipped. Liveness answers "should I be restarted?";
    readiness answers "should I get traffic?". Conflating them means a transient
    database fault kills the very process that would have recovered from it.
    """
    return {"status": "alive", "version": settings.app_version}


# -----------------------------------------------------------------------------
# Single-page app (production)
#
# Serve the built Vite frontend from the same origin as the API so the auth
# refresh cookie, the WebSocket and CORS all work without cross-origin setup.
# Only active when the build output exists (the production image). In local dev
# the Vite server owns the SPA and this block is skipped.
#
# Registered LAST so /api/*, /health, /docs and /openapi.json always win; the
# catch-all only handles client-side routes (e.g. deep links to /quant).
# -----------------------------------------------------------------------------
if _FRONTEND_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=_FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
        # Unknown API routes return a JSON 404 (never the SPA shell), so API
        # consumers get a proper error instead of an HTML page.
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found")
        candidate = (_FRONTEND_DIST / full_path).resolve()
        root_dir = _FRONTEND_DIST.resolve()
        if candidate.is_file() and (candidate == root_dir or root_dir in candidate.parents):
            return FileResponse(candidate)
        return FileResponse(_FRONTEND_DIST / "index.html", headers={"Cache-Control": "no-cache"})


# -----------------------------------------------------------------------------
# Entrypoint
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        log_level=settings.log_level.lower(),
    )
