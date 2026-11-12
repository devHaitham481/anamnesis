"""Cross-device memory sync through a folder you already sync (iCloud Drive, Dropbox, ...).

Layout inside the folder (`<folder>/anamnesis-memory/`):

    meta.json                     format version + key check (no secrets)
    <device-id>/device.json       device name (plain) for `anam devices`
    <device-id>/000001.nxs ...    immutable, end-to-end encrypted segments

Every device only ever writes its own subfolder, so file-sync services never see write
conflicts. Each device reads every other device's segments in order and imports them with the
same idempotent upsert used for local history, so merging is just "import what's new".
Segments are AES-256-GCM encrypted with a key that never touches the folder: it travels only
in the pairing code shown by `anam share` and typed into `anam join`.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import json
import logging
import os
import platform
import secrets
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from src.core.memory import MemoryService
from src.core.models import ImportedConversation, ImportedMessage

log = logging.getLogger(__name__)

FORMAT = 1
MAGIC = b"NXS1"
ROOT = "anamnesis-memory"
SEGMENT_MESSAGES = 1000
_KEY_CHECK = b"anamnesis-memory-sync-v1"


class SyncSetupError(RuntimeError):
    pass


def default_folder() -> Path:
    icloud = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
    return (icloud if icloud.is_dir() else Path.home()) / "Anamnesis"


# ---------- identity & config (stored in the local data dir, never in the shared folder) ----


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
class SyncConfig:
    folder: Path
    key: bytes

    @property
    def root(self) -> Path:
        return self.folder / ROOT


def _config_path(data_dir: Path) -> Path:
    return data_dir / "sync.json"


def load_config(data_dir: Path) -> SyncConfig | None:
    path = _config_path(data_dir)
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return SyncConfig(Path(data["folder"]), decode_code(data["code"]))


def _save_config(data_dir: Path, cfg: SyncConfig) -> None:
    path = _config_path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"folder": str(cfg.folder), "code": encode_code(cfg.key)}), encoding="utf-8"
    )
    path.chmod(0o600)  # holds the encryption key


def disable(data_dir: Path) -> bool:
    path = _config_path(data_dir)
    if path.is_file():
        path.unlink()
        return True
    return False


# ---------- pairing code ----------


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


def _key_check(key: bytes) -> str:
    return hmac.new(key, _KEY_CHECK, hashlib.sha256).hexdigest()


def _read_meta(root: Path) -> dict[str, Any] | None:
    path = root / "meta.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None


def share(data_dir: Path, folder: Path) -> str:
    """Turn on sync for this device in `folder`; returns the pairing code for other devices.

    The first device creates the key. If the folder already holds Anamnesis memory from another
    device, use `join` with that device's code instead.
    """
    existing = load_config(data_dir)
    root = folder / ROOT
    meta = _read_meta(root)
    if existing is not None and existing.folder == folder:
        return encode_code(existing.key)
    if meta is not None:
        raise SyncSetupError(
            f"{root} already holds Anamnesis memory from another device. "
            "Run `anam share` there to see its code, then `anam join <code>` here."
        )
    key = secrets.token_bytes(32)
    root.mkdir(parents=True, exist_ok=True)
    _atomic_write(root / "meta.json", json.dumps({"format": FORMAT, "check": _key_check(key)}))
    _save_config(data_dir, SyncConfig(folder, key))
    _register(root, this_device(data_dir))
    return encode_code(key)


def join(data_dir: Path, folder: Path, code: str) -> None:
    key = decode_code(code)
    root = folder / ROOT
    meta = _read_meta(root)
    if meta is None:
        raise SyncSetupError(
            f"No Anamnesis memory found in {root}. Wait for the folder to finish syncing, or check "
            "that you chose the same folder as on your other device."
        )
    if not hmac.compare_digest(str(meta.get("check", "")), _key_check(key)):
        raise SyncSetupError("That pairing code does not match the memory in this folder.")
    _save_config(data_dir, SyncConfig(folder, key))
    _register(root, this_device(data_dir))


def _register(root: Path, device: Device) -> None:
    own = root / device.id
    own.mkdir(parents=True, exist_ok=True)
    _atomic_write(own / "device.json", json.dumps({"id": device.id, "name": device.name}))


def list_devices(cfg: SyncConfig) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not cfg.root.is_dir():
        return out
    for d in sorted(p for p in cfg.root.iterdir() if p.is_dir()):
        info: dict[str, Any] = {"id": d.name, "name": d.name, "segments": len(_segments(d))}
        meta = d / "device.json"
        if meta.is_file():
            try:
                info["name"] = json.loads(meta.read_text(encoding="utf-8")).get("name", d.name)
            except ValueError:
                pass
        segs = _segments(d)
        info["last_write"] = segs[-1][1].stat().st_mtime if segs else None
        out.append(info)
    return out


# ---------- segments ----------


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


def encrypt(key: bytes, device_id: str, records: list[dict[str, Any]]) -> bytes:
    body = gzip.compress("\n".join(json.dumps(r) for r in records).encode())
    nonce = secrets.token_bytes(12)
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, body, device_id.encode())


def decrypt(key: bytes, device_id: str, blob: bytes) -> list[dict[str, Any]]:
    if not blob.startswith(MAGIC):
        raise ValueError("not a Anamnesis segment")
    nonce, ct = blob[4:16], blob[16:]
    try:
        body = AESGCM(key).decrypt(nonce, ct, device_id.encode())
    except InvalidTag as e:
        raise ValueError("cannot decrypt (different pairing key, or the file was altered)") from e
    lines = gzip.decompress(body).decode().splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _materialize(path: Path) -> None:
    """iCloud may keep only a placeholder (`.name.icloud`); ask macOS to download it."""
    placeholder = path.with_name(f".{path.name}.icloud")
    if not path.exists() and placeholder.exists() and shutil.which("brctl"):
        subprocess.run(["brctl", "download", str(path)], capture_output=True, timeout=30)


# ---------- exchange ----------


@dataclass
class ExchangeReport:
    exported: int = 0
    imported: int = 0
    from_devices: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


async def exchange(service: MemoryService, cfg: SyncConfig | None = None) -> ExchangeReport:
    """Publish this device's new memory to the folder and import everyone else's."""
    data_dir = service.settings.data_dir
    cfg = cfg or load_config(data_dir)
    report = ExchangeReport()
    if cfg is None:
        return report
    if not cfg.root.is_dir():
        report.errors.append(f"sync folder {cfg.root} is not available")
        return report
    device = this_device(data_dir)
    _register(cfg.root, device)
    try:
        report.exported = await _export(service, cfg, device)
    except Exception as e:
        log.exception("memory export failed")
        report.errors.append(f"export: {e}")
    names = {d["id"]: d["name"] for d in list_devices(cfg)}
    for device_dir in sorted(p for p in cfg.root.iterdir() if p.is_dir()):
        if device_dir.name == device.id:
            continue
        name = names.get(device_dir.name, device_dir.name)
        n, error = await _import_device(service, cfg, device_dir)
        if error is not None:
            log.warning("memory import from %s stopped: %s", name, error)
            report.errors.append(f"{name}: {error}")
        if n:
            report.imported += n
            report.from_devices[name] = report.from_devices.get(name, 0) + n
    return report


async def _export(service: MemoryService, cfg: SyncConfig, device: Device) -> int:
    db = service.db
    cursor = int(await db.get_ingest_signature("sync:export") or 0)
    own = cfg.root / device.id
    exported = 0
    while True:
        rows = await db.local_changes_after(cursor, limit=SEGMENT_MESSAGES)
        if not rows:
            return exported
        convs: dict[str, dict[str, Any]] = {}
        for thread, msg, ext in rows:
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
        segs = _segments(own)
        seq = (segs[-1][0] if segs else 0) + 1
        records = [{"type": "conversation", **c} for c in convs.values()]
        _atomic_write(own / f"{seq:06d}.nxs", encrypt(cfg.key, device.id, records))
        cursor = rows[-1][1].id
        await db.set_ingest_signature("sync:export", str(cursor))
        exported += len(rows)


async def _import_device(
    service: MemoryService, cfg: SyncConfig, device_dir: Path
) -> tuple[int, str | None]:
    """Import another device's segments in order. Returns (messages added, error or None).

    Progress is committed per segment, so an unreadable segment stops this device's import
    there (reported, retried next time) without losing what came before it.
    """
    db = service.db
    state_key = f"sync:device:{device_dir.name}"
    done = int(await db.get_ingest_signature(state_key) or 0)
    added_total = 0
    seq = done + 1
    while True:
        path = device_dir / f"{seq:06d}.nxs"
        if not path.exists():
            _materialize(path)
        if not path.exists():
            # Not synced yet (or not written yet): stop here, never skip a gap.
            return added_total, None
        try:
            records = decrypt(cfg.key, device_dir.name, path.read_bytes())
        except (ValueError, OSError) as e:
            return added_total, f"{path.name}: {e}"
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
            _, added = await service.import_conversation(conv, index=False, origin=device_dir.name)
            added_total += added
        await db.set_ingest_signature(state_key, str(seq))
        seq += 1
