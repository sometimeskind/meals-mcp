"""The Meals calendar: a minimal Google Calendar v3 client for all-day events.

The app's token has only the calendar.app.created scope, so it can reach the Meals calendar it
created and nothing else. Updates and deletes send the event's etag (If-Match), so an edit made
in Google Calendar since the last read surfaces as a Conflict instead of being overwritten.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from meals_mcp import metrics

API = "https://www.googleapis.com/calendar/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/calendar.app.created"


class CalendarError(Exception):
    pass


class Conflict(CalendarError):
    """The event changed (or vanished) since it was read."""


class GoogleAuthError(CalendarError):
    """The refresh token was rejected: revoked, expired, or the client was deleted."""


@dataclass
class Event:
    """An all-day event. `raw` is the full Google resource, kept so updates are full PUTs."""

    day: date
    summary: str
    description: str = ""
    private: dict[str, str] = field(default_factory=dict)
    id: str | None = None
    etag: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_resource(self) -> dict[str, Any]:
        body = dict(self.raw)
        body["summary"] = self.summary
        body["description"] = self.description
        body["start"] = {"date": self.day.isoformat()}
        body["end"] = {"date": (self.day + timedelta(days=1)).isoformat()}
        props = dict(body.get("extendedProperties") or {})
        props["private"] = dict(self.private)
        body["extendedProperties"] = props
        return body

    @classmethod
    def from_resource(cls, res: dict[str, Any]) -> Event | None:
        start = res.get("start") or {}
        if "date" not in start:
            return None  # timed events are not meals
        return cls(
            day=date.fromisoformat(start["date"]),
            summary=res.get("summary", ""),
            description=res.get("description", ""),
            private=dict((res.get("extendedProperties") or {}).get("private") or {}),
            id=res.get("id"),
            etag=res.get("etag"),
            raw=res,
        )


class Calendar(Protocol):
    async def list_events(self, start: date, end: date) -> list[Event]:
        """All-day events with start.date in [start, end)."""
        ...

    async def get(self, event_id: str) -> Event | None: ...

    async def insert(self, event: Event) -> Event: ...

    async def update(self, event: Event) -> Event: ...

    async def delete(self, event: Event) -> None: ...


@dataclass
class GoogleCredentials:
    client_id: str
    client_secret: str
    refresh_token: str
    calendar_id: str

    @classmethod
    def load(cls, path: str) -> GoogleCredentials:
        with open(path) as f:
            data = json.load(f)
        return cls(**{k: data[k] for k in ("client_id", "client_secret", "refresh_token", "calendar_id")})


class GoogleCalendar:
    def __init__(self, creds: GoogleCredentials, client: httpx.AsyncClient | None = None):
        self.creds = creds
        self.http = client or httpx.AsyncClient(timeout=20)
        self._token: str | None = None
        self._expires_at = 0.0
        self._base = f"{API}/calendars/{quote(creds.calendar_id, safe='')}/events"

    async def _access_token(self) -> str:
        if self._token and time.monotonic() < self._expires_at - 60:
            return self._token
        resp = await self.http.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.creds.refresh_token,
                "client_id": self.creds.client_id,
                "client_secret": self.creds.client_secret,
            },
        )
        if resp.status_code in (400, 401):
            metrics.GOOGLE_AUTH_FAILURES.inc()
            raise GoogleAuthError(
                "Google rejected the meals-mcp refresh token "
                f"({resp.json().get('error', resp.status_code)}); the operator must re-run bootstrap"
            )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._expires_at = time.monotonic() + int(data.get("expires_in", 3600))
        return self._token

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {await self._access_token()}"
        resp = await self.http.request(method, url, headers=headers, **kwargs)
        if resp.status_code == 401:
            metrics.GOOGLE_AUTH_FAILURES.inc()
            self._token = None
            raise GoogleAuthError("Google rejected the access token")
        if resp.status_code == 412:
            raise Conflict("the event changed since it was read")
        return resp

    async def list_events(self, start: date, end: date) -> list[Event]:
        # A day either side, then filter on start.date: all-day events are tz-less dates.
        params: dict[str, Any] = {
            "timeMin": f"{start - timedelta(days=1)}T00:00:00Z",
            "timeMax": f"{end + timedelta(days=1)}T00:00:00Z",
            "singleEvents": "true",
            "maxResults": 2500,
        }
        events: list[Event] = []
        while True:
            resp = await self._request("GET", self._base, params=params)
            resp.raise_for_status()
            data = resp.json()
            for item in data.get("items", []):
                ev = Event.from_resource(item)
                if ev and start <= ev.day < end and item.get("status") != "cancelled":
                    events.append(ev)
            if not data.get("nextPageToken"):
                return events
            params["pageToken"] = data["nextPageToken"]

    async def get(self, event_id: str) -> Event | None:
        resp = await self._request("GET", f"{self._base}/{quote(event_id, safe='')}")
        if resp.status_code in (404, 410):
            return None
        resp.raise_for_status()
        if resp.json().get("status") == "cancelled":
            return None
        return Event.from_resource(resp.json())

    async def insert(self, event: Event) -> Event:
        resp = await self._request("POST", self._base, json=event.to_resource())
        resp.raise_for_status()
        ev = Event.from_resource(resp.json())
        assert ev is not None
        return ev

    async def update(self, event: Event) -> Event:
        assert event.id and event.etag
        resp = await self._request(
            "PUT",
            f"{self._base}/{quote(event.id, safe='')}",
            json=event.to_resource(),
            headers={"If-Match": event.etag},
        )
        if resp.status_code in (404, 410):
            raise Conflict("the event was deleted since it was read")
        resp.raise_for_status()
        ev = Event.from_resource(resp.json())
        assert ev is not None
        return ev

    async def delete(self, event: Event) -> None:
        assert event.id and event.etag
        resp = await self._request(
            "DELETE", f"{self._base}/{quote(event.id, safe='')}", headers={"If-Match": event.etag}
        )
        if resp.status_code in (404, 410):
            return  # already gone: the goal is met
        resp.raise_for_status()
