"""Cross-device memory sync through a shared store: a synced folder or your own Anamnesis node.

Every device writes only its own stream of immutable, end-to-end encrypted segments and reads
everyone else's, importing them with the same idempotent upsert used for local history. So
merging is just "import what's new", and nothing can conflict.

Two kinds of store hold the segments, with the same layout:

- a folder you already sync (iCloud Drive, Dropbox, Syncthing…): `<folder>/anamnesis-memory/`
- an Anamnesis node (`anam node serve`), reached over HTTPS; see `src/node`.

    meta.json                     format version + key check (no secrets)
    <device-id>/device.json       device name (plain) for `anam devices`
    <device-id>/000001.nxs ...    immutable, AES-256-GCM encrypted segments

The key never touches the store: it travels only in the pairing code (`anam share` /
`anam join`), or is handed out by the node after you sign in with its password.
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import hmac
import json
import logging
import os
import platform
import re
import secrets
import shutil
import subprocess
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from src.core.memory import MemoryService
from src.core.models import ImportedConversation, ImportedMessage, Thread

log = logging.getLogger(__name__)

FORMAT = 1
MAGIC = b"NXS1"
ROOT = "anamnesis-memory"
SEGMENT_MESSAGES = 1000
_KEY_CHECK = b"anamnesis-memory-sync-v1"
_NODE_TOKEN = b"anamnesis-node-sync-v1"
DEVICE_ID = re.compile(r"^[0-9a-f]{12}$")
HTTP_TIMEOUT = 30.0


class SyncSetupError(RuntimeError):
    pass


def default_folder() -> Path:
    icloud = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
    return (icloud if icloud.is_dir() else Path.home()) / "Anamnesis"


# ---------- identity & config (stored in the local data dir, never in the store) ----------


@dataclass(frozen=True)
class Device:
    id: str
    name: str


def this_device(data_dir: Path) -> Device:
    path = data_dir / "device.json"
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
        return Device(data["id"], data["name"])
    device = Device(uuid.uuid4().hex[:12], platform.node().split(".")[0] or "device")
    data_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"id": device.id, "name": device.name}), encoding="utf-8")
    return device


@dataclass(frozen=True)
class Scope:
    """What this computer publishes to the store (and so to the node and other computers)."""

    mode: Literal["all", "notes"] = "all"
    exclude_projects: tuple[str, ...] = ()

    def allows(self, thread: Thread) -> bool:
        if self.mode == "notes" and thread.source != "note":
            return False
        project = thread.project or ""
        return not any(
            project == p or project.startswith(p.rstrip("/") + "/") for p in self.exclude_projects
        )

    def to_json(self) -> dict[str, Any]:
        return {"mode": self.mode, "exclude_projects": list(self.exclude_projects)}

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> Scope:
        data = data or {}
        mode: Literal["all", "notes"] = "notes" if data.get("mode") == "notes" else "all"
        return cls(mode, tuple(str(p) for p in data.get("exclude_projects", [])))

    def describe(self) -> str:
        what = "notes only" if self.mode == "notes" else "everything"
        if self.exclude_projects:
            what += " except " + ", ".join(self.exclude_projects)
        return what


@dataclass(frozen=True)
class SyncConfig:
    key: bytes
    folder: Path | None = None
    node: str | None = None  # base URL of an Anamnesis node
    scope: Scope = field(default_factory=Scope)

    @property
    def root(self) -> Path:
        if self.folder is None:
            raise ValueError("not a folder store")
        return self.folder / ROOT

    @property
    def where(self) -> str:
        return self.node or str(self.folder)

    def cursor_prefix(self) -> str:
        """Sync progress is per store: a node numbers segments independently of a folder, and a
        wiped node (new key) starts from scratch even at the same address."""
        if self.node is None:
            return "sync:"  # the original (folder-only) key names
        store_id = hashlib.sha256(f"{self.node}\0{key_check(self.key)}".encode()).hexdigest()
        return f"sync:node:{store_id[:16]}:"


def _config_path(data_dir: Path) -> Path:
    return data_dir / "sync.json"


def load_config(data_dir: Path) -> SyncConfig | None:
    path = _config_path(data_dir)
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return SyncConfig(
        key=decode_code(data["code"]),
        folder=Path(data["folder"]) if data.get("folder") else None,
        node=data.get("node"),
        scope=Scope.from_json(data.get("scope")),
    )


def save_config(data_dir: Path, cfg: SyncConfig) -> None:
    path = _config_path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {"code": encode_code(cfg.key), "scope": cfg.scope.to_json()}
    if cfg.folder is not None:
        data["folder"] = str(cfg.folder)
    if cfg.node is not None:
        data["node"] = cfg.node
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.chmod(0o600)  # holds the encryption key
    os.replace(tmp, path)


def disable(data_dir: Path) -> bool:
    path = _config_path(data_dir)
    if path.is_file():
        path.unlink()
        return True
    return False


# ---------- keys & codes ----------


def encode_code(key: bytes) -> str:
    text = base64.b32encode(key).decode().rstrip("=")
    return "-".join(text[i : i + 4] for i in range(0, len(text), 4))


def decode_code(code: str) -> bytes:
    text = "".join(ch for ch in code.upper() if ch.isalnum())
    text += "=" * (-len(text) % 8)
    try:
        key = base64.b32decode(text)
    except ValueError as e:
        raise SyncSetupError("That pairing code is not valid.") from e
    if len(key) != 32:
        raise SyncSetupError("That pairing code is not valid (wrong length).")
    return key


def key_check(key: bytes) -> str:
    return hmac.new(key, _KEY_CHECK, hashlib.sha256).hexdigest()


def node_token(key: bytes) -> str:
    """Bearer token a linked computer uses for the node's sync API (derived, not the key)."""
    return hmac.new(key, _NODE_TOKEN, hashlib.sha256).hexdigest()


# ---------- stores ----------


class SegmentStore(Protocol):
    def meta(self) -> dict[str, Any] | None: ...
    def register(self, device: Device) -> None: ...
    def list_devices(self) -> list[dict[str, Any]]: ...
    def next_seq(self, device_id: str) -> int: ...
    def read(self, device_id: str, seq: int) -> bytes | None: ...
    def write(self, device_id: str, seq: int, blob: bytes) -> None: ...


def _atomic_write(path: Path, data: str | bytes) -> None:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_bytes(data.encode() if isinstance(data, str) else data)
    os.replace(tmp, path)


def _segments(device_dir: Path) -> list[tuple[int, Path]]:
    out: list[tuple[int, Path]] = []
    for p in device_dir.glob("*.nxs"):
        if p.stem.isdigit():
            out.append((int(p.stem), p))
    return sorted(out)


def _materialize(path: Path) -> None:
    """iCloud may keep only a placeholder (`.name.icloud`); ask macOS to download it."""
    placeholder = path.with_name(f".{path.name}.icloud")
    if not path.exists() and placeholder.exists() and shutil.which("brctl"):
        subprocess.run(["brctl", "download", str(path)], capture_output=True, timeout=30)


class FolderStore:
    """Segments in a directory: a synced folder on a computer, or the node's own disk."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def meta(self) -> dict[str, Any] | None:
        path = self.root / "meta.json"
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None

    def create(self, key: bytes) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        meta = {"format": FORMAT, "check": key_check(key)}
        _atomic_write(self.root / "meta.json", json.dumps(meta))

    def _dir(self, device_id: str) -> Path:
        if not DEVICE_ID.match(device_id):
            raise ValueError(f"invalid device id {device_id!r}")
        return self.root / device_id

    def register(self, device: Device) -> None:
        own = self._dir(device.id)
        own.mkdir(parents=True, exist_ok=True)
        _atomic_write(own / "device.json", json.dumps({"id": device.id, "name": device.name}))

    def device_ids(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if p.is_dir() and DEVICE_ID.match(p.name))

    def list_devices(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for device_id in self.device_ids():
            d = self.root / device_id
            info: dict[str, Any] = {"id": device_id, "name": device_id}
            meta = d / "device.json"
            if meta.is_file():
                try:
                    info["name"] = json.loads(meta.read_text(encoding="utf-8")).get("name", d.name)
                except ValueError:
                    pass
            segs = _segments(d)
            info["segments"] = len(segs)
            info["last_write"] = segs[-1][1].stat().st_mtime if segs else None
            out.append(info)
        return out

    def next_seq(self, device_id: str) -> int:
        segs = _segments(self._dir(device_id))
        return (segs[-1][0] if segs else 0) + 1

    def read(self, device_id: str, seq: int) -> bytes | None:
        path = self._dir(device_id) / f"{seq:06d}.nxs"
        if not path.exists():
            _materialize(path)
        return path.read_bytes() if path.exists() else None

    def write(self, device_id: str, seq: int, blob: bytes) -> None:
        own = self._dir(device_id)
        own.mkdir(parents=True, exist_ok=True)
        _atomic_write(own / f"{seq:06d}.nxs", blob)


class NodeStore:
    """Segments held by an Anamnesis node, over its HTTPS sync API."""

    def __init__(self, url: str, key: bytes) -> None:
        self.url = url.rstrip("/")
        self.token = node_token(key)

    def _request(
        self, method: str, path: str, body: bytes | None = None, content_type: str | None = None
    ) -> bytes | None:
        req = urllib.request.Request(self.url + path, data=body, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        if content_type:
            req.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return bytes(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            detail = e.read().decode(errors="replace")[:200]
            raise OSError(f"node returned {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise OSError(f"node unreachable: {e.reason}") from e

    def api(self, method: str, path: str, data: Any = None) -> Any:
        body = json.dumps(data).encode() if data is not None else None
        raw = self._request(method, path, body, "application/json" if body else None)
        return json.loads(raw) if raw else None

    def meta(self) -> dict[str, Any] | None:
        data = self.api("GET", "/sync/meta")
        return data if isinstance(data, dict) else None

    def register(self, device: Device) -> None:
        self.api("PUT", f"/sync/devices/{device.id}", {"name": device.name})

    def list_devices(self) -> list[dict[str, Any]]:
        return list(self.api("GET", "/sync/devices") or [])

    def next_seq(self, device_id: str) -> int:
        for d in self.list_devices():
            if d["id"] == device_id:
                return int(d["segments"]) + 1
        return 1

    def read(self, device_id: str, seq: int) -> bytes | None:
        return self._request("GET", f"/sync/devices/{device_id}/{seq}")

    def write(self, device_id: str, seq: int, blob: bytes) -> None:
        self._request("PUT", f"/sync/devices/{device_id}/{seq}", blob, "application/octet-stream")


def open_store(cfg: SyncConfig) -> SegmentStore:
    if cfg.node is not None:
        return NodeStore(cfg.node, cfg.key)
    return FolderStore(cfg.root)


# ---------- setup: folder ----------


def share(data_dir: Path, folder: Path) -> str:
    """Turn on folder sync for this device; returns the pairing code for other devices.

    The first device creates the key. If the folder already holds Anamnesis memory from another
    device, use `join` with that device's code instead.
    """
    existing = load_config(data_dir)
    store = FolderStore(folder / ROOT)
    if existing is not None and existing.folder == folder:
        return encode_code(existing.key)
    if store.meta() is not None:
        raise SyncSetupError(
            f"{store.root} already holds Anamnesis memory from another device. "
            "Run `anam share` there to see its code, then `anam join <code>` here."
        )
    key = secrets.token_bytes(32)
    store.create(key)
    save_config(data_dir, SyncConfig(key=key, folder=folder))
    store.register(this_device(data_dir))
    return encode_code(key)


def join(data_dir: Path, folder: Path, code: str) -> None:
    key = decode_code(code)
    store = FolderStore(folder / ROOT)
    meta = store.meta()
    if meta is None:
        raise SyncSetupError(
            f"No Anamnesis memory found in {store.root}. Wait for the folder to finish syncing, "
            "or check that you chose the same folder as on your other device."
        )
    if not hmac.compare_digest(str(meta.get("check", "")), key_check(key)):
        raise SyncSetupError("That pairing code does not match the memory in this folder.")
    save_config(data_dir, SyncConfig(key=key, folder=folder))
    store.register(this_device(data_dir))


# ---------- setup: node ----------


def normalize_node_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if "://" not in url:
        url = "https://" + url
    if url.startswith("http://") and not re.match(r"^http://(localhost|127\.0\.0\.1)(:|/|$)", url):
        raise SyncSetupError("Use https:// for a node (http:// is only allowed for localhost).")
    return url


def fetch_node_key(url: str, password: str) -> bytes:
    """Sign in to a node with its password; returns the memory key it hands to linked devices."""
    req = urllib.request.Request(
        url + "/sync/link",
        data=json.dumps({"password": password}).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise SyncSetupError("Wrong node password.") from e
        if e.code == 429:
            raise SyncSetupError("Too many attempts. Wait a minute and try again.") from e
        raise SyncSetupError(f"The node answered {e.code}. Is {url} an Anamnesis node?") from e
    except (urllib.error.URLError, ValueError) as e:
        raise SyncSetupError(f"Could not reach an Anamnesis node at {url}: {e}") from e
    return decode_code(str(data["code"]))


def link_node(data_dir: Path, url: str, password: str, scope: Scope | None = None) -> SyncConfig:
    """Make this computer sync through a node (replacing folder sync, if any)."""
    url = normalize_node_url(url)
    key = fetch_node_key(url, password)
    cfg = SyncConfig(key=key, node=url, scope=scope or Scope())
    store = NodeStore(url, key)
    try:
        meta = store.meta()
        if meta is None or not hmac.compare_digest(str(meta.get("check", "")), key_check(key)):
            raise SyncSetupError("The node's memory does not match the key it sent.")
        store.register(this_device(data_dir))
    except OSError as e:
        raise SyncSetupError(str(e)) from e
    save_config(data_dir, cfg)
    return cfg


def set_scope(data_dir: Path, scope: Scope) -> SyncConfig:
    """Change what this computer publishes. Its export restarts so the new scope applies to
    everything already stored (imports are idempotent, so nothing is duplicated)."""
    cfg = load_config(data_dir)
    if cfg is None:
        raise SyncSetupError("Memory sync is off.")
    cfg = SyncConfig(key=cfg.key, folder=cfg.folder, node=cfg.node, scope=scope)
    save_config(data_dir, cfg)
    (data_dir / "sync-reset").write_text("export")  # picked up by the next exchange()
    return cfg


def list_devices(cfg: SyncConfig) -> list[dict[str, Any]]:
    return open_store(cfg).list_devices()


# ---------- segments ----------


def encrypt(key: bytes, device_id: str, records: list[dict[str, Any]]) -> bytes:
    body = gzip.compress("\n".join(json.dumps(r) for r in records).encode())
    nonce = secrets.token_bytes(12)
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, body, device_id.encode())


def decrypt(key: bytes, device_id: str, blob: bytes) -> list[dict[str, Any]]:
    if not blob.startswith(MAGIC):
        raise ValueError("not an Anamnesis segment")
    nonce, ct = blob[4:16], blob[16:]
    try:
        body = AESGCM(key).decrypt(nonce, ct, device_id.encode())
    except InvalidTag as e:
        raise ValueError("cannot decrypt (different pairing key, or the file was altered)") from e
    lines = gzip.decompress(body).decode().splitlines()
    return [json.loads(line) for line in lines if line.strip()]


# ---------- exchange ----------


@dataclass
class ExchangeReport:
    exported: int = 0
    imported: int = 0
    from_devices: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


async def exchange(service: MemoryService, cfg: SyncConfig | None = None) -> ExchangeReport:
    """Publish this device's new memory to the store and import everyone else's."""
    data_dir = service.settings.data_dir
    cfg = cfg or load_config(data_dir)
    report = ExchangeReport()
    if cfg is None:
        return report
    if cfg.node is None and not cfg.root.is_dir():
        report.errors.append(f"sync folder {cfg.root} is not available")
        return report
    store = open_store(cfg)
    device = this_device(data_dir)
    try:
        await asyncio.to_thread(store.register, device)
        devices = await asyncio.to_thread(store.list_devices)
    except OSError as e:
        report.errors.append(f"{cfg.where}: {e}")
        return report
    reset = data_dir / "sync-reset"
    if reset.exists():
        await service.db.set_ingest_signature(cfg.cursor_prefix() + "export", "0")
        reset.unlink()
    try:
        report.exported = await _export(service, cfg, store, device)
    except Exception as e:
        log.exception("memory export failed")
        report.errors.append(f"export: {e}")
    for d in devices:
        if d["id"] == device.id:
            continue
        n, error = await _import_device(service, cfg, store, d["id"])
        if error is not None:
            log.warning("memory import from %s stopped: %s", d["name"], error)
            report.errors.append(f"{d['name']}: {error}")
        if n:
            report.imported += n
            report.from_devices[d["name"]] = report.from_devices.get(d["name"], 0) + n
    return report


async def _export(
    service: MemoryService, cfg: SyncConfig, store: SegmentStore, device: Device
) -> int:
    db = service.db
    state_key = cfg.cursor_prefix() + "export"
    cursor = int(await db.get_ingest_signature(state_key) or 0)
    exported = 0
    while True:
        rows = await db.local_changes_after(cursor, limit=SEGMENT_MESSAGES)
        if not rows:
            return exported
        convs: dict[str, dict[str, Any]] = {}
        for thread, msg, ext in rows:
            if not cfg.scope.allows(thread):
                continue
            external_id = thread.external_id or await db.ensure_external_id(
                thread.id, f"{device.id}:{thread.id}"
            )
            key = f"{thread.source}\0{external_id}"
            conv = convs.setdefault(
                key,
                {
                    "source": thread.source,
                    "external_id": external_id,
                    "title": thread.title,
                    "project": thread.project,
                    "messages": [],
                },
            )
            conv["messages"].append(
                {
                    "external_id": ext or f"{device.id}:{msg.id}",
                    "role": msg.role,
                    "content": msg.content,
                    "created_at": msg.created_at,
                    "model": msg.model,
                }
            )
        if convs:
            seq = await asyncio.to_thread(store.next_seq, device.id)
            records = [{"type": "conversation", **c} for c in convs.values()]
            blob = encrypt(cfg.key, device.id, records)
            await asyncio.to_thread(store.write, device.id, seq, blob)
            exported += sum(len(c["messages"]) for c in convs.values())
        cursor = rows[-1][1].id
        await db.set_ingest_signature(state_key, str(cursor))


async def _import_device(
    service: MemoryService, cfg: SyncConfig, store: SegmentStore, device_id: str
) -> tuple[int, str | None]:
    """Import another device's segments in order. Returns (messages added, error or None).

    Progress is committed per segment, so an unreadable segment stops this device's import
    there (reported, retried next time) without losing what came before it.
    """
    db = service.db
    state_key = f"{cfg.cursor_prefix()}device:{device_id}"
    seq = int(await db.get_ingest_signature(state_key) or 0) + 1
    added_total = 0
    while True:
        try:
            blob = await asyncio.to_thread(store.read, device_id, seq)
        except OSError as e:
            return added_total, f"segment {seq}: {e}"
        if blob is None:
            # Not synced yet (or not written yet): stop here, never skip a gap.
            return added_total, None
        try:
            records = decrypt(cfg.key, device_id, blob)
        except (ValueError, OSError) as e:
            return added_total, f"{seq:06d}.nxs: {e}"
        for rec in records:
            if rec.get("type") != "conversation":
                continue  # newer record kinds from a newer Anamnesis: ignore, don't fail
            conv = ImportedConversation(
                source=rec["source"],
                external_id=rec["external_id"],
                title=rec.get("title"),
                project=rec.get("project"),
                messages=[ImportedMessage(**m) for m in rec.get("messages", [])],
            )
            _, added = await service.import_conversation(conv, index=False, origin=device_id)
            added_total += added
        await db.set_ingest_signature(state_key, str(seq))
        seq += 1
