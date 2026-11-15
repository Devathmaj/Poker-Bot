"""Central configuration for the poker bot."""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
ASSETS_DIR = BASE_DIR / "assets"
CARDS_DIR = ASSETS_DIR / "cards"

# Chips
DAILY_CHIPS = 1_000
DEFAULT_BUY_IN = 500
MIN_BUY_IN = 100
MAX_BUY_IN = 100_000

# Table
MIN_PLAYERS = 2
MAX_PLAYERS = 8
TURN_SECONDS = 60
MAX_TIMEOUTS = 3

# Session pacing (seconds)
SHOWDOWN_PAUSE = 8
STREET_PAUSE = 1.2

# Storage
DATABASE_URL = os.getenv("DATABASE_URL")

# Discord
GUILD_ID = os.getenv("GUILD_ID")


def blinds_for_buy_in(buy_in: int) -> tuple[int, int]:
    """Return small/big blind for a buy-in (500 -> 10/20)."""
    small = max(1, buy_in // 50)
    return small, small * 2
