"""OAuth for web-chat connectors (claude.ai, ChatGPT): the node is its own authorization server.

There is one user, the node's owner, who signs in with the node password on a consent page and
chooses read-only or read + save notes. Clients register themselves (RFC 7591), as claude.ai and
ChatGPT do. Tokens are stored only as SHA-256 hashes. Every connector call is logged so
`anam node activity` can show what each app searched or saved.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

READ = "memory:read"
WRITE = "memory:write"
SCOPES = [READ, WRITE]

ACCESS_TTL = 3600
REFRESH_TTL = 90 * 24 * 3600  # rolling: every refresh issues a new one
CODE_TTL = 300
REQUEST_TTL = 900

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    client_id  TEXT PRIMARY KEY,
    info       TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS requests (
    id         TEXT PRIMARY KEY,
    client_id  TEXT NOT NULL,
    params     TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS codes (
    code       TEXT PRIMARY KEY,
    data       TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
    hash       TEXT PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('access', 'refresh')),
    client_id  TEXT NOT NULL,
    scopes     TEXT NOT NULL,
    resource   TEXT,
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tokens_client ON tokens(client_id);
CREATE TABLE IF NOT EXISTS activity (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    client_id TEXT,
    tool      TEXT NOT NULL,
    detail    TEXT NOT NULL
);
"""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _password_hash(password: str) -> bytes:
    return hashlib.sha256(password.encode()).digest()


class PasswordGate:
    """Constant-time password check with a failure budget (per client and in total)."""

    WINDOW = 300.0
    PER_CLIENT = 5
    TOTAL = 30

    def __init__(self, password: str) -> None:
        self._hash = _password_hash(password)
        self._failures: dict[str, deque[float]] = {}
        self._all: deque[float] = deque()

    def _prune(self, q: deque[float], now: float) -> None:
        while q and now - q[0] > self.WINDOW:
            q.popleft()

    def blocked(self, client: str) -> bool:
        now = time.monotonic()
        q = self._failures.setdefault(client, deque())
        self._prune(q, now)
        self._prune(self._all, now)
        return len(q) >= self.PER_CLIENT or len(self._all) >= self.TOTAL

    def check(self, client: str, password: str) -> bool:
        ok = hmac.compare_digest(_password_hash(password), self._hash)
        if not ok:
            now = time.monotonic()
            self._failures.setdefault(client, deque()).append(now)
            self._all.append(now)
        return ok


@dataclass(frozen=True)
class PendingRequest:
    id: str
    client: OAuthClientInformationFull
    params: AuthorizationParams


class NodeAuth:
    """`OAuthAuthorizationServerProvider` backed by SQLite, plus the connector activity log."""

    def __init__(self, path: Path, base_url: str) -> None:
        self.path = path
        self.base_url = base_url.rstrip("/")
        self._conn: aiosqlite.Connection | None = None
        self._opening = asyncio.Lock()

    async def open(self) -> NodeAuth:
        await self._c()
        return self

    async def _c(self) -> aiosqlite.Connection:
        """The connection, opened on first use (requests can arrive before any startup task)."""
        if self._conn is None:
            async with self._opening:
                if self._conn is None:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    conn = await aiosqlite.connect(self.path)
                    conn.row_factory = aiosqlite.Row
                    await conn.executescript(SCHEMA)
                    await conn.commit()
                    self._conn = conn
        return self._conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def _exec(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        conn = await self._c()
        await conn.execute(sql, params)
        await conn.commit()

    async def _one(self, sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Row | None:
        async with (await self._c()).execute(sql, params) as cur:
            return await cur.fetchone()

    async def _all(self, sql: str, params: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        async with (await self._c()).execute(sql, params) as cur:
            return list(await cur.fetchall())

    # ---------- clients ----------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        row = await self._one("SELECT info FROM clients WHERE client_id = ?", (client_id,))
        return OAuthClientInformationFull.model_validate_json(row["info"]) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if client_info.client_id is None:
            raise ValueError("client_id is required")
        await self._exec(
            "INSERT OR REPLACE INTO clients(client_id, info, created_at) VALUES (?, ?, ?)",
            (client_info.client_id, client_info.model_dump_json(), time.time()),
        )

    # ---------- authorization: the consent page in between ----------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Park the request and send the browser to the node's own sign-in page."""
        request_id = secrets.token_urlsafe(24)
        now = time.time()
        await self._exec("DELETE FROM requests WHERE expires_at < ?", (now,))
        await self._exec(
            "INSERT INTO requests(id, client_id, params, expires_at) VALUES (?, ?, ?, ?)",
            (request_id, client.client_id, params.model_dump_json(), now + REQUEST_TTL),
        )
        return f"{self.base_url}/consent?request={request_id}"

    async def pending(self, request_id: str) -> PendingRequest | None:
        row = await self._one(
            "SELECT client_id, params FROM requests WHERE id = ? AND expires_at >= ?",
            (request_id, time.time()),
        )
        if row is None:
            return None
        client = await self.get_client(row["client_id"])
        if client is None:
            return None
        params = AuthorizationParams.model_validate_json(row["params"])
        return PendingRequest(request_id, client, params)

    async def approve(self, request: PendingRequest, allow_write: bool) -> str:
        """Issue an authorization code for an approved request; returns the redirect URL."""
        from mcp.server.auth.provider import construct_redirect_uri

        await self._exec("DELETE FROM requests WHERE id = ?", (request.id,))
        code = AuthorizationCode(
            code=secrets.token_urlsafe(32),
            scopes=[READ, WRITE] if allow_write else [READ],
            expires_at=time.time() + CODE_TTL,
            client_id=request.client.client_id or "",
            code_challenge=request.params.code_challenge,
            redirect_uri=request.params.redirect_uri,
            redirect_uri_provided_explicitly=request.params.redirect_uri_provided_explicitly,
            resource=request.params.resource,
            subject="owner",
        )
        await self._exec(
            "INSERT INTO codes(code, data, expires_at) VALUES (?, ?, ?)",
            (code.code, code.model_dump_json(), code.expires_at),
        )
        return construct_redirect_uri(
            str(request.params.redirect_uri), code=code.code, state=request.params.state
        )

    async def deny(self, request: PendingRequest) -> str:
        from mcp.server.auth.provider import construct_redirect_uri

        await self._exec("DELETE FROM requests WHERE id = ?", (request.id,))
        return construct_redirect_uri(
            str(request.params.redirect_uri), error="access_denied", state=request.params.state
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        row = await self._one(
            "SELECT data FROM codes WHERE code = ? AND expires_at >= ?",
            (authorization_code, time.time()),
        )
        if row is None:
            return None
        code = AuthorizationCode.model_validate_json(row["data"])
        return code if code.client_id == client.client_id else None

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        await self._exec("DELETE FROM codes WHERE code = ?", (authorization_code.code,))
        return await self._issue(
            authorization_code.client_id, authorization_code.scopes, authorization_code.resource
        )

    # ---------- tokens ----------

    async def _issue(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        now = time.time()
        conn = await self._c()
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        for token, kind, ttl in ((access, "access", ACCESS_TTL), (refresh, "refresh", REFRESH_TTL)):
            await conn.execute(
                "INSERT INTO tokens(hash, kind, client_id, scopes, resource, expires_at, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_hash(token), kind, client_id, " ".join(scopes), resource, now + ttl, now),
            )
        await conn.execute("DELETE FROM tokens WHERE expires_at < ?", (now,))
        await conn.commit()
        return OAuthToken(
            access_token=access,
            expires_in=ACCESS_TTL,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )

    async def _token(self, token: str, kind: str) -> aiosqlite.Row | None:
        return await self._one(
            "SELECT * FROM tokens WHERE hash = ? AND kind = ? AND expires_at >= ?",
            (_hash(token), kind, time.time()),
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        row = await self._token(refresh_token, "refresh")
        if row is None or row["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=row["client_id"],
            scopes=row["scopes"].split(),
            expires_at=int(row["expires_at"]),
            resource=row["resource"],
            subject="owner",
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        if any(s not in refresh_token.scopes for s in scopes):
            raise TokenError("invalid_scope", "cannot widen the scopes you approved")
        await self._exec("DELETE FROM tokens WHERE hash = ?", (_hash(refresh_token.token),))
        return await self._issue(
            refresh_token.client_id, scopes or refresh_token.scopes, refresh_token.resource
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = await self._token(token, "access")
        if row is None:
            return None
        return AccessToken(
            token=token,
            client_id=row["client_id"],
            scopes=row["scopes"].split(),
            expires_at=int(row["expires_at"]),
            resource=row["resource"],
            subject="owner",
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """RFC 7009: revoking either token ends the whole grant for that client."""
        await self._exec("DELETE FROM tokens WHERE client_id = ?", (token.client_id,))

    # ---------- management (`anam node activity / revoke / wipe`) ----------

    async def log(self, client_id: str | None, tool: str, detail: str) -> None:
        await self._exec(
            "INSERT INTO activity(ts, client_id, tool, detail) VALUES (?, ?, ?, ?)",
            (time.time(), client_id, tool, detail[:300]),
        )

    async def client_name(self, client_id: str | None) -> str:
        if client_id is None:
            return "unknown"
        client = await self.get_client(client_id)
        name = client.client_name if client else None
        return name or client_id[:8]

    async def activity(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self._all(
            "SELECT ts, client_id, tool, detail FROM activity ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [
            {
                "ts": r["ts"],
                "client": await self.client_name(r["client_id"]),
                "client_id": r["client_id"],
                "tool": r["tool"],
                "detail": r["detail"],
            }
            for r in rows
        ]

    async def connections(self) -> list[dict[str, Any]]:
        """Apps with a live grant (a refresh token that has not expired)."""
        rows = await self._all(
            "SELECT client_id, scopes, MAX(created_at) AS since FROM tokens "
            "WHERE kind = 'refresh' AND expires_at >= ? GROUP BY client_id ORDER BY since DESC",
            (time.time(),),
        )
        out = []
        for r in rows:
            last = await self._one(
                "SELECT MAX(ts) AS ts FROM activity WHERE client_id = ?", (r["client_id"],)
            )
            out.append(
                {
                    "client_id": r["client_id"],
                    "name": await self.client_name(r["client_id"]),
                    "can_save": WRITE in r["scopes"].split(),
                    "since": r["since"],
                    "last_used": last["ts"] if last else None,
                }
            )
        return out

    async def revoke(self, client: str | None) -> int:
        """Revoke one app (by client id or name, case-insensitive) or every app (None)."""
        targets = await self.connections()
        if client is not None:
            wanted = client.lower()
            targets = [
                c for c in targets if c["client_id"] == client or c["name"].lower() == wanted
            ]
        conn = await self._c()
        for c in targets:
            await conn.execute("DELETE FROM tokens WHERE client_id = ?", (c["client_id"],))
        await conn.commit()
        return len(targets)

    async def reset(self) -> None:
        conn = await self._c()
        for table in ("clients", "requests", "codes", "tokens", "activity"):
            await conn.execute(f"DELETE FROM {table}")
        await conn.commit()
