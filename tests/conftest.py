"""Fixtures: a real Postgres (TEST_DATABASE_URL), a fake calendar, and a settable clock.

Locally: `just test` starts a throwaway Postgres in podman. CI runs a postgres service.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import asyncpg
import pytest

from meals_mcp.config import Limits
from meals_mcp.service import MealsService, User
from meals_mcp.store import Store
from tests.fakes import FakeCalendar

TOM = User("sub-tom", "tom")
PARTNER = User("sub-partner", "partner")


class Clock:
    def __init__(self) -> None:
        # 2026-10-16 is a Friday; 11:00 UTC is 12:00 in Europe/London (BST).
        self.now = datetime(2026, 10, 16, 11, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


@pytest.fixture
async def store():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.fail("TEST_DATABASE_URL is not set; run `just test` or start a Postgres")
    conn = await asyncpg.connect(url)
    await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await conn.close()
    s = await Store.connect(url)
    yield s
    await s.close()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def calendar() -> FakeCalendar:
    return FakeCalendar()


@pytest.fixture
def limits() -> Limits:
    return Limits(tz=ZoneInfo("Europe/London"))


@pytest.fixture
def svc(calendar, store, limits, clock) -> MealsService:
    return MealsService(calendar, store, limits, clock=clock)
