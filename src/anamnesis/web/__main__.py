"""Entry point: `python -m anamnesis.web` or `anam-web` (binds to localhost by default)."""

from __future__ import annotations

import logging
import os

import uvicorn
from dotenv import load_dotenv


def main() -> None:
    load_dotenv(override=False)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    host = os.environ.get("ANAMNESIS_HOST", "127.0.0.1")
    port = int(os.environ.get("ANAMNESIS_PORT", "8000"))
    uvicorn.run("anamnesis.web.main:app", host=host, port=port)


if __name__ == "__main__":
    main()
