"""SQLite-backed chip wallet with atomic debits and daily claims."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path


@dataclass(frozen=True)
class DailyClaim:
    claimed: bool
    balance: int
    next_claim: datetime


class ChipStore:
    def __init__(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                chips INTEGER NOT NULL DEFAULT 0,
                last_daily TEXT
            )
            """
        )
        self._connection.commit()
        self._lock = asyncio.Lock()

    async def _call(self, function, *args):
        async with self._lock:
            return await asyncio.to_thread(function, *args)

    # ------------------------------------------------------------------ reads
    def _balance(self, user_id: int) -> int:
        row = self._connection.execute(
            "SELECT chips FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return int(row["chips"]) if row else 0

    async def balance(self, user_id: int) -> int:
        return await self._call(self._balance, user_id)

    # ----------------------------------------------------------------- writes
    def _debit(self, user_id: int, amount: int) -> int | None:
        cursor = self._connection.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        try:
            cursor.execute(
                "INSERT INTO users (user_id, chips) VALUES (?, 0) "
                "ON CONFLICT(user_id) DO NOTHING",
                (user_id,),
            )
            row = cursor.execute(
                "SELECT chips FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            if row is None or int(row["chips"]) < amount:
                self._connection.rollback()
                return None
            cursor.execute(
                "UPDATE users SET chips = chips - ? WHERE user_id = ?",
                (amount, user_id),
            )
            self._connection.commit()
            return int(row["chips"]) - amount
        except Exception:
            self._connection.rollback()
            raise

    async def debit(self, user_id: int, amount: int) -> int | None:
        """Take chips from a wallet. Returns the new balance or None."""
        return await self._call(self._debit, user_id, amount)

    def _credit(self, user_id: int, amount: int) -> int:
        cursor = self._connection.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        try:
            cursor.execute(
                "INSERT INTO users (user_id, chips) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET chips = chips + excluded.chips",
                (user_id, amount),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return self._balance(user_id)

    async def credit(self, user_id: int, amount: int) -> int:
        return await self._call(self._credit, user_id, amount)

    def _claim_daily(
        self, user_id: int, amount: int, today: date
    ) -> DailyClaim:
        next_claim = datetime.combine(
            today + timedelta(days=1), time.min, tzinfo=UTC
        )
        today_text = today.isoformat()
        cursor = self._connection.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        try:
            cursor.execute(
                "INSERT INTO users (user_id, chips) VALUES (?, 0) "
                "ON CONFLICT(user_id) DO NOTHING",
                (user_id,),
            )
            row = cursor.execute(
                "SELECT chips, last_daily FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if row is not None and row["last_daily"] == today_text:
                self._connection.commit()
                return DailyClaim(False, int(row["chips"]), next_claim)

            cursor.execute(
                "UPDATE users SET chips = chips + ?, last_daily = ? "
                "WHERE user_id = ?",
                (amount, today_text, user_id),
            )
            self._connection.commit()
            new_balance = int(row["chips"]) + amount if row else amount
            return DailyClaim(True, new_balance, next_claim)
        except Exception:
            self._connection.rollback()
            raise

    async def claim_daily(
        self, user_id: int, amount: int, today: date | None = None
    ) -> DailyClaim:
        today = today or datetime.now(UTC).date()
        return await self._call(self._claim_daily, user_id, amount, today)

    async def close(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._connection.close)
