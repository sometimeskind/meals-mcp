"""The real OIDCProxy wiring against a stub Pocket ID discovery document."""

import http.server
import json
import threading

import httpx
import pytest
from cryptography.fernet import Fernet

from meals_mcp.__main__ import build_auth
from meals_mcp.config import ServerSettings
from meals_mcp.server import build_server

CLAUDE = "https://claude.ai/api/mcp/auth_callback"


@pytest.fixture
def issuer():
    holder = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            base = holder["url"]
            docs = {
                "/.well-known/openid-configuration": {
                    "issuer": base,
                    "authorization_endpoint": f"{base}/authorize",
                    "token_endpoint": f"{base}/api/oidc/token",
                    "jwks_uri": f"{base}/.well-known/jwks.json",
                    "response_types_supported": ["code"],
                    "subject_types_supported": ["public"],
                    "id_token_signing_alg_values_supported": ["RS256"],
                },
                "/.well-known/jwks.json": {"keys": []},
            }
            body = json.dumps(docs.get(self.path, {})).encode()
            self.send_response(200 if self.path in docs else 404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    holder["url"] = f"http://127.0.0.1:{srv.server_port}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield holder["url"]
    srv.shutdown()


async def test_dcr_only_admits_the_claude_callback_and_stores_encrypted(issuer, svc, store):
    settings = ServerSettings(
        database_url="unused",
        google_credentials="unused",
        base_url="https://meals.example.test",
        oidc_config_url=f"{issuer}/.well-known/openid-configuration",
        oidc_client_id="meals-mcp",
        oidc_client_secret="s3cret",
        jwt_signing_key="jwt-key",
        storage_key=Fernet.generate_key().decode(),
        required_group="meals",
        allowed_redirects=[CLAUDE],
    )
    auth = build_auth(settings, store)
    app = build_server(svc, lambda: None, auth=auth).http_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://meals.example.test") as c:
            meta = (await c.get("/.well-known/oauth-authorization-server")).json()
            assert "groups" in meta["scopes_supported"]
            assert (await c.post("/mcp", json={})).status_code == 401
            assert (await c.get("/healthz")).text == "ok"

            reg = {"redirect_uris": ["https://evil.example/cb"], "client_name": "x",
                   "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"],
                   "response_types": ["code"]}
            assert (await c.post("/register", json=reg)).status_code == 400
            ok = await c.post("/register", json=reg | {"redirect_uris": [CLAUDE]})
            assert ok.status_code == 201, ok.text

    async with store.pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM oauth_proxy_kv")
    assert rows, "client registration not persisted in Postgres"
    assert all(CLAUDE not in json.dumps([str(v) for v in r.values()]) for r in rows), "stored in plaintext"

    # Pocket ID rejects a resource that isn't one of its APIs, so the client's is not forwarded
    # (a private FastMCP method: the consent page sits between /authorize and the redirect).
    upstream = auth._build_upstream_authorize_url(
        "txn", {"resource": "https://meals.example.test/mcp", "scopes": ["openid", "groups"]})
    query = httpx.URL(upstream).params
    assert "resource" not in query
    assert query["scope"] == "openid groups"
