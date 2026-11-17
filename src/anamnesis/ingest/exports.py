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

GEMINI_SESSION_GAP = 30 * 60  # Takeout records without a chat link: split on gaps
ATTACHMENT_CHARS = 20_000  # pasted files and documents can be huge


class ExportFormatError(ValueError):
    pass


def parse_export(path: Path) -> Iterator[ImportedConversation]:
    found = False
    for _name, data in _json_documents(path):
        if isinstance(data, dict) and isinstance(data.get("data_files"), list):
            raise ExportFormatError(
                f"{path.name} is the download list of a large claude.ai export: download the "
                "batch .zip files it links to and import those."
            )
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
# Web-search citations are private-use markers, e.g. "\ue200cite\ue202turn0search0\ue201".
_CITATION = re.compile(r"\s*\ue200[^\ue201]*\ue201|[\ue200-\ue2ff]")


def _chatgpt_parts(parts: list[Any]) -> str:
    out: list[str] = []
    for part in parts:
        if isinstance(part, str):
            out.append(part)
        elif isinstance(part, dict):
            kind = str(part.get("content_type", ""))
            if isinstance(part.get("text"), str):  # e.g. audio_transcription (voice chats)
                out.append(part["text"])
            elif kind.startswith("image"):
                out.append("[image]")
    return _CITATION.sub("", "\n\n".join(p for p in out if p.strip()))


def _chatgpt_branch(mapping: dict[str, Any], current: Any) -> list[dict[str, Any]]:
    """The visible branch: from the current node (or the newest leaf) up to the root."""
    if not (isinstance(current, str) and current in mapping):
        leaves = [
            (to_epoch((n.get("message") or {}).get("create_time")), k)
            for k, n in mapping.items()
            if isinstance(n, dict) and not n.get("children")
        ]
        current = max(leaves)[1] if leaves else None
    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    while isinstance(current, str) and current in mapping and current not in seen:
        seen.add(current)
        node = mapping[current]
        chain.append(node)
        current = node.get("parent")
    chain.reverse()
    return chain


def parse_chatgpt(data: list[Any]) -> Iterator[ImportedConversation]:
    for conv in data:
        if not isinstance(conv, dict) or not isinstance(conv.get("mapping"), dict):
            continue
        chain = _chatgpt_branch(conv["mapping"], conv.get("current_node"))
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
            text = clean_text(_chatgpt_parts(content.get("parts") or []))
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
            if isinstance(blocks, list) and blocks:
                # `text` is only a fallback for old exports: next to `content` it can hold a
                # placeholder ("This block is not supported on your current device yet.").
                text = text_of(blocks, ("text",))
            else:
                text = str(msg.get("text") or "")
            for att in msg.get("attachments") or []:
                if isinstance(att, dict) and str(att.get("extracted_content") or "").strip():
                    name = att.get("file_name") or "attachment"
                    text += f"\n\n[{name}]\n{str(att['extracted_content'])[:ATTACHMENT_CHARS]}"
            text = clean_text(text)
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

_PROMPT_PREFIX = re.compile(
    r"^(Prompted|Eingegebener Prompt:|Prompt eingegeben:|Prompt:|Requête :|Prompt saisi :|"
    r"Solicitaste|Instrucción:|Prompt inserito:|Prompt enviado:)\s*"
)
# Activity records that aren't prompts ("Used Gemini Apps", "Created Gemini Canvas …").
_NOT_PROMPT = re.compile(r"^(Used|Created|Gave|Cleared|Deleted|Shared|Selected|Viewed|Visited)\b")
_CHAT_LINK = re.compile(r"gemini\.google\.com/(?:u/\d+/)?app/([0-9a-zA-Z]+)")


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


def _chat_id(entry: dict[str, Any]) -> str | None:
    """The Gemini chat a record belongs to, from its link (details[].url or titleUrl)."""
    urls = [entry.get("titleUrl")] + [
        d.get("url") for d in entry.get("details") or [] if isinstance(d, dict)
    ]
    for url in urls:
        match = _CHAT_LINK.search(str(url or ""))
        if match:
            return match.group(1)
    return None


def parse_gemini_activity(data: list[Any]) -> Iterator[ImportedConversation]:
    entries = [e for e in data if isinstance(e, dict) and isinstance(e.get("title"), str)]
    # When records carry a prompt prefix, only those are prompts; else skip known non-prompts.
    prefixed = any(_PROMPT_PREFIX.match(e["title"]) for e in entries)
    by_chat: dict[str, list[tuple[float, str, str, str]]] = {}
    loose: list[tuple[float, str, str, str]] = []
    for entry in entries:
        title = entry["title"]
        if (prefixed and not _PROMPT_PREFIX.match(title)) or _NOT_PROMPT.match(title):
            continue
        prompt = clean_text(_PROMPT_PREFIX.sub("", title))
        if not prompt:
            continue
        reply = "\n\n".join(
            html_to_text(item["html"])
            for item in entry.get("safeHtmlItem") or []
            if isinstance(item, dict) and isinstance(item.get("html"), str)
        )
        turn = (to_epoch(entry.get("time")), str(entry.get("time")), prompt, reply)
        chat = _chat_id(entry)
        (by_chat.setdefault(chat, []) if chat else loose).append(turn)
    for chat, turns in by_chat.items():
        yield _gemini_conversation(sorted(turns), f"chat:{chat}")
    # Records without a chat link: split into sessions on 30-minute gaps.
    loose.sort()
    session: list[tuple[float, str, str, str]] = []
    for turn in loose:
        if session and turn[0] - session[-1][0] > GEMINI_SESSION_GAP:
            yield _gemini_conversation(session)
            session = []
        session.append(turn)
    if session:
        yield _gemini_conversation(session)


def _gemini_conversation(
    session: list[tuple[float, str, str, str]], external_id: str | None = None
) -> ImportedConversation:
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
        external_id=external_id or f"session:{session[0][1]}",
        title=None,
        messages=messages,
    )


# ---------- file handling ----------


def _json_documents(path: Path) -> Iterator[tuple[str, Any]]:
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                base = name.rsplit("/", 1)[-1]
                # Gemini: any JSON under a "Gemini…" folder; Takeout localizes file names
                # (MyActivity.json, MeineAktivitäten.json, …), and detect() checks content.
                if base.endswith(".json") and (
                    base.startswith("conversations") or "gemini" in name.lower()
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
