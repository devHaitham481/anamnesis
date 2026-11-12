"""Importers for other AI tools' history (fixtures mirror each tool's real on-disk format)."""

from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path

import pytest
import zstandard

from anamnesis.ingest.claude_code import ClaudeCodeSource
from anamnesis.ingest.clean import clean_text
from anamnesis.ingest.codex import CodexSource
from anamnesis.ingest.cursor import CursorSource
from anamnesis.ingest.exports import ExportFormatError, parse_export
from anamnesis.ingest.gemini_cli import GeminiCliSource


def jsonl(path: Path, records: list[dict[str, object]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


# ---------- text cleaning ----------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("<task-notification>\n<task-id>x</task-id></task-notification>", ""),
        ("[Request interrupted by user]", ""),
        ("hello <system-reminder>secret harness text</system-reminder>world", "hello world"),
        ('<pasted_content id="1">the pasted text</pasted_content>', "the pasted text"),
        ("<command-name>/review</command-name><command-args>PR 12</command-args>", "/review PR 12"),
        ("<environment_context>cwd</environment_context>", ""),
        ("x" * 30_000, "x" * 20_000 + "\n…[truncated]"),
    ],
)
def test_clean_text(raw: str, expected: str) -> None:
    assert clean_text(raw) == expected


# ---------- Claude Code ----------


def test_claude_code_keeps_conversation_and_skips_plumbing(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    ts = "2026-11-01T10:00:00.000Z"
    jsonl(
        root / "-Users-me-proj" / "sess-1.jsonl",
        [
            {
                "type": "user",
                "uuid": "u1",
                "timestamp": ts,
                "cwd": "/Users/me/proj",
                "message": {"role": "user", "content": "How should we store vectors?"},
            },
            {
                "type": "assistant",
                "uuid": "a1",
                "timestamp": ts,
                "message": {
                    "role": "assistant",
                    "model": "claude-opus-5-5",
                    "content": [
                        {"type": "thinking", "thinking": "private"},
                        {"type": "text", "text": "Use LanceDB, it is embedded."},
                        {"type": "tool_use", "name": "Bash", "input": {}},
                    ],
                },
            },
            {
                "type": "user",
                "uuid": "u2",
                "timestamp": ts,
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "content": "ls output"}],
                },
            },
            {
                "type": "user",
                "uuid": "u3",
                "isMeta": True,
                "timestamp": ts,
                "message": {"role": "user", "content": "meta"},
            },
            {
                "type": "assistant",
                "uuid": "a2",
                "isSidechain": True,
                "timestamp": ts,
                "message": {"role": "assistant", "content": [{"type": "text", "text": "side"}]},
            },
            {
                "type": "user",
                "uuid": "u4",
                "timestamp": ts,
                "message": {"role": "user", "content": "[Request interrupted by user]"},
            },
            {"type": "ai-title", "aiTitle": "Vector store choice"},
        ],
    )
    jsonl(root / "-Users-me-proj" / "sess-1" / "subagents" / "agent-x.jsonl", [{"type": "user"}])
    (root / "-Users-me-proj" / "sess-1.jsonl").open("a").write('{"type": "user", "uui')  # torn

    src = ClaudeCodeSource(root)
    assert [p.name for p in src.discover()] == ["sess-1.jsonl"]
    (conv,) = src.parse(src.discover()[0])
    assert conv.external_id == "sess-1"
    assert conv.title == "Vector store choice"
    assert conv.project == "/Users/me/proj"
    assert [(m.role, m.content) for m in conv.messages] == [
        ("user", "How should we store vectors?"),
        ("assistant", "Use LanceDB, it is embedded."),
    ]
    assert conv.messages[1].model == "claude-opus-5-5"
    assert conv.messages[0].created_at == pytest.approx(1793527200.0)


# ---------- Codex ----------


def _codex_records() -> list[dict[str, object]]:
    ts = "2026-11-02T10:01:12.004Z"
    return [
        {
            "timestamp": ts,
            "type": "session_meta",
            "payload": {"id": "0199f3a2-aaaa-bbbb-cccc-000000000001", "cwd": "/Users/me/api"},
        },
        {"timestamp": ts, "type": "turn_context", "payload": {"model": "gpt-5.5-codex"}},
        {
            "timestamp": ts,
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "<environment_context>cwd</environment_context>"}
                ],
            },
        },
        {
            "timestamp": ts,
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "Add a retry to fetch()"},
        },
        {
            "timestamp": ts,
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Add a retry to fetch()"}],
            },
        },
        {
            "timestamp": ts,
            "type": "response_item",
            "payload": {"type": "reasoning", "summary": [], "encrypted_content": "gAAA"},
        },
        {
            "timestamp": ts,
            "type": "response_item",
            "payload": {"type": "function_call", "name": "shell", "arguments": "{}"},
        },
        {
            "timestamp": ts,
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Added backoff."}],
            },
        },
    ]


def test_codex_rollout_plain_and_zstd(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    day = home / "sessions" / "2026" / "11" / "02"
    jsonl(
        day / "rollout-2026-11-02T10-01-11-0199f3a2-aaaa-bbbb-cccc-000000000001.jsonl",
        _codex_records(),
    )
    raw = ("\n".join(json.dumps(r) for r in _codex_records()) + "\n").encode()
    (day / "rollout-2026-11-02T11-00-00-x.jsonl.zst").write_bytes(
        zstandard.ZstdCompressor().compress(raw)
    )
    jsonl(
        home / "session_index.jsonl",
        [
            {"id": "0199f3a2-aaaa-bbbb-cccc-000000000001", "thread_name": "old"},
            {"id": "0199f3a2-aaaa-bbbb-cccc-000000000001", "thread_name": "Retry for fetch"},
        ],
    )
    src = CodexSource(home)
    paths = src.discover()
    assert len(paths) == 2
    for path in paths:
        (conv,) = src.parse(path)
        assert conv.source == "codex"
        assert conv.title == "Retry for fetch"
        assert conv.project == "/Users/me/api"
        # response items win over UI events (no duplicates); injected context dropped
        assert [(m.role, m.content) for m in conv.messages] == [
            ("user", "Add a retry to fetch()"),
            ("assistant", "Added backoff."),
        ]
        assert conv.messages[1].model == "gpt-5.5-codex"


def test_codex_event_fallback_and_subagent_skip(tmp_path: Path) -> None:
    ts = "2026-11-02T10:00:00Z"
    events_only = jsonl(
        tmp_path / "rollout-a.jsonl",
        [
            {"timestamp": ts, "type": "session_meta", "payload": {"id": "s1"}},
            {
                "timestamp": ts,
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "hi"},
            },
            {
                "timestamp": ts,
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": "hello"},
            },
        ],
    )
    sub = jsonl(
        tmp_path / "rollout-b.jsonl",
        [
            {
                "timestamp": ts,
                "type": "session_meta",
                "payload": {"id": "s2", "parent_thread_id": "s1"},
            },
            {
                "timestamp": ts,
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "x"},
            },
        ],
    )
    legacy = jsonl(
        tmp_path / "rollout-c.jsonl",
        [
            {"id": "legacy-1", "timestamp": ts, "instructions": ""},
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "old q"}],
            },
            {"record_type": "state"},
        ],
    )
    src = CodexSource(tmp_path)
    assert [m.content for m in next(iter(src.parse(events_only))).messages] == ["hi", "hello"]
    assert list(src.parse(sub)) == []
    (conv,) = src.parse(legacy)
    assert conv.external_id == "legacy-1" and conv.messages[0].content == "old q"


# ---------- Gemini CLI ----------


def test_gemini_cli_replays_append_log(tmp_path: Path) -> None:
    root = tmp_path / "tmp"
    proj = root / "my-proj"
    (proj).mkdir(parents=True)
    (proj / ".project_root").write_text("/Users/me/site\n")
    log = jsonl(
        proj / "chats" / "session-2026-11-03T09-00-5f1c9a2e.jsonl",
        [
            {"sessionId": "5f1c9a2e-1", "projectHash": "9b7e", "startTime": "2026-11-03T09:00:00Z"},
            {
                "id": "m1",
                "timestamp": "2026-11-03T09:00:05Z",
                "type": "user",
                "content": [{"text": "Explain the auth flow"}],
            },
            {
                "id": "m2",
                "timestamp": "2026-11-03T09:00:09Z",
                "type": "gemini",
                "content": [{"text": "thinking...", "thought": True}, {"text": "draft"}],
                "toolCalls": [{"id": "t1", "name": "read_file"}],
                "model": "gemini-3-pro",
            },
            {
                "id": "m2",
                "timestamp": "2026-11-03T09:00:09Z",
                "type": "gemini",
                "content": "It starts in login.ts.",
                "model": "gemini-3-pro",
            },
            {"id": "m3", "timestamp": "2026-11-03T09:01:00Z", "type": "user", "content": "oops"},
            {"id": "m4", "timestamp": "2026-11-03T09:01:01Z", "type": "info", "content": "note"},
            {"$rewindTo": "m3"},
            {"$set": {"summary": "Auth flow explanation"}},
        ],
    )
    legacy = proj / "chats" / "session-2026-01-01T00-00-abc.json"
    legacy.write_text(
        json.dumps(
            {
                "sessionId": "old-1",
                "startTime": "2026-01-01T00:00:00Z",
                "messages": [{"id": "x", "type": "user", "content": "legacy q"}],
            }
        )
    )
    src = GeminiCliSource([root])
    assert sorted(p.name for p in src.discover()) == sorted([log.name, legacy.name])
    (conv,) = src.parse(log)
    assert conv.external_id == "5f1c9a2e-1"
    assert conv.title == "Auth flow explanation"
    assert conv.project == "/Users/me/site"
    assert [(m.role, m.content) for m in conv.messages] == [
        ("user", "Explain the auth flow"),
        ("assistant", "It starts in login.ts."),
    ]
    (old,) = src.parse(legacy)
    assert old.messages[0].content == "legacy q"


# ---------- Cursor ----------


def test_cursor_composer_bubbles(tmp_path: Path) -> None:
    db = tmp_path / "state.vscdb"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)")
    rows = {
        "composerData:c1": {
            "composerId": "c1",
            "name": "Fix button",
            "createdAt": 1793527200000,
            "workspaceIdentifier": {"uri": {"fsPath": "/Users/me/web"}},
            "fullConversationHeadersOnly": [
                {"bubbleId": "b1", "type": 1},
                {"bubbleId": "b2", "type": 2},
                {"bubbleId": "b3", "type": 2},
                {"bubbleId": "b4", "type": 2},
            ],
        },
        "bubbleId:c1:b1": {
            "type": 1,
            "text": "Make the button blue",
            "createdAt": "2026-11-01T10:00:00Z",
        },
        "bubbleId:c1:b2": {"type": 2, "text": "", "thinking": {"text": "hmm"}},
        "bubbleId:c1:b3": {"type": 2, "text": "", "toolFormerData": {"tool": 5}},
        "bubbleId:c1:b4": {
            "type": 2,
            "text": "Done, it's blue now.",
            "modelInfo": {"modelName": "claude-sonnet-5-5"},
        },
        "composerData:c2": {
            "composerId": "c2",
            "name": "Legacy",
            "conversation": [{"type": 1, "bubbleId": "x", "text": "old question"}],
        },
        "composerData:c3": {
            "composerId": "c3",
            "isSubagent": True,
            "conversation": [{"type": 1, "text": "sub"}],
        },
    }
    conn.executemany(
        "INSERT INTO cursorDiskKV VALUES (?, ?)", [(k, json.dumps(v)) for k, v in rows.items()]
    )
    conn.commit()
    conn.close()
    src = CursorSource(db)
    convs = {c.external_id: c for c in src.parse(db)}
    assert set(convs) == {"c1", "c2"}
    c1 = convs["c1"]
    assert c1.title == "Fix button" and c1.project == "/Users/me/web"
    assert [(m.role, m.content) for m in c1.messages] == [
        ("user", "Make the button blue"),
        ("assistant", "Done, it's blue now."),
    ]
    assert c1.messages[1].model == "claude-sonnet-5-5"
    assert src.signature(db)


# ---------- web exports ----------

CHATGPT = [
    {
        "id": "conv-1",
        "title": "Trip ideas",
        "create_time": 1730000000.0,
        "current_node": "n4",
        "mapping": {
            "n0": {
                "id": "n0",
                "parent": None,
                "children": ["n1"],
                "message": {
                    "id": "n0",
                    "author": {"role": "system"},
                    "content": {"content_type": "text", "parts": ["sys"]},
                },
            },
            "n1": {
                "id": "n1",
                "parent": "n0",
                "children": ["n2", "n3"],
                "message": {
                    "id": "n1",
                    "author": {"role": "user"},
                    "create_time": 1730000001.0,
                    "content": {"content_type": "text", "parts": ["Ideas for Lisbon?"]},
                },
            },
            "n2": {
                "id": "n2",
                "parent": "n1",
                "children": [],
                "message": {
                    "id": "n2",
                    "author": {"role": "assistant"},
                    "content": {"content_type": "text", "parts": ["abandoned branch"]},
                },
            },
            "n3": {
                "id": "n3",
                "parent": "n1",
                "children": ["n4"],
                "message": {
                    "id": "n3",
                    "author": {"role": "assistant"},
                    "recipient": "browser",
                    "content": {"content_type": "code", "parts": ["search()"]},
                },
            },
            "n4": {
                "id": "n4",
                "parent": "n3",
                "children": [],
                "message": {
                    "id": "n4",
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "metadata": {"model_slug": "gpt-5"},
                    "content": {"content_type": "text", "parts": ["Try Alfama."]},
                },
            },
        },
    }
]
CLAUDE = [
    {
        "uuid": "c-1",
        "name": "Refactor plan",
        "created_at": "2026-10-01T12:00:00Z",
        "chat_messages": [
            {
                "uuid": "m1",
                "sender": "human",
                "text": "Help me refactor X",
                "created_at": "2026-10-01T12:00:00Z",
            },
            {
                "uuid": "m2",
                "sender": "assistant",
                "text": "ignored when content exists",
                "content": [
                    {"type": "thinking", "thinking": "..."},
                    {"type": "text", "text": "Sure."},
                ],
            },
        ],
    }
]
GEMINI = [
    {
        "header": "Gemini Apps",
        "title": "Prompted What is RAG?",
        "time": "2026-09-01T10:00:00Z",
        "products": ["Gemini Apps"],
        "safeHtmlItem": [{"html": "<p>Retrieval &amp; generation.</p>"}],
    },
    {
        "header": "Gemini Apps",
        "title": "Prompted Give an example",
        "time": "2026-09-01T10:05:00Z",
        "products": ["Gemini Apps"],
    },
    {
        "header": "Gemini Apps",
        "title": "Eingegebener Prompt: Hallo",
        "time": "2026-09-02T08:00:00Z",
        "products": ["Gemini Apps"],
        "safeHtmlItem": [{"html": "Hallo!"}],
    },
]


def test_chatgpt_export_follows_visible_branch(tmp_path: Path) -> None:
    f = tmp_path / "conversations.json"
    f.write_text(json.dumps(CHATGPT))
    (conv,) = parse_export(f)
    assert conv.source == "chatgpt" and conv.title == "Trip ideas"
    assert [(m.role, m.content) for m in conv.messages] == [
        ("user", "Ideas for Lisbon?"),
        ("assistant", "Try Alfama."),
    ]
    assert conv.messages[1].model == "gpt-5"


def test_claude_export_in_zip(tmp_path: Path) -> None:
    z = tmp_path / "data-2026.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("conversations.json", json.dumps(CLAUDE))
        zf.writestr("users.json", "[]")
    (conv,) = parse_export(z)
    assert conv.source == "claude-web" and conv.title == "Refactor plan"
    assert [m.content for m in conv.messages] == ["Help me refactor X", "Sure."]


def test_gemini_takeout_groups_sessions(tmp_path: Path) -> None:
    z = tmp_path / "takeout.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("Takeout/My Activity/Gemini Apps/MyActivity.json", json.dumps(GEMINI))
    convs = list(parse_export(z))
    assert [len(c.messages) for c in convs] == [3, 2]
    assert [(m.role, m.content) for m in convs[0].messages] == [
        ("user", "What is RAG?"),
        ("assistant", "Retrieval & generation."),
        ("user", "Give an example"),
    ]
    assert convs[1].messages[0].content == "Hallo"


def test_unknown_export_is_rejected(tmp_path: Path) -> None:
    f = tmp_path / "x.json"
    f.write_text('{"not": "an export"}')
    with pytest.raises(ExportFormatError):
        list(parse_export(f))
    h = tmp_path / "MyActivity.html"
    h.write_text("<html></html>")
    with pytest.raises(ExportFormatError, match="JSON"):
        list(parse_export(h))
