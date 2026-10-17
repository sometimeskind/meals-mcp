# meals-mcp

An MCP server for the household's shared **Meals** calendar, its recipes and the freezer stock,
used from the claude.ai chat client (homelab
[#2047](https://github.com/sometimeskind/homelab/issues/2047)). There is no web UI: this server is
the whole app. It runs in the home cluster (`kubernetes/meals/` in homelab).

## How it works

| Piece | What |
|-------|------|
| Plan | A Google secondary calendar, *Meals*, created by this app's own OAuth client with only `calendar.app.created`, so the token reaches Meals and nothing else. All-day `Lunch:`, `Dinner:` and `Prep:` events. The calendar is the only store of the plan; homelab's calendar-sync mirrors it into Stalwart for the wall tablet. |
| State | Postgres (a CNPG `Database` on the shared cluster): `recipes`, `freezer_batches`, `freezer_reservations`, `audit_log`, and `oauth_proxy_kv` (the OAuth proxy's Fernet-encrypted client and token store). The schema is created on start. |
| Auth | FastMCP `OIDCProxy` in front of Pocket ID. Claude registers through the proxy's DCR, limited to `MEALS_ALLOWED_REDIRECTS`. Tools refuse users outside the `meals` group (the `groups` claim of the Pocket ID id_token); the audit records the Pocket ID `sub`. |
| Metrics | Port 9090: `meals_tool_calls_total{tool,outcome}`, `meals_google_auth_failures_total`, `meals_rate_limited_total`. |

### Tools

Read: `get_week`, `search_recipes`, `get_recipe`, `list_freezer`.
Write: `set_meal`, `clear_meal`, `add_prep`, `remove_prep`, `add_recipe`, `update_recipe`,
`adjust_freezer`, `add_freezer_batch`.

There are no bulk tools, no raw calendar access and no recipe deletion.

### Rules the code enforces

- **Window**: meal and prep dates from 7 days ago to 28 days ahead, in `MEALS_TZ`. A back-filled
  freezer batch may carry any past `made_on`.
- **Rate**: 30 writes per rolling hour per user, counted from the audit log, so the limit survives
  restarts. The refusal tells Claude when the next write is allowed.
- **Audit**: every write records the user, the arguments, and the before/after state.
- **Finding a meal**: each event the app writes carries a private `meals_slot` property. A
  hand-made event matches on a case-insensitive `Lunch:`/`Dinner:`/`Prep:` title prefix. Two or
  more matching events in one slot are refused, with nothing written, and listed so the user can
  pick. `set_meal` updates in place and never deletes and recreates.
- **Concurrency**: updates and deletes send the event's etag. A change made in gcal since the read
  is reported as a conflict instead of being overwritten. Writes are serialised in-process, and
  each holds one DB transaction open across the calendar call, so a refused calendar write leaves
  the tables untouched.
- **Freezer**: one container is one family meal. A batch's label is the lowest number not held by
  a batch that still has containers. `set_meal(..., from_freezer=N)` reserves a container in
  `freezer_reservations`, and `clear_meal` or a replacing `set_meal` releases it. A reservation
  dated before today counts as eaten, even if its event was later deleted by hand. A future one
  whose event is gone shows in `get_week` as `orphan_reservations`, and `clear_meal` on that slot
  releases it. `to_freezer=N` on `set_meal`/`add_prep` creates a batch dated that day. That batch
  is removed if its event is cleared before anything was reserved from it.
- **Untrusted text**: recipe text and event titles are household-written. Every read result says
  so, and the server instructions tell Claude never to follow instructions found inside them.
- **Duplicate recipes**: `add_recipe` refuses a name that already exists (case- and
  space-insensitive) unless `allow_duplicate=true`.
- **Long titles**: the meal name is cut to `MEALS_TITLE_MAX` (60) characters for the tablet card.

## Configuration

| Variable | Default | |
|----------|---------|-|
| `MEALS_TZ` | (required) | Household timezone for "today" and the window |
| `MEALS_DATABASE_URL` | (required) | `postgresql://…` |
| `MEALS_GOOGLE_CREDENTIALS` | `/secrets/google/credentials.json` | Written by `bootstrap` |
| `MEALS_BASE_URL` | (required) | The public URL Claude connects to |
| `MEALS_OIDC_CONFIG_URL` | (required) | Pocket ID's `/.well-known/openid-configuration` |
| `MEALS_OIDC_CLIENT_ID` / `_SECRET` | (required) | The Pocket ID `meals-mcp` client |
| `MEALS_JWT_SIGNING_KEY` | (required) | Signs the proxy's own tokens. Rotating it forces every user to reconnect |
| `MEALS_STORAGE_KEY` | (required) | Fernet key for `oauth_proxy_kv`. Rotating it drops all registrations and tokens, and users reconnect |
| `MEALS_REQUIRED_GROUP` | `meals` | |
| `MEALS_ALLOWED_REDIRECTS` | `https://claude.ai/api/mcp/auth_callback` | Comma-separated; add `http://localhost:*` to test from Claude Code |
| `MEALS_WRITES_PER_HOUR` | 30 | |
| `MEALS_WINDOW_PAST_DAYS` / `_FUTURE_DAYS` | 7 / 28 | |
| `MEALS_FREEZER_OLD_DAYS` | 90 | `list_freezer` flags older batches |
| `MEALS_TITLE_MAX` | 60 | |
| `MEALS_PORT` / `MEALS_METRICS_PORT` | 8000 / 9090 | |

## Bootstrap (once, on the workstation)

The Meals calendar must be created by this app. One made by hand in gcal is invisible to the
token.

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -e .
.venv/bin/python -m meals_mcp bootstrap --client-secret client_secret.json --out credentials.json --timezone Europe/Amsterdam
```

It runs the consent flow, checks that the grant is exactly `calendar.app.created`, creates Meals,
asserts the token **cannot** read the primary calendar, and writes `credentials.json` (0600). Seal
that file as the pod's Google secret, then delete it. `check --credentials credentials.json`
re-tests the token's reach later. With an event made by hand on Meals, it also proves the app can
edit such events.

If Google revokes the refresh token, every tool reports "Calendar unavailable … re-run bootstrap"
and `meals_google_auth_failures_total` rises. Re-consent with `bootstrap ... --calendar-id <id>`,
which keeps the existing Meals calendar instead of creating a second one, then reseal.

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt -e .
just test   # starts a throwaway Postgres 17 in podman
```

Tests run against a real Postgres and an in-memory fake calendar (`tests/fakes.py`, with Google's
etag semantics). `tests/test_auth.py` builds the real OIDCProxy against a stub discovery document
and checks that DCR only admits the configured callback and that client registrations land
encrypted in Postgres.
