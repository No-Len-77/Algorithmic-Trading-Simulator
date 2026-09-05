"""
Database engines + session factories — sync + async, side by side.

Phase 2.2:
- `Base` lives in `app/models/base.py` now (audit 5.1). Re-exported here for
  backward compatibility while the rest of the codebase migrates imports.
- Async engine + AsyncSession (SQLAlchemy 2.0) is the canonical path going
  forward. New service-layer code uses `get_async_db`.
- Sync engine + Session is kept *only* for the legacy `app/api/routes.py`
  router. Deleted in Phase 2.3 when routes split fully into `app/api/v1/`.
- `create_tables()` is no longer called from production startup (audit 6.2).
  Migration runs `alembic upgrade head`. The helper remains for the test
  conftest which uses in-memory SQLite.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.core.logger import logger
from app.models.base import Base  # noqa: F401  re-exported for back-compat

# ---------------------------------------------------------------------------
# Sync engine (legacy path)
# ---------------------------------------------------------------------------
_sync_url = settings.database_url
sync_engine = create_engine(
    _sync_url,
    future=True,
    pool_pre_ping=settings.database_pool_pre_ping,
    pool_size=settings.database_pool_size if not _sync_url.startswith("sqlite") else 5,
    max_overflow=settings.database_max_overflow if not _sync_url.startswith("sqlite") else 0,
    echo=settings.database_echo,
)
SessionLocal = sessionmaker(
    bind=sync_engine,
    autoflush=False,
    autocommit=False,
    future=True,
)
# Back-compat aliases (some legacy code imported `engine`).
engine = sync_engine


# ---------------------------------------------------------------------------
# Async engine (canonical)
# ---------------------------------------------------------------------------
_async_url = settings.database_url_async
async_engine = create_async_engine(
    _async_url,
    future=True,
    pool_pre_ping=settings.database_pool_pre_ping,
    pool_size=settings.database_pool_size if "sqlite" not in _async_url else 5,
    max_overflow=settings.database_max_overflow if "sqlite" not in _async_url else 0,
    echo=settings.database_echo,
)
AsyncSessionLocal = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------
def get_db() -> Iterator[Session]:
    """Sync session — used by legacy `app/api/routes.py` only."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


async def get_async_db() -> AsyncIterator[AsyncSession]:
    """Async session — canonical path. New v1 routers depend on this."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def async_session_context() -> AsyncIterator[AsyncSession]:
    """Non-dep variant for scripts and engines."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


# ---------------------------------------------------------------------------
# Lifecycle helpers
# ---------------------------------------------------------------------------
def init_database() -> None:
    """Sync connectivity probe (single attempt, raises on failure).

    Startup uses `wait_for_database` instead, which retries; this stays for
    scripts and admin tooling that want an immediate pass/fail.
    """
    try:
        with sync_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        logger.info("[db] sync engine ready url=%s", _redacted(_sync_url))
    except Exception:
        logger.exception("[db] sync engine probe failed")
        raise


async def check_database_connection() -> bool:
    """Async connectivity probe used by /health and main.py."""
    try:
        async with async_engine.begin() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        logger.exception("[db] async ping failed")
        return False


async def wait_for_database(
    timeout: float | None = None,
    max_delay: float = 8.0,
) -> bool:
    """Poll the database until it accepts a connection, or `timeout` elapses.

    Returns True as soon as a ``SELECT 1`` succeeds, False if the whole grace
    window passes without one.

    Why this exists: a single probe at startup conflates "the database is
    gone" with "the database has not finished waking up". A managed Postgres
    that is restarting, being resized, or cold-starting after idle refuses
    connections for tens of seconds, and a process that dies on the first
    refusal never sees it come back — the platform reports a failed service
    for an outage that healed on its own. Retrying with backoff distinguishes
    the two, while still failing fast (see main.py) once the window is spent.

    Unlike `check_database_connection`, intermediate failures are logged at
    warning level without a traceback: a refused connection during boot is
    expected, not exceptional. The final failure is logged with the reason.
    """
    if timeout is None:
        timeout = settings.database_startup_timeout

    deadline = time.monotonic() + max(timeout, 0.0)
    delay = 1.0
    attempt = 0
    last_error: Exception | None = None

    while True:
        attempt += 1
        try:
            async with async_engine.begin() as conn:
                await conn.execute(text("SELECT 1"))
            if attempt > 1:
                logger.info("[db] reachable after {} attempt(s)", attempt)
            return True
        # Broad by design: during boot, any failure to connect — refused,
        # DNS not resolving yet, auth not provisioned — means "not ready yet".
        except Exception as exc:
            last_error = exc
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            wait = min(delay, max_delay, remaining)
            logger.warning(
                "[db] not ready (attempt {}): {} — retrying in {:.1f}s",
                attempt, exc.__class__.__name__, wait,
            )
            await asyncio.sleep(wait)
            delay = min(delay * 2, max_delay)

    logger.error(
        "[db] unreachable after {:.0f}s / {} attempt(s): {}",
        timeout, attempt, last_error,
    )
    return False


def create_tables() -> None:
    """
    Create all tables from ORM metadata.

    NOT called from production startup anymore (audit 6.2). Use Alembic.
    Kept here for the test conftest (in-memory SQLite).
    """
    # Ensure all model modules are imported so metadata is fully populated.
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=sync_engine)


async def dispose_engines() -> None:
    """Cleanly tear down both engines on application shutdown."""
    try:
        await async_engine.dispose()
    except Exception:
        logger.exception("[db] async dispose error")
    try:
        sync_engine.dispose()
    except Exception:
        logger.exception("[db] sync dispose error")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _redacted(url: str) -> str:
    """Mask credentials in a SQLAlchemy URL for log output."""
    if "@" not in url:
        return url
    scheme, rest = url.split("://", 1)
    creds, host = rest.split("@", 1)
    if ":" in creds:
        user, _ = creds.split(":", 1)
        return f"{scheme}://{user}:***@{host}"
    return url
