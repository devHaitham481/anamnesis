"""Scan the local history of every supported AI tool into shared memory (incrementally)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from anamnesis.core.memory import MemoryService
from anamnesis.core.models import ImportedConversation
from anamnesis.ingest.base import Source, file_signature

log = logging.getLogger(__name__)


@dataclass
class ScanReport:
    files_seen: int = 0
    files_parsed: int = 0
    conversations: int = 0
    messages_added: int = 0
    shared: int = 0  # messages published to other computers (cross-device sync)
    errors: list[str] = field(default_factory=list)
    by_source: dict[str, int] = field(default_factory=dict)

    def merge(self, other: ScanReport) -> None:
        self.files_seen += other.files_seen
        self.files_parsed += other.files_parsed
        self.conversations += other.conversations
        self.messages_added += other.messages_added
        self.errors += other.errors
        for k, v in other.by_source.items():
            self.by_source[k] = self.by_source.get(k, 0) + v


def default_sources() -> list[Source]:
    from anamnesis.ingest.claude_code import ClaudeCodeSource

    sources: list[Source] = [ClaudeCodeSource()]
    try:
        from anamnesis.ingest.codex import CodexSource
        from anamnesis.ingest.cursor import CursorSource
        from anamnesis.ingest.gemini_cli import GeminiCliSource

        sources += [CodexSource(), GeminiCliSource(), CursorSource()]
    except ImportError:  # pragma: no cover - optional sources not present
        pass
    return sources


def _parse_all(source: Source, path: Path) -> list[ImportedConversation]:
    return list(source.parse(path))


async def store(
    service: MemoryService, convs: Sequence[ImportedConversation], report: ScanReport
) -> None:
    for conv in convs:
        _, added = await service.import_conversation(conv, index=False)
        report.conversations += 1
        report.messages_added += added
        if added:
            report.by_source[conv.source] = report.by_source.get(conv.source, 0) + added


async def scan(
    service: MemoryService,
    sources: Sequence[Source] | None = None,
    force: bool = False,
    index: bool = True,
) -> ScanReport:
    """Import new/changed history from every source. Unchanged files are skipped cheaply.

    Text is stored first (keyword-searchable at once); with `index`, new messages are then
    embedded for semantic recall. Pass `index=False` to run embedding separately.
    """
    report = ScanReport()
    for source in sources if sources is not None else default_sources():
        try:
            paths = await asyncio.to_thread(source.discover)
        except Exception as e:
            log.warning("%s: discovery failed: %s", source.name, e)
            report.errors.append(f"{source.name}: {e}")
            continue
        for path in paths:
            report.files_seen += 1
            key = f"{source.name}:{path}"
            try:
                sig_fn = getattr(source, "signature", None)
                signature = sig_fn(path) if sig_fn else file_signature(path)
                if not force and await service.db.get_ingest_signature(key) == signature:
                    continue
                convs = await asyncio.to_thread(_parse_all, source, path)
                await store(service, convs, report)
                await service.db.set_ingest_signature(key, signature)
                report.files_parsed += 1
            except FileNotFoundError:
                continue  # deleted between discovery and parsing
            except Exception as e:
                log.warning("%s: failed to import %s: %s", source.name, path, e)
                report.errors.append(f"{path.name}: {e}")
    if index and report.messages_added:
        await service.backfill_index()
    return report


@contextlib.contextmanager
def sync_lock(data_dir: Path) -> Iterator[bool]:
    """Cross-process, non-blocking lock: yields True if this process may sync right now.

    Every open Claude Code / Cursor window starts its own MCP server; without this they would
    all import and embed the same history at once.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:  # Windows: no advisory locks; syncing twice is merely wasteful
        yield True
        return
    fd = os.open(data_dir / "sync.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


async def sync_once(service: MemoryService, sources: Sequence[Source] | None = None) -> ScanReport:
    """One background sync pass (import + embed), skipped if another process is syncing."""
    with sync_lock(service.settings.data_dir) as acquired:
        if not acquired:
            return ScanReport()
        report = await scan(service, sources, index=False)
        await exchange_with_devices(service, report)
        await service.backfill_index()  # also catches up on anything left from earlier runs
        return report


async def exchange_with_devices(service: MemoryService, report: ScanReport) -> None:
    """If cross-device sync is on, publish local memory and import other devices' memory."""
    from anamnesis.core.devices import exchange

    result = await exchange(service)
    report.shared += result.exported
    report.messages_added += result.imported
    for name, n in result.from_devices.items():
        key = f"device:{name}"
        report.by_source[key] = report.by_source.get(key, 0) + n
    report.errors += result.errors


async def watch(
    service: MemoryService,
    interval: float = 60.0,
    sources: Sequence[Source] | None = None,
    stop: asyncio.Event | None = None,
) -> None:
    """Re-scan forever (until `stop` is set) so new sessions show up while you work."""
    stop = stop or asyncio.Event()
    while not stop.is_set():
        try:
            report = await sync_once(service, sources)
            if report.messages_added:
                log.info("Imported %d new messages %s", report.messages_added, report.by_source)
        except Exception:
            log.exception("history scan failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass


async def import_file(service: MemoryService, path: Path, index: bool = True) -> ScanReport:
    """Import a data export (ChatGPT / claude.ai / Gemini Takeout), as .zip, .json or .html."""
    from anamnesis.ingest.exports import parse_export

    report = ScanReport(files_seen=1)
    convs = await asyncio.to_thread(lambda: list(parse_export(path)))
    await store(service, convs, report)
    report.files_parsed = 1
    if index:
        await service.backfill_index()
    return report
