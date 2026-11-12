"""Import data exports from web chat apps: ChatGPT, claude.ai (also covers Claude Desktop chats)
and Google Takeout "My Activity > Gemini Apps". Accepts the downloaded .zip or the JSON file."""

from __future__ import annotations

import html
import json
import re
import zipfile
from collections.abc import Iterator
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, cast

from anamnesis.core.models import ImportedConversation, ImportedMessage, Role
from anamnesis.ingest.base import text_of, to_epoch
from anamnesis.ingest.clean import clean_text

GEMINI_SESSION_GAP = 30 * 60  # Takeout has no conversation ids: split turns on 30-minute gaps


class ExportFormatError(ValueError):
    pass


def parse_export(path: Path) -> Iterator[ImportedConversation]:
    found = False
    for _name, data in _json_documents(path):
        kind = detect(data)
        if kind == "chatgpt":
            yield from parse_chatgpt(data)
        elif kind == "claude":
            yield from parse_claude(data)
        elif kind == "gemini":
            yield from parse_gemini_activity(data)
        else:
            continue
        found = True
    if not found:
        hint = ""
        if path.suffix.lower() == ".html" or (
            path.suffix.lower() == ".zip" and _zip_has(path, "MyActivity.html")
        ):
            hint = " For Gemini, re-export from Google Takeout with format JSON (not HTML)."
        raise ExportFormatError(
            f"{path.name}: no ChatGPT, claude.ai or Gemini (Takeout) conversations found.{hint}"
        )


def detect(data: Any) -> str | None:
    if not isinstance(data, list) or not data:
        return None
    first = next((x for x in data if isinstance(x, dict)), None)
    if first is None:
        return None
    if "mapping" in first:
        return "chatgpt"
    if "chat_messages" in first:
        return "claude"
    if (
        "header" in first
        and "time" in first
        and any(
            "Gemini" in str(x.get("header", "")) or "Gemini" in json.dumps(x.get("products", []))
            for x in data[:50]
            if isinstance(x, dict)
        )
    ):
        return "gemini"
    return None


# ---------- ChatGPT ----------

_CHATGPT_TEXT_TYPES = {"text", "multimodal_text"}


def parse_chatgpt(data: list[Any]) -> Iterator[ImportedConversation]:
    for conv in data:
        if not isinstance(conv, dict) or not isinstance(conv.get("mapping"), dict):
            continue
        mapping: dict[str, Any] = conv["mapping"]
        # The visible branch: walk up from the current node, then reverse.
        node_id = conv.get("current_node")
        chain: list[dict[str, Any]] = []
        seen: set[str] = set()
        while isinstance(node_id, str) and node_id in mapping and node_id not in seen:
            seen.add(node_id)
            node = mapping[node_id]
            chain.append(node)
            node_id = node.get("parent")
        if not chain:  # no current_node: fall back to mapping order
            chain = list(mapping.values())
        else:
            chain.reverse()
        created = to_epoch(conv.get("create_time"))
        messages = []
        for node in chain:
            msg = node.get("message") if isinstance(node, dict) else None
            if not isinstance(msg, dict):
                continue
            role = (msg.get("author") or {}).get("role")
            content = msg.get("content") or {}
            meta = msg.get("metadata") or {}
            if (
                role not in ("user", "assistant")
                or msg.get("recipient", "all") != "all"
                or meta.get("is_visually_hidden_from_conversation")
                or content.get("content_type") not in _CHATGPT_TEXT_TYPES
            ):
                continue
            parts = [p for p in content.get("parts") or [] if isinstance(p, str)]
            text = clean_text("\n\n".join(parts))
            if text:
                messages.append(
                    ImportedMessage(
                        external_id=str(msg.get("id") or node.get("id")),
                        role=cast(Role, role),
                        content=text,
                        created_at=to_epoch(msg.get("create_time"), created),
                        model=meta.get("model_slug") if role == "assistant" else None,
                    )
                )
        if messages:
            yield ImportedConversation(
                source="chatgpt",
                external_id=str(conv.get("conversation_id") or conv.get("id")),
                title=conv.get("title"),
                messages=messages,
            )


# ---------- claude.ai ----------


def parse_claude(data: list[Any]) -> Iterator[ImportedConversation]:
    for conv in data:
        if not isinstance(conv, dict) or not isinstance(conv.get("chat_messages"), list):
            continue
        created = to_epoch(conv.get("created_at"))
        messages = []
        for i, msg in enumerate(conv["chat_messages"]):
            if not isinstance(msg, dict):
                continue
            role = {"human": "user", "assistant": "assistant"}.get(msg.get("sender", ""))
            blocks = msg.get("content")
            text = text_of(blocks, ("text",)) if isinstance(blocks, list) and blocks else ""
            text = clean_text(text or str(msg.get("text") or ""))
            if role and text:
                messages.append(
                    ImportedMessage(
                        external_id=str(msg.get("uuid") or i),
                        role=cast(Role, role),
                        content=text,
                        created_at=to_epoch(msg.get("created_at"), created),
                    )
                )
        if messages:
            yield ImportedConversation(
                source="claude-web",
                external_id=str(conv.get("uuid")),
                title=conv.get("name") or None,
                messages=messages,
            )


# ---------- Gemini (Google Takeout, My Activity) ----------

_PROMPT_PREFIX = re.compile(r"^(Prompted|Eingegebener Prompt:|Prompt:|Requête :|Solicitaste)\s*")


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("p", "br", "li", "div", "h1", "h2", "h3", "tr"):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(markup: str) -> str:
    parser = _Text()
    parser.feed(markup)
    text = html.unescape("".join(parser.parts))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_gemini_activity(data: list[Any]) -> Iterator[ImportedConversation]:
    turns = []
    for entry in data:
        if not isinstance(entry, dict) or not isinstance(entry.get("title"), str):
            continue
        prompt = clean_text(_PROMPT_PREFIX.sub("", entry["title"]))
        if not prompt:
            continue
        reply = "\n\n".join(
            html_to_text(item["html"])
            for item in entry.get("safeHtmlItem") or []
            if isinstance(item, dict) and isinstance(item.get("html"), str)
        )
        turns.append((to_epoch(entry.get("time")), str(entry.get("time")), prompt, reply))
    turns.sort()
    session: list[tuple[float, str, str, str]] = []
    for turn in turns:
        if session and turn[0] - session[-1][0] > GEMINI_SESSION_GAP:
            yield _gemini_conversation(session)
            session = []
        session.append(turn)
    if session:
        yield _gemini_conversation(session)


def _gemini_conversation(session: list[tuple[float, str, str, str]]) -> ImportedConversation:
    messages = []
    for ts, key, prompt, reply in session:
        messages.append(
            ImportedMessage(external_id=f"{key}:u", role="user", content=prompt, created_at=ts)
        )
        if reply:
            messages.append(
                ImportedMessage(
                    external_id=f"{key}:a", role="assistant", content=reply, created_at=ts + 0.001
                )
            )
    return ImportedConversation(
        source="gemini-web",
        external_id=f"session:{session[0][1]}",
        title=None,
        messages=messages,
    )


# ---------- file handling ----------


def _json_documents(path: Path) -> Iterator[tuple[str, Any]]:
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                base = name.rsplit("/", 1)[-1]
                if (
                    base.startswith("conversations")
                    and base.endswith(".json")
                    or (base == "MyActivity.json" and "Gemini" in name)
                ):
                    with zf.open(name) as fh:
                        yield name, _load(fh.read())
        return
    if path.suffix.lower() == ".json":
        yield path.name, _load(path.read_bytes())


def _load(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8-sig", errors="replace"))
    except json.JSONDecodeError as e:
        raise ExportFormatError(f"not valid JSON: {e}") from e


def _zip_has(path: Path, basename: str) -> bool:
    try:
        with zipfile.ZipFile(path) as zf:
            return any(n.endswith(basename) for n in zf.namelist())
    except zipfile.BadZipFile:
        return False
