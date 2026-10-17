"""Phase 0, run once on the operator's workstation (homelab #2047).

`bootstrap` runs the installed-app consent flow with only the calendar.app.created scope,
creates the Meals calendar, proves the token cannot read the primary calendar, and writes the
credentials JSON the pod mounts (client id/secret, refresh token, calendar id). Meals must be
created here: a calendar made by hand in gcal is invisible to this token.

`check` re-tests the token's reach later, including the open question of whether it can edit
an event a person created by hand on Meals (make one in gcal first).
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import threading
import webbrowser
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from meals_mcp.gcal import API, SCOPE, TOKEN_URL

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"


def _fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def _consent(client_id: str, client_secret: str) -> str:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    got: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            got.update(q)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"meals-mcp bootstrap: you can close this tab.")

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{server.server_port}/"
    url = AUTH_URL + "?" + urlencode({
        "client_id": client_id,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    print(f"Opening the consent page. If no browser opens, visit:\n{url}\n")
    webbrowser.open(url)
    thread = threading.Thread(target=server.handle_request)
    thread.start()
    thread.join()
    server.server_close()
    if got.get("state") != state:
        _fail("state mismatch on the consent callback")
    if "code" not in got:
        _fail(f"consent refused: {got.get('error', 'no code returned')}")
    resp = httpx.post(TOKEN_URL, data={
        "grant_type": "authorization_code",
        "code": got["code"],
        "redirect_uri": redirect,
        "client_id": client_id,
        "client_secret": client_secret,
        "code_verifier": verifier,
    })
    resp.raise_for_status()
    tokens = resp.json()
    if tokens.get("scope", "").split() != [SCOPE]:
        _fail(f"granted scopes are {tokens.get('scope')!r}, expected only {SCOPE}")
    if "refresh_token" not in tokens:
        _fail("no refresh token returned (revoke the app's access in the Google account and retry)")
    return tokens["refresh_token"]


def _access(creds: dict[str, str]) -> httpx.Client:
    resp = httpx.post(TOKEN_URL, data={
        "grant_type": "refresh_token",
        "refresh_token": creds["refresh_token"],
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
    })
    resp.raise_for_status()
    return httpx.Client(headers={"Authorization": f"Bearer {resp.json()['access_token']}"}, timeout=20)


def _assert_no_primary(http: httpx.Client) -> None:
    resp = http.get(f"{API}/calendars/primary/events", params={"maxResults": 1})
    if resp.status_code not in (403, 404):
        _fail(f"the token can read the primary calendar (HTTP {resp.status_code}); do not use it")
    print(f"ok: primary calendar is unreadable (HTTP {resp.status_code})")


def bootstrap(client_secret_path: str, out: str, timezone: str, calendar_id: str | None = None) -> None:
    if os.path.exists(out):
        _fail(f"{out} exists; move it away first (a second Meals calendar would be created)")
    with open(client_secret_path) as f:
        client = json.load(f)
    client = client.get("installed") or client.get("web") or client
    creds = {"client_id": client["client_id"], "client_secret": client["client_secret"]}
    creds["refresh_token"] = _consent(creds["client_id"], creds["client_secret"])
    http = _access(creds)
    if calendar_id:
        # Re-consent after a revoked token: the same OAuth client still reaches the calendar it made.
        resp = http.get(f"{API}/calendars/{calendar_id}")
        if resp.status_code != 200:
            _fail(f"the new token cannot reach {calendar_id} (HTTP {resp.status_code})")
        creds["calendar_id"] = calendar_id
        print(f"ok: reusing Meals ({calendar_id})")
    else:
        resp = http.post(f"{API}/calendars", json={"summary": "Meals", "timeZone": timezone})
        resp.raise_for_status()
        creds["calendar_id"] = resp.json()["id"]
        print(f"ok: created Meals ({creds['calendar_id']})")
    _assert_no_primary(http)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(creds, f, indent=2)
    print(f"ok: credentials written to {out} (0600). Seal it, then delete it.")
    print(f"calendar id for the vdirsyncer tuple and HA: {creds['calendar_id']}")


def check(credentials_path: str) -> None:
    with open(credentials_path) as f:
        creds = json.load(f)
    http = _access(creds)
    _assert_no_primary(http)
    cal = creds["calendar_id"]
    events = http.get(f"{API}/calendars/{cal}/events", params={"maxResults": 50, "singleEvents": "true"})
    events.raise_for_status()
    items = events.json().get("items", [])
    hand = [e for e in items if "meals_slot" not in (e.get("extendedProperties") or {}).get("private", {})]
    print(f"ok: Meals readable, {len(items)} events, {len(hand)} not written by meals-mcp")
    if not hand:
        print("note: create one event by hand on Meals in gcal to test editing it")
        return
    ev = hand[0]
    resp = http.patch(
        f"{API}/calendars/{cal}/events/{ev['id']}",
        json={"extendedProperties": {"private": {"meals_check": "1"}}},
        headers={"If-Match": ev["etag"]},
    )
    if resp.status_code != 200:
        _fail(f"cannot edit the hand-made event {ev.get('summary')!r} (HTTP {resp.status_code})")
    print(f"ok: edited the hand-made event {ev.get('summary')!r} (added a private property)")
