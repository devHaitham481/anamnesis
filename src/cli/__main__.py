"""Entry point: `python -m src.cli` or `nexus`."""

from __future__ import annotations

import logging

from src.config import load_settings


def main() -> None:
    settings = load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    # Never log to the terminal while the TUI owns it.
    logging.basicConfig(
        filename=settings.data_dir / "nexus.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from src.cli.app import NexusApp

    NexusApp(settings).run()


if __name__ == "__main__":
    main()
