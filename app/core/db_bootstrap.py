"""Bring the database schema to head before the API accepts traffic.

Run as a module from the container entrypoint::

    python -m app.core.db_bootstrap

Replaces the shell one-liner ``alembic upgrade head || create_all`` that the
production image used to run. That line had two defects:

1. It could not succeed on the deployed database. Alembic was broken from the
   first commit (missing logging sections in alembic.ini), so every deploy
   fell through to ``Base.metadata.create_all`` and no environment ever got an
   ``alembic_version`` row. Once Alembic was repaired, ``upgrade head`` started
   running for real against a database whose tables already existed — the
   baseline revision's ``CREATE TABLE users`` raises ``DuplicateTable``, so the
   command still failed on every boot and still fell through to create_all.
   Migrations could therefore never be adopted, only worked around.

2. It could not tell a *schema* problem from an *unreachable database*. With
   the database down both halves fail identically, the fallback logs a wall of
   noise, and the real cause is buried.

This module resolves both. It waits for the database, adopts a create_all-built
database into Alembic exactly once by stamping the baseline, then upgrades to
head. It never raises: schema readiness is reported to the log and the process
exits 0 so uvicorn still starts, and the application lifespan makes the final
call on whether the instance is healthy enough to serve.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from sqlalchemy import inspect, text

from app.core.config import settings
from app.core.database import sync_engine
from app.core.logger import logger

# Revision that describes the schema `Base.metadata.create_all` produces. A
# database built by the old fallback is byte-for-byte this revision's output,
# so stamping it is truthful, not a shortcut.
_REPO_ROOT = Path(__file__).resolve().parents[2]

BASELINE_REVISION = "0001_baseline"

# Any table from the baseline is enough to tell "pre-existing schema" from
# "empty database"; `users` is the one nothing else can exist without.
SENTINEL_TABLE = "users"


def _wait_for_database(timeout: float, max_delay: float = 8.0) -> bool:
    """Block until the database accepts a connection, or `timeout` elapses."""
    deadline = time.monotonic() + max(timeout, 0.0)
    delay = 1.0
    attempt = 0

    while True:
        attempt += 1
        try:
            with sync_engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            if attempt > 1:
                logger.info("[bootstrap] database reachable after {} attempts", attempt)
            return True
        # Broad by design: any failure to connect means "not ready yet".
        except Exception as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.error(
                    "[bootstrap] database unreachable after {:.0f}s ({}: {})",
                    timeout, exc.__class__.__name__, exc,
                )
                return False
            wait = min(delay, max_delay, remaining)
            logger.warning(
                "[bootstrap] database not ready ({}) — retrying in {:.1f}s",
                exc.__class__.__name__, wait,
            )
            time.sleep(wait)
            delay = min(delay * 2, max_delay)


def _alembic_config():
    """Build an Alembic Config pointing at this repo's migrations.

    Running the migration in-process (rather than shelling out to the
    `alembic` console script) keeps one interpreter, one set of settings, and
    one log stream for the whole boot sequence.

    The URL is deliberately NOT set here. alembic/env.py resolves it from
    $DATABASE_URL, coercing `postgres://` and stripping async drivers — and
    `set_main_option` writes through ConfigParser, which applies pyformat
    interpolation, so a raw `%` in a generated database password would blow up
    on read. Leave the single owner of that value in env.py.
    """
    from alembic.config import Config

    # Absolute paths: the entrypoint's working directory is not guaranteed to
    # be the repo root, and `script_location = alembic` in alembic.ini is
    # relative to it.
    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
    return cfg


def _adopt_existing_schema() -> bool:
    """Stamp the baseline if tables exist but Alembic has no version row.

    Returns True if a stamp was applied. This is the one-time adoption step
    for databases built by the old create_all fallback; on an empty database
    or an already-stamped one it does nothing.
    """
    inspector = inspect(sync_engine)
    tables = set(inspector.get_table_names())

    if "alembic_version" in tables:
        return False
    if SENTINEL_TABLE not in tables:
        return False

    from alembic import command

    logger.info(
        "[bootstrap] pre-Alembic schema detected ({} tables, no alembic_version) "
        "— stamping {}", len(tables), BASELINE_REVISION,
    )
    command.stamp(_alembic_config(), BASELINE_REVISION)
    return True


def _upgrade_to_head() -> bool:
    """Run `alembic upgrade head`. Returns True on success."""
    from alembic import command

    try:
        command.upgrade(_alembic_config(), "head")
        logger.info("[bootstrap] alembic upgrade head complete")
        return True
    except Exception:
        logger.exception("[bootstrap] alembic upgrade head failed")
        return False


def _create_all_fallback() -> bool:
    """Last resort: build any missing tables straight from ORM metadata.

    `create_all` is checkfirst-by-default, so this is safe to run against a
    partially populated database — it adds what is missing and touches nothing
    that exists.
    """
    try:
        from app import models  # noqa: F401  - populate Base.metadata
        from app.models.base import Base

        Base.metadata.create_all(bind=sync_engine)
        logger.warning("[bootstrap] schema ensured via create_all fallback")
        return True
    except Exception:
        logger.exception("[bootstrap] create_all fallback failed")
        return False


def bootstrap() -> bool:
    """Ensure the schema is at head. Returns True if the schema is usable."""
    logger.info("[bootstrap] ensuring database schema (env={})", settings.environment)

    if not _wait_for_database(settings.database_startup_timeout):
        logger.error(
            "[bootstrap] skipping migrations — database did not answer. "
            "Check DATABASE_URL and that the instance is running; a suspended "
            "free-tier database never answers (see DEPLOY.md)."
        )
        return False

    try:
        _adopt_existing_schema()
    except Exception:
        # Adoption is an optimisation, not a requirement — an upgrade that
        # then fails on DuplicateTable still falls through to create_all.
        logger.exception("[bootstrap] baseline adoption failed; continuing")

    if _upgrade_to_head():
        return True
    return _create_all_fallback()


def main() -> int:
    # Exit 0 even on failure: the entrypoint must still start uvicorn so the
    # process can serve /health and report *why* it is unhealthy, rather than
    # dying silently before it can log anything useful.
    ok = bootstrap()
    if not ok:
        logger.error("[bootstrap] schema not verified — starting API anyway")
    return 0


if __name__ == "__main__":
    sys.exit(main())
