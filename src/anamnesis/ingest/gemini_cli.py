"""Gemini CLI: ~/.gemini/tmp/<project>/chats/session-*.jsonl (append-only log, replayed here).

Per google-gemini/gemini-cli `chatRecordingService`: a metadata line, message lines keyed by
`id` (later lines replace earlier ones), plus `$set`, `$patch` and `$rewindTo` operations.
Legacy `session-*.json` files hold one ConversationRecord object.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

from anamnesis.core.models import ImportedConversation, ImportedMessage, Role
from anamnesis.ingest.base import to_epoch
from anamnesis.ingest.clean import clean_text


def gemini_roots() -> list[Path]:
    home = Path.home()
    return [home / ".gemini" / "tmp", home / ".cache" / ".gemini" / "tmp"]


def gemini_text(content: Any) -> str:
    """Concatenate the visible text of a genai PartListUnion (skipping thoughts/function calls)."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        content = [content]
    if not isinstance(content, list):
        return ""
    parts = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif (
            isinstance(part, dict) and not part.get("thought") and isinstance(part.get("text"), str)
        ):
            parts.append(part["text"])
    return "".join(parts)


class GeminiCliSource:
    name = "gemini-cli"

    def __init__(self, roots: list[Path] | None = None) -> None:
        self.roots = roots or gemini_roots()

    def discover(self) -> list[Path]:
        found: list[Path] = []
        for root in self.roots:
            if root.is_dir():
                found += root.glob("*/chats/session-*.jsonl")
                found += root.glob("*/chats/session-*.json")
        return sorted(found)

    def _project(self, path: Path) -> str | None:
        project_dir = path.parent.parent
        marker = project_dir / ".project_root"
        if marker.is_file():
            value = marker.read_text(encoding="utf-8", errors="replace").strip()
            if value:
                return value
        registry = project_dir.parent.parent / "projects.json"  # ~/.gemini/projects.json
        if registry.is_file():
            try:
                projects = json.loads(registry.read_text(encoding="utf-8")).get("projects", {})
            except (ValueError, AttributeError):
                projects = {}
            for abs_path, slug in projects.items():
                if slug == project_dir.name:
                    return str(abs_path)
        return None

    def parse(self, path: Path) -> Iterator[ImportedConversation]:
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix == ".json":
            try:
                record = json.loads(text)
            except json.JSONDecodeError:
                return
            meta, messages = record, list(record.get("messages") or [])
        else:
            meta, messages = _replay(text)
        if meta.get("kind") == "subagent":
            return
        out = []
        for m in messages:
            role = {"user": "user", "gemini": "assistant"}.get(m.get("type", ""))
            body = clean_text(gemini_text(m.get("content")))
            if role and body and m.get("id"):
                out.append(
                    ImportedMessage(
                        external_id=str(m["id"]),
                        role=cast(Role, role),
                        content=body,
                        created_at=to_epoch(m.get("timestamp"), to_epoch(meta.get("startTime"))),
                        model=m.get("model") if role == "assistant" else None,
                    )
                )
        if out:
            yield ImportedConversation(
                source=self.name,
                external_id=str(meta.get("sessionId") or path.stem),
                title=meta.get("summary"),
                project=self._project(path),
                messages=out,
            )


def _replay(text: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    meta: dict[str, Any] = {}
    order: list[str] = []
    msgs: dict[str, dict[str, Any]] = {}
    for line in text.splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        if "$set" in rec and isinstance(rec["$set"], dict):
            update = dict(rec["$set"])
            if isinstance(update.get("messages"), list):
                msgs = {str(m.get("id")): m for m in update.pop("messages") if isinstance(m, dict)}
                order = list(msgs)
            meta.update(update)
        elif "$patch" in rec and isinstance(rec["$patch"], dict):
            patch = rec["$patch"]
            mid = patch.get("id")
            if mid is not None and str(mid) in msgs and "content" in patch:
                msgs[str(mid)] = {**msgs[str(mid)], "content": patch["content"]}
            for update in patch.get("updates") or []:
                if isinstance(update, dict) and str(update.get("id")) in msgs:
                    msgs[str(update["id"])] = {**msgs[str(update["id"])], **update}
            for rid in patch.get("removeIds") or []:
                msgs.pop(str(rid), None)
            if isinstance(patch.get("orderIds"), list):
                order = [str(i) for i in patch["orderIds"]]
        elif "$rewindTo" in rec:
            target = str(rec["$rewindTo"])
            if target in order:
                for rid in order[order.index(target) :]:
                    msgs.pop(rid, None)
                order = order[: order.index(target)]
        elif "sessionId" in rec and "type" not in rec:
            meta.update(rec)
        elif "id" in rec and "type" in rec:
            mid = str(rec["id"])
            if mid not in msgs:
                order.append(mid)
            msgs[mid] = rec
    ordered = [msgs[i] for i in order if i in msgs]
    ordered += [m for i, m in msgs.items() if i not in order]
    return meta, ordered
