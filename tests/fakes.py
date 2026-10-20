"""An in-memory Calendar with Google's etag semantics."""

from __future__ import annotations

import itertools
from dataclasses import replace
from datetime import date, datetime

from meals_mcp.gcal import Conflict, Event


class FakeCalendar:
    def __init__(self) -> None:
        self.events: dict[str, Event] = {}
        self._ids = itertools.count(1)
        self._etags = itertools.count(1)
        self.fail_next: Exception | None = None

    def _maybe_fail(self) -> None:
        if self.fail_next:
            exc, self.fail_next = self.fail_next, None
            raise exc

    def hand_made(self, day: date, summary: str, description: str = "") -> Event:
        """An event a person typed in gcal: no private properties."""
        ev = Event(day=day, summary=summary, description=description, id=f"h{next(self._ids)}",
                   etag=f'"{next(self._etags)}"')
        self.events[ev.id] = ev
        return ev

    def edit_in_gcal(self, event_id: str, **changes: object) -> None:
        self.events[event_id] = replace(self.events[event_id], etag=f'"{next(self._etags)}"', **changes)

    async def list_events(self, start: date, end: date) -> list[Event]:
        self._maybe_fail()
        return [replace(e) for e in self.events.values() if start <= e.day < end]

    async def get(self, event_id: str) -> Event | None:
        ev = self.events.get(event_id)
        return replace(ev) if ev else None

    async def insert(self, event: Event) -> Event:
        self._maybe_fail()
        ev = replace(event, id=f"e{next(self._ids)}", etag=f'"{next(self._etags)}"')
        self.events[ev.id] = ev
        return replace(ev)

    async def update(self, event: Event) -> Event:
        self._maybe_fail()
        cur = self.events.get(event.id or "")
        if cur is None or cur.etag != event.etag:
            raise Conflict("the event changed since it was read")
        ev = replace(event, etag=f'"{next(self._etags)}"')
        self.events[ev.id] = ev
        return replace(ev)

    async def delete(self, event: Event) -> None:
        self._maybe_fail()
        cur = self.events.get(event.id or "")
        if cur is None:
            return
        if cur.etag != event.etag:
            raise Conflict("the event changed since it was read")
        del self.events[event.id]


class FakeBusy:
    """Busy intervals as Stalwart would return them, or an error."""

    def __init__(self) -> None:
        self.blocks: list[tuple[datetime, datetime]] = []
        self.error: Exception | None = None

    async def busy(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        if self.error:
            raise self.error
        return [(s, e) for s, e in self.blocks if s < end and e > start]
