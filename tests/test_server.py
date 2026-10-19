from types import SimpleNamespace

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from meals_mcp import server as server_mod
from meals_mcp.server import build_server, pocket_id_user
from meals_mcp.service import MealsError
from tests.conftest import TOM

TOOLS = {
    "get_week", "search_recipes", "get_recipe", "list_freezer", "set_meal", "clear_meal", "add_prep",
    "remove_prep", "add_recipe", "update_recipe", "adjust_freezer", "add_freezer_batch",
    "list_rules", "add_rule", "update_rule", "remove_rule",
}


async def test_tool_surface_has_no_bulk_or_delete_recipe_tools(svc):
    async with Client(build_server(svc, lambda: TOM)) as client:
        assert {t.name for t in await client.list_tools()} == TOOLS


async def test_round_trip_through_mcp(svc):
    async with Client(build_server(svc, lambda: TOM)) as client:
        await client.call_tool("set_meal", {"date": "2026-10-17", "slot": "dinner", "free_text": "Soup"})
        week = (await client.call_tool("get_week", {})).data
        assert week["days"][1]["dinner"]["title"] == "Dinner: Soup"


async def test_refusals_reach_claude_as_tool_errors(svc):
    async with Client(build_server(svc, lambda: TOM)) as client:
        with pytest.raises(ToolError, match="outside the plannable window"):
            await client.call_tool("set_meal", {"date": "2027-01-01", "slot": "dinner", "free_text": "x"})


def _token(monkeypatch, claims):
    monkeypatch.setattr(server_mod, "get_access_token", lambda: SimpleNamespace(claims=claims))


def test_group_gate(monkeypatch):
    resolve = pocket_id_user("meals")
    _token(monkeypatch, {"sub": "abc", "groups": ["admins"]})
    with pytest.raises(MealsError, match="not in the 'meals' group"):
        resolve()
    _token(monkeypatch, {"sub": "abc", "groups": ["meals"], "preferred_username": "tom"})
    assert resolve().sub == "abc" and resolve().name == "tom"


def test_unauthenticated(monkeypatch):
    monkeypatch.setattr(server_mod, "get_access_token", lambda: None)
    with pytest.raises(MealsError, match="not signed in"):
        pocket_id_user("meals")()


async def test_a_refused_user_cannot_read_either(svc):
    def outsider():
        raise MealsError("your account is not in the 'meals' group")

    async with Client(build_server(svc, outsider)) as client:
        with pytest.raises(ToolError, match="meals"):
            await client.call_tool("get_week", {})
