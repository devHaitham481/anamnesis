"""The Anamnesis node: an always-on, self-hosted home for your memory.

One process, one URL:

- `/sync/*`   the hub your computers sync through (encrypted segments, as in a synced folder)
- `/mcp`      remote MCP for web-chat connectors (claude.ai, ChatGPT), behind OAuth
- `/consent`  the sign-in page those connectors send you to (node password)
- `/admin/*`  activity log, revoking apps, wiping (for `anam node …` on a linked computer)

The node is itself a device in the sync: it imports every computer's segments into its own
searchable copy, and publishes notes that web chats save, so your computers pick them up.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import html
import json
import logging
import os
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from src.config import Settings
from src.core import devices
from src.core.memory import MemoryService
from src.memory_mcp import server as mcp_server
from src.node.auth import READ, SCOPES, NodeAuth, PasswordGate

log = logging.getLogger(__name__)

MIN_PASSWORD = 12
MAX_SEGMENT_BYTES = 64 * 1024 * 1024
SYNC_INTERVAL = 60.0
NODE_NAME = "node"


Handler = Callable[[Request], Awaitable[Response]]


class NodeConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class NodeSettings:
    password: str
    public_url: str

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> NodeSettings:
        env = dict(os.environ if environ is None else environ)
        password = env.get("ANAMNESIS_NODE_PASSWORD", "")
        if len(password) < MIN_PASSWORD:
            raise NodeConfigError(
                f"Set ANAMNESIS_NODE_PASSWORD to a password of at least {MIN_PASSWORD} characters. "
                "It protects your whole memory."
            )
        url = env.get("ANAMNESIS_NODE_URL", "").strip().rstrip("/")
        if not url and env.get("FLY_APP_NAME"):
            url = f"https://{env['FLY_APP_NAME']}.fly.dev"
        if not url:
            raise NodeConfigError(
                "Set ANAMNESIS_NODE_URL to the public https:// address of this node."
            )
        return cls(password=password, public_url=url)


def prepare(data_dir: Path) -> devices.SyncConfig:
    """First start: name this device "node" and create the hub (and its memory key)."""
    data_dir.mkdir(parents=True, exist_ok=True)
    device_file = data_dir / "device.json"
    if not device_file.is_file():
        import uuid

        device_file.write_text(json.dumps({"id": uuid.uuid4().hex[:12], "name": NODE_NAME}))
    cfg = devices.load_config(data_dir)
    if cfg is None:
        devices.share(data_dir, data_dir / "hub")
        cfg = devices.load_config(data_dir)
    assert cfg is not None
    return cfg


def _client_ip(request: Request) -> str:
    for header in ("fly-client-ip", "cf-connecting-ip", "x-forwarded-for"):
        value = request.headers.get(header)
        if value:
            return value.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


_SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
    "form-action 'self' https:; frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


_CSS = """
:root { --bg:#f7f7f5; --panel:#fff; --text:#1d1d1b; --muted:#6b6a65; --border:#e2e0da;
  --accent:#c2410c; --ink:#fff; --error:#b42318; color-scheme: light; }
@media (prefers-color-scheme: dark) { :root { --bg:#161615; --panel:#1e1e1c; --text:#ecebe7;
  --muted:#9a9993; --border:#2e2e2b; --accent:#f97316; --ink:#1a0d05; --error:#f87171;
  color-scheme: dark; } }
* { box-sizing: border-box; }
body { margin:0; min-height:100vh; display:grid; place-items:center; padding:24px 16px;
  background:var(--bg); color:var(--text);
  font:15px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
main { width:100%; max-width:420px; background:var(--panel); border:1px solid var(--border);
  border-radius:12px; padding:24px; }
h1 { font-size:18px; margin:0 0 4px; }
p { margin:8px 0; }
.muted { color:var(--muted); font-size:13px; }
code { font:13px ui-monospace,"SF Mono",Menlo,monospace; word-break:break-all; }
label { display:block; margin:14px 0 6px; font-weight:600; font-size:14px; }
input[type=password] { width:100%; padding:10px 12px; border-radius:8px;
  border:1px solid var(--border); background:var(--bg); color:var(--text); font:inherit; }
.check { display:flex; gap:8px; align-items:flex-start; font-weight:400; }
.check input { margin-top:4px; }
.row { display:flex; gap:8px; margin-top:18px; }
button { flex:1; padding:10px 12px; border-radius:8px; border:1px solid var(--border);
  background:var(--panel); color:var(--text); font:inherit; cursor:pointer; }
button.primary { background:var(--accent); border-color:var(--accent); color:var(--ink);
  font-weight:600; }
.error { color:var(--error); }
"""


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    doc = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{_CSS}</style></head>"
        f"<body><main>{body}</main></body></html>"
    )
    return HTMLResponse(doc, status_code=status, headers=_SECURITY_HEADERS)


def build_node_app(
    settings: Settings,
    node: NodeSettings,
    service: MemoryService | None = None,
    sync_interval: float = SYNC_INTERVAL,
) -> Starlette:
    """The node's ASGI app. Pass `service` in tests; otherwise memory is opened at startup."""
    data_dir = settings.data_dir
    base = node.public_url
    cfg_box: dict[str, devices.SyncConfig] = {"cfg": prepare(data_dir)}
    auth = NodeAuth(data_dir / "node-auth.db", base)
    gate = PasswordGate(node.password)
    kick = asyncio.Event()  # something to sync (a computer pushed, a web app saved a note)
    to_index = asyncio.Event()  # new memory to embed
    lock = asyncio.Lock()  # exchange and wipe never overlap

    def hub() -> devices.FolderStore:
        return devices.FolderStore(cfg_box["cfg"].root)

    async def sync_loop(svc: MemoryService, stop: asyncio.Event) -> None:
        """Import what computers pushed; publish notes saved by web chats."""
        try:
            indexer = asyncio.create_task(index_loop(svc, stop))
            while not stop.is_set():
                kick.clear()  # before exchanging, so a push during it triggers another round
                try:
                    async with lock:
                        report = await devices.exchange(svc, cfg_box["cfg"])
                    if report.errors:
                        log.warning("node sync: %s", "; ".join(report.errors))
                    if report.imported:
                        to_index.set()
                except Exception:
                    log.exception("node sync failed")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(_first(stop, kick), timeout=sync_interval)
            indexer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await indexer
        finally:
            await auth.close()

    async def index_loop(svc: MemoryService, stop: asyncio.Event) -> None:
        """Embed new memory for semantic search, separately from syncing: a big first upload
        takes minutes to embed, and new uploads must not wait for it (keyword search works at
        once)."""
        while not stop.is_set():
            to_index.clear()
            try:
                n = await svc.backfill_index()
                if n:
                    log.info("indexed %d messages for semantic search", n)
            except Exception:
                log.exception("indexing failed")
            await _first(stop, to_index)

    async def on_activity(tool: str, detail: str) -> None:
        token = get_access_token()
        await auth.log(token.client_id if token else None, tool, detail)
        if tool == "remember":
            kick.set()  # publish the note to your computers right away

    server = mcp_server.build_server(
        settings,
        service=service,
        auth=AuthSettings(
            issuer_url=base,  # type: ignore[arg-type]
            resource_server_url=f"{base}/mcp",  # type: ignore[arg-type]
            validate_token_resource=False,
            required_scopes=[READ],
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=SCOPES, default_scopes=SCOPES
            ),
            revocation_options=RevocationOptions(enabled=True),
        ),
        auth_provider=auth,
        background=sync_loop,
        activity=on_activity,
    )

    # ---------- helpers ----------

    def route(path: str, methods: list[str]) -> Callable[[Handler], Handler]:
        return cast(Callable[[Handler], Handler], server.custom_route(path, methods=methods))

    def sync_authorized(request: Request) -> bool:
        header = request.headers.get("authorization", "")
        supplied = header[7:] if header.lower().startswith("bearer ") else ""
        expected = devices.node_token(cfg_box["cfg"].key)
        return hmac.compare_digest(supplied.encode(), expected.encode())

    def unauthorized() -> JSONResponse:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    def device_id(request: Request) -> str | None:
        value = str(request.path_params["device"])
        return value if devices.DEVICE_ID.match(value) else None

    # ---------- public pages ----------

    @route("/", methods=["GET"])
    async def home(_: Request) -> Response:
        return _page(
            "Anamnesis node",
            f"""<h1>Your Anamnesis node is running</h1>
<p class="muted">This node keeps your memory available while your computers are off.</p>
<label>Link a computer</label><p><code>anam node link {html.escape(base)}</code></p>
<label>Connector URL for claude.ai / ChatGPT</label><p><code>{html.escape(base)}/mcp</code></p>
<p class="muted">Both ask for the node password.</p>""",
        )

    @route("/healthz", methods=["GET"])
    async def healthz(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    # ---------- consent (OAuth sign-in) ----------

    def consent_form(req_id: str, client: str, redirect_host: str, error: str = "") -> Response:
        err = f'<p class="error">{html.escape(error)}</p>' if error else ""
        return _page(
            "Sign in to your memory",
            f"""<h1>Allow {html.escape(client)} to use your memory?</h1>
<p class="muted">Requested by {html.escape(redirect_host)}. It will be able to search your past
conversations from every AI tool you use.</p>{err}
<form method="post" action="{html.escape(base)}/consent">
<input type="hidden" name="request" value="{html.escape(req_id)}">
<label for="pw">Node password</label>
<input id="pw" name="password" type="password" autocomplete="current-password" required autofocus>
<label class="check"><input type="checkbox" name="write" value="1" checked>
<span>Also allow it to save notes to your memory</span></label>
<div class="row"><button type="submit" name="action" value="deny">Deny</button>
<button class="primary" type="submit" name="action" value="allow">Allow</button></div>
</form>""",
            status=400 if error else 200,
        )

    def expired() -> Response:
        return _page(
            "Request expired",
            "<h1>This sign-in request has expired</h1><p>Go back to the app and connect again.</p>",
            status=400,
        )

    @route("/consent", methods=["GET", "POST"])
    async def consent(request: Request) -> Response:
        if request.method == "GET":
            pending = await auth.pending(request.query_params.get("request", ""))
            if pending is None:
                return expired()
            return consent_form(
                pending.id,
                pending.client.client_name or "An app",
                pending.params.redirect_uri.host or "",
            )
        form = await request.form()
        pending = await auth.pending(str(form.get("request", "")))
        if pending is None:
            return expired()
        if form.get("action") == "deny":
            return RedirectResponse(await auth.deny(pending), status_code=302)
        name, host = pending.client.client_name or "An app", pending.params.redirect_uri.host or ""
        ip = _client_ip(request)
        if gate.blocked(ip):
            return consent_form(pending.id, name, host, "Too many attempts. Wait a few minutes.")
        if not gate.check(ip, str(form.get("password", ""))):
            await asyncio.sleep(1.0)
            return consent_form(pending.id, name, host, "Wrong password.")
        url = await auth.approve(pending, allow_write=form.get("write") == "1")
        return RedirectResponse(url, status_code=302)

    # ---------- sync API (computers) ----------

    @route("/sync/link", methods=["POST"])
    async def link(request: Request) -> Response:
        """Hand the memory key to a computer that knows the node password."""
        ip = _client_ip(request)
        if gate.blocked(ip):
            return JSONResponse({"detail": "Too many attempts"}, status_code=429)
        try:
            password = str((await request.json()).get("password", ""))
        except ValueError:
            return JSONResponse({"detail": "Invalid JSON"}, status_code=400)
        if not gate.check(ip, password):
            await asyncio.sleep(1.0)
            return JSONResponse({"detail": "Wrong password"}, status_code=401)
        return JSONResponse({"code": devices.encode_code(cfg_box["cfg"].key)})

    @route("/sync/meta", methods=["GET"])
    async def meta(request: Request) -> Response:
        if not sync_authorized(request):
            return unauthorized()
        return JSONResponse(hub().meta())

    @route("/sync/devices", methods=["GET"])
    async def list_devices(request: Request) -> Response:
        if not sync_authorized(request):
            return unauthorized()
        return JSONResponse(await asyncio.to_thread(hub().list_devices))

    @route("/sync/devices/{device}", methods=["PUT"])
    async def register(request: Request) -> Response:
        if not sync_authorized(request):
            return unauthorized()
        did = device_id(request)
        if did is None:
            return JSONResponse({"detail": "invalid device id"}, status_code=400)
        name = str((await request.json()).get("name", did))[:80] or did
        await asyncio.to_thread(hub().register, devices.Device(did, name))
        return JSONResponse({"ok": True})

    @route("/sync/devices/{device}/{seq:int}", methods=["GET", "PUT"])
    async def segment(request: Request) -> Response:
        if not sync_authorized(request):
            return unauthorized()
        did = device_id(request)
        seq = int(request.path_params["seq"])
        if did is None or seq < 1:
            return JSONResponse({"detail": "invalid segment"}, status_code=400)
        store = hub()
        if request.method == "GET":
            blob = await asyncio.to_thread(store.read, did, seq)
            if blob is None:
                return Response(status_code=404)
            return Response(blob, media_type="application/octet-stream")
        body = await request.body()
        if len(body) > MAX_SEGMENT_BYTES:
            return JSONResponse({"detail": "segment too large"}, status_code=413)
        if seq != await asyncio.to_thread(store.next_seq, did):
            return JSONResponse({"detail": "segments are append-only"}, status_code=409)
        try:
            devices.decrypt(cfg_box["cfg"].key, did, body)
        except ValueError as e:
            return JSONResponse({"detail": str(e)}, status_code=400)
        await asyncio.to_thread(store.write, did, seq, body)
        kick.set()  # import it into the node's own memory now
        return JSONResponse({"ok": True}, status_code=201)

    # ---------- admin API (linked computers) ----------

    @route("/admin/status", methods=["GET"])
    async def status(request: Request) -> Response:
        if not sync_authorized(request):
            return unauthorized()
        svc = mcp_server._service()
        return JSONResponse(
            {
                "url": base,
                "sources": await svc.source_counts(),
                "devices": await asyncio.to_thread(hub().list_devices),
                "connections": await auth.connections(),
                "semantic": svc.index is not None,
            }
        )

    @route("/admin/activity", methods=["GET"])
    async def activity(request: Request) -> Response:
        if not sync_authorized(request):
            return unauthorized()
        limit = max(1, min(int(request.query_params.get("limit", "50")), 500))
        return JSONResponse(await auth.activity(limit))

    @route("/admin/revoke", methods=["POST"])
    async def revoke(request: Request) -> Response:
        if not sync_authorized(request):
            return unauthorized()
        client = (await request.json()).get("client")
        return JSONResponse({"revoked": await auth.revoke(client or None)})

    @route("/admin/wipe", methods=["POST"])
    async def wipe(request: Request) -> Response:
        """Erase the node's memory, apps and hub, and start over with a new key. Every
        computer keeps its own copy and has to run `anam node link` again."""
        if not sync_authorized(request):
            return unauthorized()
        if (await request.json()).get("confirm") != "wipe":
            return JSONResponse({"detail": "confirmation missing"}, status_code=400)
        async with lock:
            await mcp_server._service().wipe()
            await auth.reset()
            old = cfg_box["cfg"]
            devices.disable(data_dir)
            await asyncio.to_thread(shutil.rmtree, old.root.parent, True)
            cfg_box["cfg"] = prepare(data_dir)
        return JSONResponse({"wiped": True})

    app = server.streamable_http_app(
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        host="0.0.0.0",
    )
    return app


async def _first(*events: asyncio.Event) -> None:
    waiters = [asyncio.create_task(e.wait()) for e in events]
    try:
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for w in waiters:
            w.cancel()


def main(host: str = "0.0.0.0", port: int | None = None) -> None:
    import uvicorn

    from src.config import load_settings

    settings = load_settings()
    try:
        node = NodeSettings.from_env()
    except NodeConfigError as e:
        raise SystemExit(str(e)) from None
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    app = build_node_app(settings, node)
    log.info("Anamnesis node at %s (data in %s)", node.public_url, settings.data_dir)
    uvicorn.run(
        app,
        host=host,
        port=port or int(os.environ.get("PORT", "8080")),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
