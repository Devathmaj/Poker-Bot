"""Run with: python -m pokerbot"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    load_dotenv(root / ".env")

    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print(
            "DISCORD_TOKEN is missing. Copy .env.example to .env and add your "
            "bot token.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    if not os.getenv("DATABASE_URL"):
        print(
            "DATABASE_URL is missing. Add your PostgreSQL connection string "
            "to .env.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    # Import after .env is loaded because config reads environment variables.
    from .bot import PokerBot

    PokerBot().run(token)


if __name__ == "__main__":
    main()
