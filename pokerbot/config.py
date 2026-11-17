"""Central configuration for the poker bot."""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
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

# Optional per-game hand limit (None = play until one player is left).
MIN_HANDS = 1
MAX_HANDS = 1_000

# Session pacing (seconds)
SHOWDOWN_PAUSE = 8
STREET_PAUSE = 1.2

# Storage
DATABASE_URL = os.getenv("DATABASE_URL")

# Discord
GUILD_ID = os.getenv("GUILD_ID")

# Health endpoint polled by uptime monitors and container checks. Hosting
# platforms inject PORT and route traffic to it, so PORT wins when present;
# HEALTH_PORT is the local fallback. Port 0 would bind an ephemeral port that
# no poller can guess, so it is rejected in favour of the default.
HEALTH_HOST = os.getenv("HEALTH_HOST", "0.0.0.0")


def _port_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        port = int(raw)
    except ValueError:
        return default
    return port if 1 <= port <= 65535 else default


HEALTH_PORT = _port_env("PORT", _port_env("HEALTH_PORT", 8080))


def blinds_for_buy_in(buy_in: int) -> tuple[int, int]:
    """Return small/big blind for a buy-in (500 -> 10/20)."""
    small = max(1, buy_in // 50)
    return small, small * 2
