"""
Application configuration.

Single source of truth for runtime values. All settings load from environment
variables with explicit defaults; nothing is read via os.getenv() anywhere else
in the codebase (audit finding 1.7).

Usage:
    from app.core.config import settings
    settings.database_url      # never settings.DATABASE_URL — case insensitive
    settings.jwt_secret_key    # the *only* place this value should be read

Phase 2.0 changes from prior version:
- Added `allowed_hosts` (replaces the hardcoded "yourdomain.com" placeholder in
  main.py — audit 3.10).
- Added `signal_update_interval` (fixes the 3600s default sleep — audit 3.5).
- Added `cors_allow_credentials`, `cors_allow_methods`, `cors_allow_headers`.
- Removed the `validate_model` field validator that raised on missing .h5
  (audit 5.11) — predictor.py already handles missing files gracefully.
- Refresh-token TTL renamed from `jwt_refresh_hours` to `jwt_refresh_days`
  for consistency with how it's expressed in product docs.
"""

from __future__ import annotations

from functools import lru_cache
from typing import List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # -------------------------------------------------------------------------
    # Application identity
    # -------------------------------------------------------------------------
    app_name:     str = "Algorithmic Trading Simulator"
    app_version:  str = "0.2.0"
    environment:  str = "development"           # development | test | staging | production
    debug:        bool = False

    # -------------------------------------------------------------------------
    # Server
    # -------------------------------------------------------------------------
    host:    str = "0.0.0.0"
    port:    int = 8000
    workers: int = 1

    # Trusted host middleware. Comma-separated env var.
    # Empty list => middleware skipped (development).
    allowed_hosts: List[str] = Field(
        default_factory=lambda: ["localhost", "127.0.0.1", "api"]
    )

    # -------------------------------------------------------------------------
    # CORS
    # -------------------------------------------------------------------------
    cors_origins: List[str] = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://localhost:3000",
            "http://127.0.0.1:5173",
        ]
    )
    cors_allow_credentials: bool = True
    cors_allow_methods:     List[str] = Field(default_factory=lambda: ["*"])
    cors_allow_headers:     List[str] = Field(default_factory=lambda: ["*"])

    # -------------------------------------------------------------------------
    # Database
    # -------------------------------------------------------------------------
    database_url:           str = "sqlite:///./hft.db"
    database_pool_size:     int = 10
    database_max_overflow:  int = 20
    database_pool_pre_ping: bool = True
    database_echo:          bool = False
    # Grace window (seconds) that startup will spend waiting for the database
    # to answer before giving up. A managed Postgres that is restarting, being
    # resized, or waking from idle can take tens of seconds to accept
    # connections; without a wait the very first probe fails and the platform
    # marks the whole service failed, even though the database returns
    # moments later. 0 disables the wait (single probe, previous behaviour).
    database_startup_timeout: float = 90.0

    # -------------------------------------------------------------------------
    # Redis
    # -------------------------------------------------------------------------
    redis_url:       Optional[str] = "redis://localhost:6379/0"
    redis_cache_ttl: int = 300                  # seconds
    # When true (or when a real Redis is unreachable in non-production), the
    # app uses an in-process fakeredis client so it runs with zero external
    # infra. Never honored in production (see redis_client.init_redis).
    use_fake_redis:  bool = False

    # -------------------------------------------------------------------------
    # User defaults
    # -------------------------------------------------------------------------
    default_user_balance: float = 100_000.0
    max_users:            int = 10_000

    # -------------------------------------------------------------------------
    # Auth / Security
    # -------------------------------------------------------------------------
    # NOTE (audit 3.1): jwt_handler.py MUST consume this value. A hardcoded
    # SECRET_KEY in jwt_handler.py bypassed this setting in the original code.
    # The Phase 2.1 auth refactor closes that hole.
    jwt_secret_key:        str = "change-me-in-production"
    jwt_algorithm:         str = "HS256"
    jwt_access_ttl_minutes: int = 15            # short-lived access tokens
    jwt_refresh_days:      int = 7              # long-lived refresh tokens
    jwt_ws_ttl_seconds:    int = 60             # ephemeral WS handshake tokens
    jwt_audience:          str = "hft-platform"

    password_min_length:      int = 8
    password_require_special: bool = False

    # -------------------------------------------------------------------------
    # Market data
    # -------------------------------------------------------------------------
    market_data_refresh_interval: int = 2       # seconds between price polls
    market_data_cache_ttl:        int = 5       # seconds for per-(symbol,interval) cache
    max_symbols:                  int = 50
    # Simulator default: serve a deterministic 24/7 synthetic market so prices
    # never flicker or depend on exchange hours / provider rate limits. Set to
    # False to prefer the live yfinance provider (with synthetic fallback).
    use_synthetic_market:         bool = True
    # Cadence for periodic account-equity snapshots (drives the equity curve).
    equity_snapshot_interval:     int = 15

    # External provider keys (optional — synthetic provider used when absent)
    alpha_vantage_api_key: Optional[str] = None
    polygon_api_key:       Optional[str] = None

    # -------------------------------------------------------------------------
    # WebSocket / stream
    # -------------------------------------------------------------------------
    websocket_enabled:             bool = True
    websocket_broadcast_interval:  float = 1.0   # legacy — Phase 2.5 collapses
    websocket_max_connections:     int = 1000
    websocket_heartbeat_interval:  int = 30      # server -> client ping period
    websocket_heartbeat_timeout:   int = 10      # missed-pong tolerance
    websocket_max_message_bytes:   int = 64_000

    # -------------------------------------------------------------------------
    # Trading
    # -------------------------------------------------------------------------
    trading_enabled:        bool  = True
    max_position_size:      float = 10_000.0
    max_daily_loss:         float = 5_000.0
    max_concurrent_trades:  int   = 50
    concentration_limit:    float = 0.30         # max 30% in one symbol

    # -------------------------------------------------------------------------
    # Risk
    # -------------------------------------------------------------------------
    risk_check_interval:      int   = 30
    stop_loss_percentage:     float = 0.05
    take_profit_percentage:   float = 0.10

    # -------------------------------------------------------------------------
    # Signals / ML
    # -------------------------------------------------------------------------
    # FIX (audit 3.5): the original config did not define this key, so
    # signal_engine.py fell back to `model_update_interval` (3600s = 1 hour).
    # The signal background loop now defaults to 10s.
    signal_update_interval: int = 10
    model_update_interval:  int = 3600          # ML retrain cadence (Phase 4)

    model_path:                       str   = "app/ml/models/lstm_model.h5"
    model_device:                     str   = "cpu"
    model_load_on_startup:             bool  = False
    model_max_batch_size:             int   = 64
    model_timeout_seconds:            int   = 5
    prediction_confidence_threshold:  float = 0.7

    # -------------------------------------------------------------------------
    # Logging
    # -------------------------------------------------------------------------
    log_level:     str = "INFO"
    log_file:      str = "logs/app.log"
    log_rotation:  str = "10 MB"
    log_retention: str = "7 days"
    log_json:      bool = False                  # set true in production

    # -------------------------------------------------------------------------
    # Rate limiting / API guardrails
    # -------------------------------------------------------------------------
    api_rate_limit:          int = 100           # default rps per IP
    auth_rate_limit:         int = 5             # auth attempts per minute per IP
    request_timeout_seconds: int = 10

    # -------------------------------------------------------------------------
    # Pydantic Settings config
    # -------------------------------------------------------------------------
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        # Required because we use `model_*` field names alongside Pydantic's
        # built-in `model_` protected namespace.
        protected_namespaces=(),
    )

    # -------------------------------------------------------------------------
    # Validators
    # -------------------------------------------------------------------------
    @field_validator("environment")
    @classmethod
    def _validate_environment(cls, v: str) -> str:
        allowed = {"development", "test", "staging", "production"}
        v_lower = v.lower()
        if v_lower not in allowed:
            raise ValueError(f"environment must be one of {allowed}, got {v!r}")
        return v_lower

    @field_validator("jwt_secret_key")
    @classmethod
    def _validate_secret(cls, v: str, info) -> str:
        env = (info.data.get("environment") or "development").lower()
        if env == "production" and ("change-me" in v.lower() or len(v) < 32):
            raise ValueError(
                "jwt_secret_key must be set to a strong value (>=32 chars) "
                "in production."
            )
        return v

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        v_upper = v.upper()
        if v_upper not in allowed:
            raise ValueError(f"log_level must be one of {allowed}")
        return v_upper

    @field_validator("database_url")
    @classmethod
    def _normalize_database_url(cls, v: str) -> str:
        # Managed providers (Render, Heroku, …) often hand out the legacy
        # `postgres://` scheme, which SQLAlchemy 2.0 no longer recognizes.
        # Coerce it to the canonical `postgresql://` so both the sync engine
        # and `database_url_async` work without manual edits.
        if v.startswith("postgres://"):
            return v.replace("postgres://", "postgresql://", 1)
        return v

    # NOTE (audit 5.11): the original config had a `validate_model` field
    # validator that raised if `model_path` didn't exist on disk. That made
    # the whole app fail to import without an .h5 file, even when
    # `model_load_on_startup=False`. predictor.py already handles missing
    # files gracefully, so the validator has been removed.

    # -------------------------------------------------------------------------
    # Convenience properties
    # -------------------------------------------------------------------------
    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def is_development(self) -> bool:
        return self.environment == "development"

    @property
    def is_test(self) -> bool:
        return self.environment == "test"

    @property
    def database_url_async(self) -> str:
        """
        Return the database URL coerced to an async driver.
        SQLAlchemy 2.0 async needs `postgresql+asyncpg://` or `sqlite+aiosqlite://`.
        """
        url = self.database_url
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+asyncpg://", 1)
        if url.startswith("sqlite:///"):
            return url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
        return url


@lru_cache
def get_settings() -> Settings:
    """Singleton settings accessor (cached after first call)."""
    return Settings()


# Module-level convenience: `from app.core.config import settings`
settings = get_settings()
