"""PostgreSQL-backed chip wallet with atomic debits and daily claims."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

import psycopg
from psycopg.rows import dict_row

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id BIGINT PRIMARY KEY,
    chips BIGINT NOT NULL DEFAULT 0,
    last_daily DATE
)
"""


@dataclass(frozen=True)
class DailyClaim:
    claimed: bool
    balance: int
    next_claim: datetime


class ChipStore:
    """Chip balances stored in the PostgreSQL database at ``DATABASE_URL``."""

    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise ValueError(
                "DATABASE_URL is missing. Add your PostgreSQL connection string "
                "to .env."
            )
        if database_url.startswith("postgres://"):
            database_url = "postgresql://" + database_url[len("postgres://") :]
        self._database_url = database_url
        self._connection = self._connect()
        self._connection.execute(_SCHEMA)
        self._connection.commit()
        self._lock = asyncio.Lock()

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._database_url, row_factory=dict_row)

    def _ensure_connection(self) -> None:
        if self._connection.closed:
            self._connection = self._connect()
            self._connection.execute(_SCHEMA)
            self._connection.commit()

    async def _call(self, function, *args):
        async with self._lock:
            return await asyncio.to_thread(self._run, function, *args)

    def _run(self, function, *args):
        self._ensure_connection()
        return function(*args)

    # ------------------------------------------------------------------ reads
    def _balance(self, user_id: int) -> int:
        row = self._connection.execute(
            "SELECT chips FROM users WHERE user_id = %s", (user_id,)
        ).fetchone()
        return int(row["chips"]) if row else 0

    async def balance(self, user_id: int) -> int:
        return await self._call(self._balance, user_id)

    # ----------------------------------------------------------------- writes
    def _debit(self, user_id: int, amount: int) -> int | None:
        if amount < 0:
            raise ValueError("Cannot debit a negative amount.")
        with self._connection.transaction():
            self._connection.execute(
                "INSERT INTO users (user_id, chips) VALUES (%s, 0) "
                "ON CONFLICT (user_id) DO NOTHING",
                (user_id,),
            )
            row = self._connection.execute(
                "SELECT chips FROM users WHERE user_id = %s FOR UPDATE",
                (user_id,),
            ).fetchone()
            if row is None or int(row["chips"]) < amount:
                return None
            self._connection.execute(
                "UPDATE users SET chips = chips - %s WHERE user_id = %s",
                (amount, user_id),
            )
            return int(row["chips"]) - amount

    async def debit(self, user_id: int, amount: int) -> int | None:
        """Take chips from a wallet. Returns the new balance or None."""
        return await self._call(self._debit, user_id, amount)

    def _credit(self, user_id: int, amount: int) -> int:
        with self._connection.transaction():
            row = self._connection.execute(
                "INSERT INTO users (user_id, chips) VALUES (%s, %s) "
                "ON CONFLICT (user_id) DO UPDATE "
                "SET chips = users.chips + EXCLUDED.chips "
                "RETURNING chips",
                (user_id, amount),
            ).fetchone()
        assert row is not None
        return int(row["chips"])

    async def credit(self, user_id: int, amount: int) -> int:
        return await self._call(self._credit, user_id, amount)

    def _claim_daily(
        self, user_id: int, amount: int, today: date
    ) -> DailyClaim:
        next_claim = datetime.combine(
            today + timedelta(days=1), time.min, tzinfo=UTC
        )
        with self._connection.transaction():
            self._connection.execute(
                "INSERT INTO users (user_id, chips) VALUES (%s, 0) "
                "ON CONFLICT (user_id) DO NOTHING",
                (user_id,),
            )
            row = self._connection.execute(
                "SELECT chips, last_daily FROM users WHERE user_id = %s FOR UPDATE",
                (user_id,),
            ).fetchone()
            if row is not None and row["last_daily"] == today:
                return DailyClaim(False, int(row["chips"]), next_claim)

            self._connection.execute(
                "UPDATE users SET chips = chips + %s, last_daily = %s "
                "WHERE user_id = %s",
                (amount, today, user_id),
            )
            new_balance = int(row["chips"]) + amount if row else amount
        return DailyClaim(True, new_balance, next_claim)

    async def claim_daily(
        self, user_id: int, amount: int, today: date | None = None
    ) -> DailyClaim:
        today = today or datetime.now(UTC).date()
        return await self._call(self._claim_daily, user_id, amount, today)

    async def close(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._connection.close)
