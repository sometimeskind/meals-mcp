"""Settings, read from MEALS_* environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or value == "":
        raise RuntimeError(f"{name} is required")
    return value


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


@dataclass(frozen=True)
class Limits:
    """The guard rails every write goes through."""

    tz: ZoneInfo
    window_past_days: int = 7
    window_future_days: int = 28
    writes_per_hour: int = 30  # calendar writes
    store_writes_per_hour: int = 200  # recipe and freezer writes
    freezer_old_days: int = 90
    title_max: int = 60

    @classmethod
    def from_env(cls) -> Limits:
        return cls(
            # No default: "today" and "tomorrow" must be the household's, not the pod's UTC.
            tz=ZoneInfo(_env("MEALS_TZ")),
            window_past_days=_int("MEALS_WINDOW_PAST_DAYS", 7),
            window_future_days=_int("MEALS_WINDOW_FUTURE_DAYS", 28),
            writes_per_hour=_int("MEALS_WRITES_PER_HOUR", 30),
            store_writes_per_hour=_int("MEALS_STORE_WRITES_PER_HOUR", 200),
            freezer_old_days=_int("MEALS_FREEZER_OLD_DAYS", 90),
            title_max=_int("MEALS_TITLE_MAX", 60),
        )


@dataclass(frozen=True)
class ServerSettings:
    database_url: str
    google_credentials: str
    base_url: str
    oidc_config_url: str
    oidc_client_id: str
    oidc_client_secret: str
    jwt_signing_key: str
    storage_key: str
    required_group: str
    allowed_redirects: list[str] = field(default_factory=list)
    # "" binds every address of both families. The cluster is dual-stack and the gateway may
    # pick the IPv6 endpoint; uvicorn makes "::" IPv6-only.
    host: str = ""
    port: int = 8000
    metrics_port: int = 9090
    # Busy times from Stalwart: off unless the URL is set
    stalwart_url: str | None = None
    stalwart_user: str | None = None
    stalwart_password: str | None = None
    busy_exclude: frozenset[str] = frozenset({"meals"})

    @classmethod
    def from_env(cls) -> ServerSettings:
        return cls(
            database_url=_env("MEALS_DATABASE_URL"),
            google_credentials=_env("MEALS_GOOGLE_CREDENTIALS", "/secrets/google/credentials.json"),
            base_url=_env("MEALS_BASE_URL"),
            oidc_config_url=_env("MEALS_OIDC_CONFIG_URL"),
            oidc_client_id=_env("MEALS_OIDC_CLIENT_ID"),
            oidc_client_secret=_env("MEALS_OIDC_CLIENT_SECRET"),
            jwt_signing_key=_env("MEALS_JWT_SIGNING_KEY"),
            storage_key=_env("MEALS_STORAGE_KEY"),
            required_group=_env("MEALS_REQUIRED_GROUP", "meals"),
            allowed_redirects=[
                u.strip()
                for u in _env(
                    "MEALS_ALLOWED_REDIRECTS", "https://claude.ai/api/mcp/auth_callback"
                ).split(",")
                if u.strip()
            ],
            port=_int("MEALS_PORT", 8000),
            metrics_port=_int("MEALS_METRICS_PORT", 9090),
            stalwart_url=os.environ.get("MEALS_STALWART_URL") or None,
            stalwart_user=os.environ.get("MEALS_STALWART_USER") or None,
            stalwart_password=os.environ.get("MEALS_STALWART_PASSWORD") or None,
            busy_exclude=frozenset(
                c.strip() for c in os.environ.get("MEALS_BUSY_EXCLUDE", "meals").split(",") if c.strip()
            ),
        )
