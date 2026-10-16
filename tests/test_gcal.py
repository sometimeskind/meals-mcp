from datetime import date

import httpx
import pytest
import respx

from meals_mcp.gcal import API, TOKEN_URL, Conflict, Event, GoogleAuthError, GoogleCalendar, GoogleCredentials

CREDS = GoogleCredentials("cid", "csecret", "refresh", "abc@group.calendar.google.com")
BASE = f"{API}/calendars/abc%40group.calendar.google.com/events"


def token_ok():
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "at", "expires_in": 3600}))


@respx.mock
async def test_list_keeps_all_day_events_in_range_only():
    token_ok()
    items = [
        {"id": "1", "etag": '"1"', "summary": "Dinner: a", "start": {"date": "2026-10-17"}},
        {"id": "2", "etag": '"2"', "summary": "timed", "start": {"dateTime": "2026-10-17T12:00:00Z"}},
        {"id": "3", "etag": '"3"', "summary": "Dinner: b", "start": {"date": "2026-10-16"}},
        {"id": "4", "etag": '"4"', "summary": "gone", "start": {"date": "2026-10-17"}, "status": "cancelled"},
    ]
    route = respx.get(BASE).mock(return_value=httpx.Response(200, json={"items": items}))
    events = await GoogleCalendar(CREDS).list_events(date(2026, 10, 17), date(2026, 10, 18))
    assert [e.id for e in events] == ["1"]
    assert route.calls[0].request.headers["Authorization"] == "Bearer at"


@respx.mock
async def test_update_sends_if_match_and_maps_412():
    token_ok()
    route = respx.put(f"{BASE}/e1").mock(return_value=httpx.Response(412))
    ev = Event(day=date(2026, 10, 17), summary="Dinner: x", id="e1", etag='"7"', raw={"id": "e1", "colorId": "5"})
    with pytest.raises(Conflict):
        await GoogleCalendar(CREDS).update(ev)
    req = route.calls[0].request
    assert req.headers["If-Match"] == '"7"'
    body = req.read().decode()
    assert '"colorId":"5"' in body.replace(" ", "")  # full PUT keeps fields set in gcal
    assert '"end":{"date":"2026-10-18"}' in body.replace(" ", "")


@respx.mock
async def test_revoked_refresh_token():
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(GoogleAuthError, match="invalid_grant"):
        await GoogleCalendar(CREDS).list_events(date(2026, 10, 17), date(2026, 10, 18))


@respx.mock
async def test_delete_of_a_gone_event_is_fine():
    token_ok()
    respx.delete(f"{BASE}/e1").mock(return_value=httpx.Response(410))
    await GoogleCalendar(CREDS).delete(Event(day=date(2026, 10, 17), summary="x", id="e1", etag='"1"'))
