import pytest

from src.cli.commands import COMMANDS, Command, CommandError, help_text, is_command, parse_command


def test_parse() -> None:
    assert is_command("  /help") and not is_command("hello /x")
    assert parse_command("/model gemini") == Command("model", "gemini")
    assert parse_command("/MODEL") == Command("model", "")
    assert parse_command("/search  big  query ") == Command("search", "big  query")
    assert parse_command("/exit") == Command("quit")
    assert parse_command("/open 3") == Command("open", "3")


@pytest.mark.parametrize("bad", ["/nope", "/search", "/rename", "/open", "/open x"])
def test_errors(bad: str) -> None:
    with pytest.raises(CommandError):
        parse_command(bad)


def test_help_lists_every_command() -> None:
    assert all(f"/{name}" in help_text() for name in COMMANDS)
