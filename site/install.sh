#!/bin/sh
# Anamnesis installer: one memory for all your AI tools.
#
#   curl -LsSf https://devhaitham481.github.io/anamnesis/install.sh | sh
#
# What it does, in order:
#   1. installs uv (Astral's Python tool installer) if you don't have it,
#   2. installs Anamnesis with it, in its own isolated environment (uv fetches a
#      suitable Python if needed; your system Python is never touched),
#   3. runs `anam setup`, which imports your AI tools' history and asks before
#      connecting them.
# Re-run it any time to upgrade. Uninstall: see the README.
#
# Environment overrides:
#   ANAMNESIS_SOURCE      what to install (default: the latest main branch on GitHub)
#   ANAMNESIS_NO_SETUP=1  install only; run `anam setup` yourself later

set -eu

REPO="https://github.com/devHaitham481/anamnesis"
PYTHON_VERSION="3.12"

say() { printf '%s\n' "$*"; }
fail() { printf 'error: %s\n' "$*" >&2; exit 1; }

case "$(uname -s)" in
  Darwin | Linux) ;;
  *) fail "Anamnesis supports macOS and Linux (on Windows, use WSL)." ;;
esac

# 1. uv
if ! command -v uv >/dev/null 2>&1; then
  say "Installing uv (https://docs.astral.sh/uv)…"
  command -v curl >/dev/null 2>&1 || fail "curl is required"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  PATH="$HOME/.local/bin:$PATH"
  export PATH
  command -v uv >/dev/null 2>&1 || fail "uv was installed but is not on PATH; open a new terminal and re-run"
fi

# 2. Anamnesis. A git checkout when git is available, else the source archive.
if [ -n "${ANAMNESIS_SOURCE:-}" ]; then
  source="$ANAMNESIS_SOURCE"
elif command -v git >/dev/null 2>&1; then
  source="git+$REPO"
else
  source="$REPO/archive/refs/heads/main.tar.gz"
fi
say "Installing Anamnesis…"
uv tool install --force --reinstall --refresh --python "$PYTHON_VERSION" "$source"

bin_dir="$(uv tool dir --bin)"
anam="$bin_dir/anam"
[ -x "$anam" ] || fail "install finished but $anam is missing"

on_path=1
case ":$PATH:" in
  *":$bin_dir:"*) ;;
  *) on_path=0; uv tool update-shell >/dev/null 2>&1 || true ;;
esac

say ""
say "✓ Anamnesis installed: $("$anam" --version 2>/dev/null || echo anam)"

# 3. Guided setup. Read answers from the terminal even though this script is piped into sh.
if [ "${ANAMNESIS_NO_SETUP:-0}" != "1" ]; then
  say ""
  if [ -t 1 ] && (exec </dev/tty) 2>/dev/null; then
    "$anam" setup </dev/tty || true
  else
    "$anam" setup || true
  fi
fi

if [ "$on_path" = "0" ]; then
  say ""
  say "Open a new terminal to use \`anam\` (added $bin_dir to your PATH)."
fi
