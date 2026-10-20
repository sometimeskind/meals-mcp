"""Household busy times from Stalwart: a minimal CalDAV free/busy client (homelab #2047).

Stalwart mirrors the household's Google calendars, so busy times are read there rather than
from Google: no extra Google scope, and Stalwart stays the only thing that talks to Google. The
credential is an app password whose allow-list is discovery and free/busy only (homelab's
test-stalwart-credentials.sh proves it is refused event reads, writes and mail). A
free-busy-query REPORT returns VFREEBUSY periods: start and end, nothing about the events.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import quote, urljoin

import httpx
from defusedxml import ElementTree as ET

log = logging.getLogger(__name__)

DAV = "{DAV:}"
CAL = "{urn:ietf:params:xml:ns:caldav}"
PROPFIND = (
    '<?xml version="1.0" encoding="utf-8" ?>'
    '<D:propfind xmlns:D="DAV:"><D:prop><D:resourcetype/></D:prop></D:propfind>'
)
# Busy types that count; FREE does not. A FREEBUSY line without FBTYPE is BUSY (RFC 5545).
BUSY_TYPES = {"BUSY", "BUSY-UNAVAILABLE", "BUSY-TENTATIVE"}
_DURATION_RE = re.compile(r"^([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


class BusyError(Exception):
    """Busy times could not be read at all (one unreadable calendar is skipped instead)."""


class BusySource(Protocol):
    async def busy(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        """Busy intervals overlapping [start, end), from every calendar in scope, unmerged."""
        ...


def _utc(value: str) -> datetime:
    return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)


def _duration(value: str) -> timedelta:
    m = _DURATION_RE.match(value)
    if not m:
        raise ValueError(f"bad duration {value!r}")
    sign, w, d, h, mi, s = m.groups()
    delta = timedelta(weeks=int(w or 0), days=int(d or 0), hours=int(h or 0), minutes=int(mi or 0),
                      seconds=int(s or 0))
    return -delta if sign == "-" else delta


def parse_freebusy(ics: str) -> list[tuple[datetime, datetime]]:
    """The busy periods of a VFREEBUSY reply. Periods are UTC start/end or start/duration."""
    unfolded = re.sub(r"\r?\n[ \t]", "", ics)
    out = []
    for line in unfolded.splitlines():
        name, _, value = line.partition(":")
        params = name.split(";")
        if params[0].upper() != "FREEBUSY":
            continue
        fbtype = next((p.split("=", 1)[1].upper() for p in params[1:] if p.upper().startswith("FBTYPE=")), "BUSY")
        if fbtype not in BUSY_TYPES:
            continue
        for period in value.strip().split(","):
            start_s, _, end_s = period.partition("/")
            start = _utc(start_s)
            end = start + _duration(end_s) if end_s.upper().startswith(("P", "+", "-")) else _utc(end_s)
            out.append((start, end))
    return out


class StalwartBusy:
    def __init__(
        self,
        base_url: str,
        user: str,
        password: str,
        exclude: frozenset[str] = frozenset({"meals"}),
        client: httpx.AsyncClient | None = None,
    ):
        self.base = base_url.rstrip("/")
        self.home = f"{self.base}/dav/cal/{quote(user, safe='@')}/"
        self.exclude = exclude
        self.http = client or httpx.AsyncClient(timeout=20, auth=(user, password))

    async def calendars(self) -> list[str]:
        """The account's calendar collections (hrefs), less the excluded ones (Meals itself:
        its all-day meals would read as busy all day)."""
        try:
            resp = await self.http.request(
                "PROPFIND", self.home, content=PROPFIND,
                headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
            )
        except httpx.HTTPError as e:
            raise BusyError(f"Stalwart unreachable: {e}") from e
        if resp.status_code != 207:
            raise BusyError(f"listing calendars in Stalwart failed (HTTP {resp.status_code})")
        hrefs = []
        for r in ET.fromstring(resp.content).iter(f"{DAV}response"):
            href = r.findtext(f"{DAV}href") or ""
            if r.find(f".//{DAV}resourcetype/{CAL}calendar") is None:
                continue  # the home itself, and the scheduling inbox and outbox
            name = href.rstrip("/").rsplit("/", 1)[-1]
            if name not in self.exclude:
                hrefs.append(urljoin(self.home, href))
        return hrefs

    async def busy(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        span = f'start="{start.astimezone(UTC):%Y%m%dT%H%M%SZ}" end="{end.astimezone(UTC):%Y%m%dT%H%M%SZ}"'
        body = (
            '<?xml version="1.0" encoding="utf-8" ?>'
            f'<C:free-busy-query xmlns:C="urn:ietf:params:xml:ns:caldav"><C:time-range {span}/>'
            "</C:free-busy-query>"
        )
        out: list[tuple[datetime, datetime]] = []
        for href in await self.calendars():
            try:
                resp = await self.http.request(
                    "REPORT", href, content=body,
                    headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
                )
            except httpx.HTTPError as e:
                raise BusyError(f"Stalwart unreachable: {e}") from e
            if resp.status_code != 200:
                log.warning("no free/busy for %s (HTTP %s); leaving it out", href, resp.status_code)
                continue
            out += parse_freebusy(resp.text)
        return out
