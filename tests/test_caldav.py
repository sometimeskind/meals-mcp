from datetime import UTC, datetime

import httpx
import pytest
import respx

from meals_mcp.caldav import BusyError, StalwartBusy, parse_freebusy

BASE = "http://stalwart:8080"
HOME = f"{BASE}/dav/cal/tom@prins.id/"


def _multistatus(*collections: tuple[str, bool]) -> str:
    rows = "".join(
        f"<D:response><D:href>{href}</D:href><D:propstat><D:prop><D:resourcetype><D:collection/>"
        f"{'<C:calendar/>' if is_cal else '<C:schedule-inbox/>'}</D:resourcetype></D:prop>"
        "<D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>"
        for href, is_cal in collections
    )
    home = ("<D:response><D:href>/dav/cal/tom@prins.id/</D:href><D:propstat><D:prop><D:resourcetype>"
            "<D:collection/></D:resourcetype></D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat>"
            "</D:response>")
    return ('<?xml version="1.0" encoding="utf-8"?><D:multistatus xmlns:D="DAV:" '
            f'xmlns:C="urn:ietf:params:xml:ns:caldav">{home}{rows}</D:multistatus>')


def _vfreebusy(*lines: str) -> str:
    return "\r\n".join(["BEGIN:VCALENDAR", "BEGIN:VFREEBUSY", *lines, "END:VFREEBUSY", "END:VCALENDAR", ""])


def utc(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


def test_parse_freebusy_periods_durations_folding_and_types():
    ics = _vfreebusy(
        "DTSTART:20261016T000000Z",
        "FREEBUSY;FBTYPE=BUSY:20261017T170000Z/20261017T183000Z,20261018T090000Z/PT1H30M",
        "FREEBUSY:20261019T080000Z/2026",
        " 1019T090000Z",  # folded continuation
        "FREEBUSY;FBTYPE=BUSY-TENTATIVE:20261020T120000Z/P1D",
        "FREEBUSY;FBTYPE=FREE:20261021T120000Z/20261021T130000Z",
    )
    assert parse_freebusy(ics) == [
        (utc(17, 17), utc(17, 18, 30)),
        (utc(18, 9), utc(18, 10, 30)),
        (utc(19, 8), utc(19, 9)),
        (utc(20, 12), utc(21, 12)),
    ]


@respx.mock
async def test_busy_queries_every_calendar_but_meals_and_skips_unreadable_ones():
    respx.request("PROPFIND", HOME).mock(return_value=httpx.Response(207, text=_multistatus(
        ("/dav/cal/tom@prins.id/default/", True),
        ("/dav/cal/tom@prins.id/family/", True),
        ("/dav/cal/tom@prins.id/meals/", True),
        ("/dav/cal/tom@prins.id/inbox/", False),
        ("/dav/cal/tom@prins.id/broken/", True),
    )))
    default = respx.request("REPORT", f"{HOME}default/").mock(return_value=httpx.Response(
        200, text=_vfreebusy("FREEBUSY:20261017T170000Z/20261017T180000Z")))
    respx.request("REPORT", f"{HOME}family/").mock(return_value=httpx.Response(
        200, text=_vfreebusy("FREEBUSY:20261017T173000Z/20261017T190000Z")))
    respx.request("REPORT", f"{HOME}broken/").mock(return_value=httpx.Response(403))
    meals = respx.request("REPORT", f"{HOME}meals/")
    got = await StalwartBusy(BASE, "tom@prins.id", "pw").busy(utc(16, 0), utc(23, 0))
    assert sorted(got) == [(utc(17, 17), utc(17, 18)), (utc(17, 17, 30), utc(17, 19))]
    assert not meals.called
    req = default.calls[0].request
    assert req.headers["Depth"] == "1"
    assert b'start="20261016T000000Z" end="20261023T000000Z"' in req.read()
    assert req.headers["Authorization"].startswith("Basic ")


@respx.mock
async def test_a_refused_listing_is_a_busy_error():
    respx.request("PROPFIND", HOME).mock(return_value=httpx.Response(401))
    with pytest.raises(BusyError, match="HTTP 401"):
        await StalwartBusy(BASE, "tom@prins.id", "pw").busy(utc(16, 0), utc(23, 0))
