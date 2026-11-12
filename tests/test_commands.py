import pytest

from anamnesis.cli.commands import (
    COMMANDS,
    Command,
    CommandError,
    help_text,
    is_command,
    parse_command,
)


def test_parse() -> None:
    assert is_command("  /help") and not is_command("hello /x")
    assert parse_command("/history chatgpt") == Command("history", "chatgpt")
    assert parse_command("/SYNC") == Command("sync", "")
    assert parse_command("/search  big  query ") == Command("search", "big  query")
    assert parse_command("/exit") == Command("quit")
    assert parse_command("/open 3") == Command("open", "3")


@pytest.mark.parametrize(
    "bad", ["/nope", "/search", "/rename", "/open", "/open x", "/model", "/new"]
)
def test_errors(bad: str) -> None:
    with pytest.raises(CommandError):
        parse_command(bad)


def test_help_lists_every_command() -> None:
    assert all(f"/{name}" in help_text() for name in COMMANDS)
