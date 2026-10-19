"""Postgres state: recipes, freezer stock and reservations, planning rules, and the audit log.

The meal plan itself lives only in the calendar (homelab #2047 decision 4). Freezer
reservations are a table rather than derived from calendar events (operator decision
2026-10-16), so a past reservation stays eaten even if its event is deleted by hand in gcal.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import asyncpg

SCHEMA = """
CREATE TABLE IF NOT EXISTS recipes (
    id          serial PRIMARY KEY,
    name        text NOT NULL,
    name_key    text NOT NULL,
    tags        text[] NOT NULL DEFAULT '{}',
    source      text,
    body        text NOT NULL,
    created_by  text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  text NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS recipes_name_key ON recipes (name_key);

CREATE TABLE IF NOT EXISTS freezer_batches (
    id              serial PRIMARY KEY,
    label           integer NOT NULL,
    recipe_id       integer REFERENCES recipes (id),
    name            text NOT NULL,
    containers      integer NOT NULL CHECK (containers >= 0),
    made_on         date NOT NULL,
    added_by        text NOT NULL,
    note            text,
    source_event_id text,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS freezer_reservations (
    id          serial PRIMARY KEY,
    batch_id    integer NOT NULL REFERENCES freezer_batches (id),
    day         date NOT NULL,
    slot        text NOT NULL,
    event_id    text,
    reserved_by text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (day, slot)
);

CREATE TABLE IF NOT EXISTS planning_rules (
    id          serial PRIMARY KEY,
    text        text NOT NULL,
    created_by  text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  text NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS audit_log (
    id        bigserial PRIMARY KEY,
    at        timestamptz NOT NULL DEFAULT now(),
    user_sub  text NOT NULL,
    user_name text,
    tool      text NOT NULL,
    args      jsonb NOT NULL,
    before    jsonb,
    after     jsonb
);
CREATE INDEX IF NOT EXISTS audit_log_user_at ON audit_log (user_sub, at);
"""


def name_key(name: str) -> str:
    return " ".join(name.lower().split())


@dataclass
class Batch:
    id: int
    label: int
    recipe_id: int | None
    name: str
    containers: int
    made_on: date
    added_by: str
    note: str | None
    source_event_id: str | None
    eaten: int  # reservations dated before today
    reserved: int  # reservations dated today or later

    @property
    def remaining(self) -> int:
        return self.containers - self.eaten

    @property
    def free(self) -> int:
        return self.remaining - self.reserved

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "name": self.name,
            "recipe_id": self.recipe_id,
            "made_on": self.made_on.isoformat(),
            "containers": self.containers,
            "remaining": self.remaining,
            "reserved": self.reserved,
            "free": self.free,
            "note": self.note,
        }


_BATCH_SELECT = """
SELECT b.*,
       count(r.id) FILTER (WHERE r.day < $1)  AS eaten,
       count(r.id) FILTER (WHERE r.day >= $1) AS reserved
FROM freezer_batches b
LEFT JOIN freezer_reservations r ON r.batch_id = b.id
"""


def _batch(row: asyncpg.Record) -> Batch:
    return Batch(
        id=row["id"],
        label=row["label"],
        recipe_id=row["recipe_id"],
        name=row["name"],
        containers=row["containers"],
        made_on=row["made_on"],
        added_by=row["added_by"],
        note=row["note"],
        source_event_id=row["source_event_id"],
        eaten=row["eaten"],
        reserved=row["reserved"],
    )


class Tx:
    """One transaction. The service holds it open across the calendar write and rolls back
    if that write fails, so the tables never record a change the calendar refused."""

    def __init__(self, conn: asyncpg.Connection):
        self.c = conn

    # --- recipes -------------------------------------------------------------------------

    async def add_recipe(
        self, name: str, body: str, tags: list[str], source: str | None, user: str
    ) -> int:
        return await self.c.fetchval(
            "INSERT INTO recipes (name, name_key, tags, source, body, created_by, updated_by)"
            " VALUES ($1, $2, $3, $4, $5, $6, $6) RETURNING id",
            name, name_key(name), tags, source, body, user,
        )

    async def update_recipe(self, recipe_id: int, fields: dict[str, Any], user: str) -> None:
        if "name" in fields:
            fields["name_key"] = name_key(fields["name"])
        sets = [f"{k} = ${i}" for i, k in enumerate(fields, start=3)]
        await self.c.execute(
            f"UPDATE recipes SET {', '.join(sets)}, updated_by = $2, updated_at = now()"
            " WHERE id = $1",
            recipe_id, user, *fields.values(),
        )

    async def get_recipe(self, recipe_id: int) -> dict[str, Any] | None:
        row = await self.c.fetchrow("SELECT * FROM recipes WHERE id = $1", recipe_id)
        return _recipe(row) if row else None

    async def list_rules(self) -> list[dict[str, Any]]:
        rows = await self.c.fetch("SELECT id, text FROM planning_rules ORDER BY id")
        return [dict(r) for r in rows]

    async def get_rule(self, rule_id: int) -> dict[str, Any] | None:
        row = await self.c.fetchrow("SELECT id, text FROM planning_rules WHERE id = $1", rule_id)
        return dict(row) if row else None

    async def add_rule(self, text: str, user: str) -> int:
        return await self.c.fetchval(
            "INSERT INTO planning_rules (text, created_by, updated_by) VALUES ($1, $2, $2) RETURNING id",
            text, user,
        )

    async def update_rule(self, rule_id: int, text: str, user: str) -> None:
        await self.c.execute(
            "UPDATE planning_rules SET text = $2, updated_by = $3, updated_at = now() WHERE id = $1",
            rule_id, text, user,
        )

    async def remove_rule(self, rule_id: int) -> None:
        await self.c.execute("DELETE FROM planning_rules WHERE id = $1", rule_id)

    async def recipes_named(self, name: str) -> list[dict[str, Any]]:
        rows = await self.c.fetch("SELECT * FROM recipes WHERE name_key = $1", name_key(name))
        return [_recipe(r) for r in rows]

    async def search_recipes(self, query: str, tag: str | None, limit: int) -> list[dict[str, Any]]:
        rows = await self.c.fetch(
            "SELECT * FROM recipes"
            " WHERE ($1 = '' OR name ILIKE '%' || $1 || '%' OR body ILIKE '%' || $1 || '%'"
            "        OR EXISTS (SELECT 1 FROM unnest(tags) t WHERE t ILIKE $1))"
            "   AND ($2::text IS NULL OR $2 = ANY (tags))"
            " ORDER BY (name ILIKE '%' || $1 || '%') DESC, name LIMIT $3",
            query, tag, limit,
        )
        return [_recipe(r) for r in rows]

    # --- freezer -------------------------------------------------------------------------

    async def batches(self, today: date, *, active_only: bool = True) -> list[Batch]:
        rows = await self.c.fetch(
            _BATCH_SELECT + " GROUP BY b.id ORDER BY b.made_on, b.label", today
        )
        out = [_batch(r) for r in rows]
        return [b for b in out if b.remaining > 0] if active_only else out

    async def batch_by_label(self, label: int, today: date) -> Batch | None:
        for b in await self.batches(today):
            if b.label == label:
                return b
        return None

    async def batch_by_id(self, batch_id: int, today: date) -> Batch | None:
        row = await self.c.fetchrow(
            _BATCH_SELECT + " WHERE b.id = $2 GROUP BY b.id", today, batch_id
        )
        return _batch(row) if row else None

    async def add_batch(
        self,
        today: date,
        *,
        name: str,
        recipe_id: int | None,
        containers: int,
        made_on: date,
        user: str,
        note: str | None = None,
    ) -> Batch:
        # Serialise label allocation: two batches must never get the same free label.
        await self.c.execute("LOCK TABLE freezer_batches IN EXCLUSIVE MODE")
        held = {b.label for b in await self.batches(today)}
        label = next(n for n in range(1, len(held) + 2) if n not in held)
        batch_id = await self.c.fetchval(
            "INSERT INTO freezer_batches (label, recipe_id, name, containers, made_on, added_by, note)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id",
            label, recipe_id, name, containers, made_on, user, note,
        )
        batch = await self.batch_by_id(batch_id, today)
        assert batch
        return batch

    async def set_batch_event(self, batch_id: int, event_id: str) -> None:
        await self.c.execute(
            "UPDATE freezer_batches SET source_event_id = $2 WHERE id = $1", batch_id, event_id
        )

    async def batch_from_event(self, event_id: str, today: date) -> Batch | None:
        row = await self.c.fetchrow(
            _BATCH_SELECT + " WHERE b.source_event_id = $2 GROUP BY b.id", today, event_id
        )
        return _batch(row) if row else None

    async def delete_batch(self, batch_id: int) -> None:
        await self.c.execute("DELETE FROM freezer_batches WHERE id = $1", batch_id)

    async def set_containers(self, batch_id: int, containers: int) -> None:
        await self.c.execute(
            "UPDATE freezer_batches SET containers = $2 WHERE id = $1", batch_id, containers
        )

    async def reservation(self, day: date, slot: str) -> dict[str, Any] | None:
        row = await self.c.fetchrow(
            "SELECT r.*, b.label FROM freezer_reservations r"
            " JOIN freezer_batches b ON b.id = r.batch_id WHERE r.day = $1 AND r.slot = $2",
            day, slot,
        )
        return dict(row) if row else None

    async def reservations_between(self, start: date, end: date) -> list[dict[str, Any]]:
        rows = await self.c.fetch(
            "SELECT r.*, b.label, b.name FROM freezer_reservations r"
            " JOIN freezer_batches b ON b.id = r.batch_id"
            " WHERE r.day >= $1 AND r.day < $2 ORDER BY r.day, r.slot",
            start, end,
        )
        return [dict(r) for r in rows]

    async def reservations_for(self, batch_id: int) -> list[dict[str, Any]]:
        rows = await self.c.fetch(
            "SELECT day, slot FROM freezer_reservations WHERE batch_id = $1 ORDER BY day", batch_id
        )
        return [{"date": r["day"].isoformat(), "slot": r["slot"]} for r in rows]

    async def reserve(self, batch_id: int, day: date, slot: str, user: str) -> None:
        await self.c.execute(
            "INSERT INTO freezer_reservations (batch_id, day, slot, reserved_by)"
            " VALUES ($1, $2, $3, $4)",
            batch_id, day, slot, user,
        )

    async def set_reservation_event(self, day: date, slot: str, event_id: str) -> None:
        await self.c.execute(
            "UPDATE freezer_reservations SET event_id = $3 WHERE day = $1 AND slot = $2",
            day, slot, event_id,
        )

    async def release(self, day: date, slot: str) -> None:
        await self.c.execute(
            "DELETE FROM freezer_reservations WHERE day = $1 AND slot = $2", day, slot
        )

    # --- audit and limits ----------------------------------------------------------------

    async def audit(
        self,
        user: str,
        user_name: str | None,
        tool: str,
        args: dict[str, Any],
        before: Any,
        after: Any,
        at: datetime,
    ) -> None:
        await self.c.execute(
            "INSERT INTO audit_log (at, user_sub, user_name, tool, args, before, after)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7)",
            at, user, user_name, tool,
            json.dumps(args, default=str),
            json.dumps(before, default=str),
            json.dumps(after, default=str),
        )

    async def writes_since(self, user: str, since: datetime, tools: Sequence[str]) -> list[datetime]:
        rows = await self.c.fetch(
            "SELECT at FROM audit_log WHERE user_sub = $1 AND at > $2 AND tool = ANY($3::text[])"
            " ORDER BY at",
            user,
            since,
            list(tools),
        )
        return [r["at"] for r in rows]


def _recipe(row: asyncpg.Record) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "tags": list(row["tags"]),
        "source": row["source"],
        "text": row["body"],
        "created_by": row["created_by"],
        "updated_at": row["updated_at"].isoformat(),
    }


class Store:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    @classmethod
    async def connect(cls, url: str) -> Store:
        pool = await asyncpg.create_pool(url, min_size=1, max_size=4)
        assert pool is not None
        async with pool.acquire() as conn:
            await conn.execute(SCHEMA)
        return cls(pool)

    @asynccontextmanager
    async def tx(self) -> AsyncIterator[Tx]:
        async with self.pool.acquire() as conn, conn.transaction():
            yield Tx(conn)

    async def close(self) -> None:
        await self.pool.close()

