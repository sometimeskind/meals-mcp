"""meals-mcp entrypoint: `serve` (the default) or the workstation-only `bootstrap` and `check`."""

from __future__ import annotations

import argparse
import asyncio
import logging


def build_auth(settings, store):
    """OIDCProxy in front of Pocket ID, its state Fernet-encrypted in the app's own database."""
    from cryptography.fernet import Fernet
    from fastmcp.server.auth.oidc_proxy import OIDCProxy
    from key_value.aio.stores.postgresql import PostgreSQLStore
    from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

    return OIDCProxy(
        config_url=settings.oidc_config_url,
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        base_url=settings.base_url,
        # Pocket ID's id_token carries the groups claim the tools check.
        verify_id_token=True,
        required_scopes=["openid", "profile", "email", "groups"],
        allowed_client_redirect_uris=settings.allowed_redirects,
        jwt_signing_key=settings.jwt_signing_key,
        client_storage=FernetEncryptionWrapper(
            key_value=PostgreSQLStore(pool=store.pool, table_name="oauth_proxy_kv"),
            fernet=Fernet(settings.storage_key),
            # A rotated key reads old rows as missing: every user reconnects, nothing crashes.
            raise_on_decryption_error=False,
        ),
    )


async def serve() -> None:
    from prometheus_client import start_http_server

    from meals_mcp.config import Limits, ServerSettings
    from meals_mcp.gcal import GoogleCalendar, GoogleCredentials
    from meals_mcp.server import build_server, pocket_id_user
    from meals_mcp.service import MealsService
    from meals_mcp.store import Store

    settings = ServerSettings.from_env()
    limits = Limits.from_env()
    store = await Store.connect(settings.database_url)
    calendar = GoogleCalendar(GoogleCredentials.load(settings.google_credentials))
    service = MealsService(calendar, store, limits)
    mcp = build_server(service, pocket_id_user(settings.required_group), auth=build_auth(settings, store))
    # A plain socket on "::" also accepts IPv4 (unlike uvicorn's, see ServerSettings.host).
    start_http_server(settings.metrics_port, addr="::")
    try:
        await mcp.run_async(transport="http", host=settings.host, port=settings.port)
    finally:
        await store.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(prog="meals-mcp")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="run the MCP server (default)")
    boot = sub.add_parser("bootstrap", help="workstation: consent, create the Meals calendar, write credentials")
    boot.add_argument("--client-secret", required=True, help="the OAuth client JSON downloaded from GCP")
    boot.add_argument("--out", required=True, help="where to write the credentials JSON (mode 0600)")
    boot.add_argument("--timezone", required=True, help="the Meals calendar's timezone, e.g. Europe/Amsterdam")
    boot.add_argument("--calendar-id", help="re-consent only: keep this existing Meals calendar")
    check = sub.add_parser("check", help="workstation: verify the token's reach against the live calendar")
    check.add_argument("--credentials", required=True)
    args = parser.parse_args()

    if args.cmd == "bootstrap":
        from meals_mcp.bootstrap import bootstrap

        bootstrap(args.client_secret, args.out, args.timezone, args.calendar_id)
    elif args.cmd == "check":
        from meals_mcp.bootstrap import check

        check(args.credentials)
    else:
        asyncio.run(serve())


if __name__ == "__main__":
    main()
