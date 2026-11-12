"""The self-hosted node, end to end over real HTTP: linking, sync through the hub, the OAuth
sign-in that web-chat connectors use, MCP over HTTP, read-only grants, activity, revoke, wipe."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import socket
import threading
import time
import urllib.parse
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from src.config import Settings
from src.core import devices
from src.core.memory import MemoryService
from src.node.app import NodeConfigError, NodeSettings, build_node_app
from tests.conftest import conversation, open_service

PASSWORD = "correct horse battery staple"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class Node:
    url: str
    settings: Settings


@pytest.fixture
def node(tmp_path: Path) -> Iterator[Node]:
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    settings = Settings(data_dir=tmp_path / "node", semantic_search=False, memory_sync=False)
    app = build_node_app(settings, NodeSettings(PASSWORD, url), sync_interval=0.2)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not srv.started:
        if time.time() > deadline:
            raise RuntimeError("node did not start")
        time.sleep(0.05)
    yield Node(url, settings)
    srv.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
async def laptops(tmp_path: Path) -> AsyncIterator[tuple[MemoryService, MemoryService]]:
    a = await open_service(Settings(data_dir=tmp_path / "a", semantic_min_score=0.3))
    b = await open_service(Settings(data_dir=tmp_path / "b", semantic_min_score=0.3))
    yield a, b
    await a.close()
    await b.close()


def admin(node: Node, data_dir: Path, method: str, path: str, data: Any = None) -> Any:
    cfg = devices.load_config(data_dir)
    assert cfg is not None
    return devices.NodeStore(node.url, cfg.key).api(method, path, data)


async def eventually(check: Any, limit: float = 10.0) -> None:
    deadline = time.monotonic() + limit
    while True:
        if await check():
            return
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.1)


# ---------- configuration ----------


def test_node_settings_require_password_and_url() -> None:
    with pytest.raises(NodeConfigError, match="at least 12"):
        NodeSettings.from_env({"ANAMNESIS_NODE_PASSWORD": "short", "ANAMNESIS_NODE_URL": "x"})
    with pytest.raises(NodeConfigError, match="ANAMNESIS_NODE_URL"):
        NodeSettings.from_env({"ANAMNESIS_NODE_PASSWORD": PASSWORD})
    s = NodeSettings.from_env({"ANAMNESIS_NODE_PASSWORD": PASSWORD, "FLY_APP_NAME": "anam-x"})
    assert s.public_url == "https://anam-x.fly.dev"


def test_node_url_must_be_https_except_localhost() -> None:
    assert devices.normalize_node_url("anam.fly.dev/") == "https://anam.fly.dev"
    assert devices.normalize_node_url("http://localhost:8080") == "http://localhost:8080"
    with pytest.raises(devices.SyncSetupError, match="https"):
        devices.normalize_node_url("http://my-node.example.com")


# ---------- linking & sync ----------


async def test_link_sync_through_node_and_notes_flow_back(
    node: Node, laptops: tuple[MemoryService, MemoryService]
) -> None:
    a, b = laptops
    with pytest.raises(devices.SyncSetupError, match="Wrong node password"):
        devices.link_node(a.settings.data_dir, node.url, "not the password")
    devices.link_node(a.settings.data_dir, node.url, PASSWORD)
    devices.link_node(b.settings.data_dir, node.url, PASSWORD)

    await a.import_conversation(
        conversation("s1", "we rotate the staging api keys every month", "via vault", project="/w")
    )
    await a.remember("Prefers pnpm over npm.", topic="preferences")
    assert (await devices.exchange(a)).exported == 3

    # The node imports what A pushed into its own searchable copy.
    async def node_has_it() -> bool:
        info = admin(node, a.settings.data_dir, "GET", "/admin/status")
        return bool(info["sources"].get("claude-code"))

    await eventually(node_has_it)

    got = await devices.exchange(b)
    assert got.imported == 3 and got.errors == []
    assert [h.content for h in await b.search("staging api keys", k=1)] == [
        "we rotate the staging api keys every month"
    ]
    assert (await devices.exchange(b)).exported == 0  # never echoed back

    found = admin(node, a.settings.data_dir, "GET", "/admin/status")["devices"]
    assert len(found) == 3 and "node" in {d["name"] for d in found}


async def test_scope_limits_what_leaves_the_computer(
    node: Node, laptops: tuple[MemoryService, MemoryService]
) -> None:
    a, b = laptops
    devices.link_node(a.settings.data_dir, node.url, PASSWORD, devices.Scope("all", ("/w/secret",)))
    devices.link_node(b.settings.data_dir, node.url, PASSWORD)
    await a.import_conversation(conversation("s1", "public project talk", project="/w/app"))
    await a.import_conversation(conversation("s2", "secret project talk", project="/w/secret/x"))
    await a.remember("a note")
    assert (await devices.exchange(a)).exported == 2
    await devices.exchange(b)
    assert set(await b.source_counts()) == {"claude-code", "note"}
    assert all("secret" not in h.content for h in await b.search("secret project", k=5))

    # Narrowing to notes only; widening again re-exports what was held back (no duplicates).
    devices.set_scope(a.settings.data_dir, devices.Scope("notes"))
    await a.import_conversation(conversation("s3", "later session"))
    assert (await devices.exchange(a)).exported == 1  # only the note, re-sent idempotently
    devices.set_scope(a.settings.data_dir, devices.Scope("all"))
    await devices.exchange(a)
    await devices.exchange(b)
    threads = await b.list_threads()
    firsts = [(await b.messages(t.id))[0].content for t in threads]
    assert sorted(firsts) == [
        "a note",
        "later session",
        "public project talk",
        "secret project talk",
    ]
    counts = [len(await b.messages(t.id)) for t in threads]
    assert sum(counts) == 4  # nothing duplicated


def test_sync_api_rejects_bad_requests(node: Node, tmp_path: Path) -> None:
    devices.link_node(tmp_path / "c", node.url, PASSWORD)
    cfg = devices.load_config(tmp_path / "c")
    assert cfg is not None
    token = devices.node_token(cfg.key)
    with httpx.Client(base_url=node.url) as http:
        assert http.get("/sync/devices").status_code == 401
        assert (
            http.get("/sync/devices", headers={"Authorization": "Bearer nope"}).status_code == 401
        )
        auth = {"Authorization": f"Bearer {token}"}
        assert http.put("/sync/devices/../x/1", headers=auth, content=b"x").status_code in (
            400,
            404,
        )
        assert (
            http.put("/sync/devices/abcdefabcdef/1", headers=auth, content=b"junk").status_code
            == 400
        )
        good = devices.encrypt(cfg.key, "abcdefabcdef", [{"type": "conversation"}])
        assert (
            http.put("/sync/devices/abcdefabcdef/2", headers=auth, content=good).status_code == 409
        )
        assert (
            http.put("/sync/devices/abcdefabcdef/1", headers=auth, content=good).status_code == 201
        )
        assert (
            http.put("/sync/devices/abcdefabcdef/1", headers=auth, content=good).status_code == 409
        )
        # The page shows how to link and connect, never a secret.
        home = http.get("/").text
        assert "anam node link" in home and devices.encode_code(cfg.key) not in home


def test_password_attempts_are_rate_limited(node: Node) -> None:
    with httpx.Client(base_url=node.url) as http:
        codes = [
            http.post("/sync/link", json={"password": f"guess-{i}"}).status_code for i in range(6)
        ]
        assert codes[:5] == [401] * 5 and codes[5] == 429
        assert http.post("/sync/link", json={"password": PASSWORD}).status_code == 429


# ---------- web-chat connectors: OAuth + MCP over HTTP ----------


def oauth_sign_in(node: Node, allow_write: bool, name: str = "Claude") -> dict[str, Any]:
    """Do what claude.ai does: register, authorize with PKCE, sign in on the node, get tokens."""
    redirect = "https://claude.ai/api/mcp/auth_callback"
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    with httpx.Client(base_url=node.url) as http:
        meta = http.get("/.well-known/oauth-authorization-server").json()
        assert meta["registration_endpoint"].endswith("/register")
        client = http.post(
            "/register",
            json={
                "redirect_uris": [redirect],
                "client_name": name,
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
            },
        ).json()
        r = http.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client["client_id"],
                "redirect_uri": redirect,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "st4te",
                "scope": "memory:read memory:write",
            },
        )
        assert r.status_code in (302, 307), r.text
        consent_url = r.headers["location"]
        assert consent_url.startswith(node.url + "/consent?request=")
        page = http.get(consent_url)
        assert page.status_code == 200 and f"Allow {name}" in page.text
        assert page.headers["x-frame-options"] == "DENY"
        request_id = urllib.parse.parse_qs(urllib.parse.urlsplit(consent_url).query)["request"][0]
        wrong = http.post(
            "/consent", data={"request": request_id, "password": "nope", "action": "allow"}
        )
        assert wrong.status_code == 400 and "Wrong password" in wrong.text
        form = {"request": request_id, "password": PASSWORD, "action": "allow"}
        if allow_write:
            form["write"] = "1"
        done = http.post("/consent", data=form)
        assert done.status_code == 302
        back = urllib.parse.urlsplit(done.headers["location"])
        query = urllib.parse.parse_qs(back.query)
        assert done.headers["location"].startswith(redirect) and query["state"] == ["st4te"]
        token = http.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": query["code"][0],
                "redirect_uri": redirect,
                "client_id": client["client_id"],
                "code_verifier": verifier,
            },
        )
        assert token.status_code == 200, token.text
        return {**token.json(), "client_id": client["client_id"]}


async def call_mcp(node: Node, token: str | None, tool: str, args: dict[str, Any]) -> Any:
    import httpx2
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with (
        httpx2.AsyncClient(headers=headers, timeout=30) as http,
        streamable_http_client(f"{node.url}/mcp", http_client=http) as (read, write, *_),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        return await session.call_tool(tool, args)


def text(result: Any) -> str:
    return "\n".join(c.text for c in result.content)


async def test_web_connector_read_only_then_revoke(
    node: Node, laptops: tuple[MemoryService, MemoryService]
) -> None:
    a, _ = laptops
    devices.link_node(a.settings.data_dir, node.url, PASSWORD)
    await a.import_conversation(
        conversation("s1", "the staging database lives in eu-west-1", title="Infra notes")
    )
    await devices.exchange(a)

    async with httpx.AsyncClient(base_url=node.url) as http:
        r = await http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert r.status_code == 401 and "resource_metadata" in r.headers["www-authenticate"]

    tokens = oauth_sign_in(node, allow_write=False)
    assert tokens["scope"] == "memory:read"

    async def found() -> bool:
        result = await call_mcp(node, tokens["access_token"], "search_memory", {"query": "staging"})
        return "eu-west-1" in text(result)

    await eventually(found)
    denied = await call_mcp(node, tokens["access_token"], "remember", {"note": "x y z"})
    assert denied.is_error and "read-only" in text(denied)

    log = admin(node, a.settings.data_dir, "GET", "/admin/activity")
    assert [(e["client"], e["tool"]) for e in log][0] == ("Claude", "search_memory")
    assert not any(e["tool"] == "remember" for e in log)

    (conn,) = admin(node, a.settings.data_dir, "GET", "/admin/status")["connections"]
    assert conn["name"] == "Claude" and conn["can_save"] is False
    assert admin(node, a.settings.data_dir, "POST", "/admin/revoke", {"client": "claude"}) == {
        "revoked": 1
    }
    with pytest.raises(Exception):  # noqa: B017 - the transport raises on 401
        await call_mcp(node, tokens["access_token"], "search_memory", {"query": "staging"})


async def test_web_connector_saves_notes_that_reach_computers(
    node: Node, laptops: tuple[MemoryService, MemoryService]
) -> None:
    a, _ = laptops
    devices.link_node(a.settings.data_dir, node.url, PASSWORD)
    tokens = oauth_sign_in(node, allow_write=True, name="ChatGPT")
    assert set(tokens["scope"].split()) == {"memory:read", "memory:write"}
    saved = await call_mcp(
        node,
        tokens["access_token"],
        "remember",
        {"note": "Lisbon offsite is in May.", "topic": "team"},
    )
    assert not saved.is_error and "Notes: team" in text(saved)

    async def arrived() -> bool:
        await devices.exchange(a)
        return bool(await a.search("Lisbon offsite", k=1))

    await eventually(arrived)  # saved on the node while the computer was "asleep"

    # Refresh tokens rotate; the old one stops working.
    async with httpx.AsyncClient(base_url=node.url) as http:
        body = {
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": tokens["client_id"],
        }
        fresh = await http.post("/token", data=body)
        assert fresh.status_code == 200 and fresh.json()["access_token"] != tokens["access_token"]
        assert (await http.post("/token", data=body)).status_code == 400


async def test_wipe_erases_node_and_requires_relinking(
    node: Node, laptops: tuple[MemoryService, MemoryService]
) -> None:
    a, _ = laptops
    devices.link_node(a.settings.data_dir, node.url, PASSWORD)
    await a.remember("something to forget")
    await devices.exchange(a)
    tokens = oauth_sign_in(node, allow_write=True)
    old_key = devices.load_config(a.settings.data_dir).key  # type: ignore[union-attr]

    assert admin(node, a.settings.data_dir, "POST", "/admin/wipe", {"confirm": "wipe"}) == {
        "wiped": True
    }
    with pytest.raises(OSError, match="401"):
        admin(node, a.settings.data_dir, "GET", "/admin/status")
    with pytest.raises(Exception):  # noqa: B017 - app grants are gone too
        await call_mcp(node, tokens["access_token"], "search_memory", {"query": "forget"})

    devices.link_node(a.settings.data_dir, node.url, PASSWORD)
    assert devices.load_config(a.settings.data_dir).key != old_key  # type: ignore[union-attr]
    info = admin(node, a.settings.data_dir, "GET", "/admin/status")
    assert info["sources"] == {} and info["connections"] == []
    # This computer still has its memory and can upload it again.
    assert await a.search("something to forget", k=1)
    assert (await devices.exchange(a)).exported == 1


def test_node_status_page_and_metadata(node: Node) -> None:
    with httpx.Client(base_url=node.url) as http:
        assert http.get("/healthz").json() == {"status": "ok"}
        prm = http.get("/.well-known/oauth-protected-resource/mcp")
        assert prm.status_code == 200, prm.text
        assert prm.json()["resource"] == f"{node.url}/mcp"
        assert "text/html" in http.get("/").headers["content-type"]


# ---------- CLI ----------


def test_cli_link_scope_status_and_connect(
    node: Node, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from src.cli.__main__ import main

    data = tmp_path / "cli"
    monkeypatch.setenv("ANAMNESIS_DATA_DIR", str(data))
    monkeypatch.setenv("ANAMNESIS_SEMANTIC_SEARCH", "false")
    monkeypatch.setattr("src.ingest.runner.default_sources", lambda: [])  # no real tool history

    assert main(["connect", "claude-web"]) == 1
    assert "anam node deploy fly" in capsys.readouterr().out
    assert main(["node", "status"]) == 0
    assert "Not linked to a node" in capsys.readouterr().out

    monkeypatch.setenv("ANAMNESIS_NODE_PASSWORD", PASSWORD)
    assert main(["node", "link", node.url, "--notes-only"]) == 0
    out = capsys.readouterr().out
    assert f"Linked to {node.url}. Uploading: notes only." in out
    cfg = devices.load_config(data)
    assert cfg is not None and cfg.scope.mode == "notes"

    assert main(["node", "scope", "--everything"]) == 0
    assert devices.load_config(data).scope.mode == "all"  # type: ignore[union-attr]
    assert main(["node", "status"]) == 0
    out = capsys.readouterr().out
    assert f"Node: {node.url}" in out and "none yet" in out
    assert main(["connect", "claude-web"]) == 0
    assert f"URL: {node.url}/mcp" in capsys.readouterr().out
    assert main(["node", "revoke"]) == 1  # must name an app or pass --all
    assert main(["node", "activity"]) == 0
    assert "No activity yet." in capsys.readouterr().out
    assert main(["node", "wipe", "--yes"]) == 0
    assert devices.load_config(data) is None
