"""PostgreSQL-backed chip wallet with atomic debits and daily claims."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger(__name__)

# Every claim starts the player's cooldown over. Using a rolling window keyed
# to each user's own timestamp (instead of a UTC day boundary) means nobody
# can claim twice in a short span around midnight.
DAILY_COOLDOWN = timedelta(hours=24)

# Errors that mean "the socket is gone", not "the query is wrong". A pooled
# Postgres endpoint (Supabase, PgBouncer, ...) closes idle sessions, and the
# first statement after that fails even though the connection object still
# reports itself as open.
_CONNECTION_ERRORS = (psycopg.OperationalError, psycopg.InterfaceError)

_SCHEMAS = (
    """
    CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY,
        chips BIGINT NOT NULL DEFAULT 0,
        last_daily TIMESTAMPTZ
    )
    """,
    # ``last_daily`` used to be a UTC date. The daily reward is now a rolling
    # 24-hour cooldown measured from the last claim, so existing rows are
    # converted from the start of that UTC day.
    """
    DO $$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'users'
              AND column_name = 'last_daily'
              AND data_type = 'date'
        ) THEN
            ALTER TABLE users
                ALTER COLUMN last_daily TYPE TIMESTAMPTZ
                USING last_daily::timestamp AT TIME ZONE 'UTC';
        END IF;
    END $$
    """,
    # Identity lives on the same row as the wallet. Databases created before
    # these columns existed get them added in place.
    """
    ALTER TABLE users
        ADD COLUMN IF NOT EXISTS username TEXT,
        ADD COLUMN IF NOT EXISTS display_name TEXT,
        ADD COLUMN IF NOT EXISTS added_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    """,
    """
    CREATE TABLE IF NOT EXISTS player_stats (
        user_id BIGINT PRIMARY KEY,
        games_played BIGINT NOT NULL DEFAULT 0,
        hands_played BIGINT NOT NULL DEFAULT 0,
        hands_won BIGINT NOT NULL DEFAULT 0,
        biggest_pot BIGINT NOT NULL DEFAULT 0,
        net_chips BIGINT NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS discord_guilds (
        guild_id BIGINT PRIMARY KEY,
        name TEXT NOT NULL,
        added_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        removed_at TIMESTAMPTZ
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS guild_events (
        id BIGSERIAL PRIMARY KEY,
        guild_id BIGINT NOT NULL,
        name TEXT NOT NULL,
        event TEXT NOT NULL CHECK (event IN ('added', 'removed')),
        occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
)


@dataclass(frozen=True)
class DailyClaim:
    claimed: bool
    balance: int
    next_claim: datetime


@dataclass(frozen=True)
class HandStat:
    user_id: int
    won: bool
    pot_won: int
    net: int


@dataclass(frozen=True)
class ProfileData:
    balance: int
    can_claim_daily: bool
    next_claim: datetime
    games_played: int
    hands_played: int
    hands_won: int
    biggest_pot: int
    net_chips: int

    @property
    def hand_win_rate(self) -> float:
        if not self.hands_played:
            return 0.0
        return self.hands_won / self.hands_played


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
        self._create_tables()
        self._fold_legacy_users()
        self._lock = asyncio.Lock()

    def _fold_legacy_users(self) -> None:
        """Merge the short-lived ``discord_users`` table into ``users``."""
        with self._connection.transaction():
            found = self._connection.execute(
                "SELECT to_regclass('discord_users') AS name"
            ).fetchone()
            if found is None or found["name"] is None:
                return
            self._connection.execute(
                "INSERT INTO users "
                "(user_id, username, display_name, added_at, updated_at) "
                "SELECT user_id, username, display_name, added_at, updated_at "
                "FROM discord_users "
                "ON CONFLICT (user_id) DO UPDATE SET "
                "username = COALESCE(EXCLUDED.username, users.username), "
                "display_name = COALESCE(EXCLUDED.display_name, users.display_name), "
                "added_at = LEAST(users.added_at, EXCLUDED.added_at), "
                "updated_at = GREATEST(users.updated_at, EXCLUDED.updated_at)"
            )
            self._connection.execute("DROP TABLE discord_users")
            logger.info("Merged discord_users into users")

    def _create_tables(
        self, connection: psycopg.Connection | None = None
    ) -> None:
        connection = connection or self._connection
        with connection.transaction():
            for schema in _SCHEMAS:
                connection.execute(schema)

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(
            self._database_url,
            row_factory=dict_row,
            application_name="pokerbot",
            connect_timeout=10,
            # Single-statement reads must not leave an idle transaction open:
            # it holds locks that block schema changes and pins the session.
            autocommit=True,
            # Keep the pooled session alive so an idle game does not lose its
            # connection while nobody is claiming chips.
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=5,
        )

    def _reconnect(self) -> None:
        with suppress(Exception):
            self._connection.close()
        connection = self._connect()
        self._create_tables(connection)
        self._connection = connection

    def _ensure_connection(self) -> None:
        # ``closed`` is only set once psycopg notices; ``broken`` catches a
        # server-side session that was killed while the object looked healthy.
        if self._connection.closed or self._connection.broken:
            logger.warning("Database connection is gone; reconnecting")
            self._reconnect()

    async def _call(self, function, *args):
        async with self._lock:
            return await asyncio.to_thread(self._run, function, *args)

    def _run(self, function, *args):
        self._ensure_connection()
        try:
            return function(*args)
        except _CONNECTION_ERRORS as exc:
            # The session dropped between the check above and the statement.
            # Nothing was committed, so it is safe to retry once on a fresh
            # connection instead of failing the Discord interaction.
            logger.warning("Database statement failed (%s); retrying once", exc)
            self._reconnect()
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
        self, user_id: int, amount: int, now: datetime
    ) -> DailyClaim:
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
            assert row is not None
            chips = int(row["chips"])
            last_daily = row["last_daily"]
            if last_daily is not None:
                ready_at = last_daily + DAILY_COOLDOWN
                if now < ready_at:
                    return DailyClaim(False, chips, ready_at)

            self._connection.execute(
                "UPDATE users SET chips = chips + %s, last_daily = %s "
                "WHERE user_id = %s",
                (amount, now, user_id),
            )
        return DailyClaim(True, chips + amount, now + DAILY_COOLDOWN)

    async def claim_daily(
        self, user_id: int, amount: int, now: datetime | None = None
    ) -> DailyClaim:
        now = now or datetime.now(UTC)
        return await self._call(self._claim_daily, user_id, amount, now)

    # ------------------------------------------------------------------ stats
    def _record_games_started(self, user_ids: list[int]) -> None:
        with self._connection.transaction():
            for user_id in user_ids:
                self._connection.execute(
                    "INSERT INTO users (user_id, chips) VALUES (%s, 0) "
                    "ON CONFLICT (user_id) DO NOTHING",
                    (user_id,),
                )
                self._connection.execute(
                    "INSERT INTO player_stats (user_id, games_played) "
                    "VALUES (%s, 1) "
                    "ON CONFLICT (user_id) DO UPDATE "
                    "SET games_played = player_stats.games_played + 1",
                    (user_id,),
                )

    async def record_games_started(self, user_ids: list[int]) -> None:
        await self._call(self._record_games_started, user_ids)

    def _record_hand_stats(self, stats: list[HandStat]) -> None:
        with self._connection.transaction():
            for stat in stats:
                self._connection.execute(
                    "INSERT INTO users (user_id, chips) VALUES (%s, 0) "
                    "ON CONFLICT (user_id) DO NOTHING",
                    (stat.user_id,),
                )
                self._connection.execute(
                    "INSERT INTO player_stats "
                    "(user_id, hands_played, hands_won, biggest_pot, net_chips) "
                    "VALUES (%s, 1, %s, %s, %s) "
                    "ON CONFLICT (user_id) DO UPDATE SET "
                    "hands_played = player_stats.hands_played + 1, "
                    "hands_won = player_stats.hands_won + EXCLUDED.hands_won, "
                    "biggest_pot = GREATEST("
                    "player_stats.biggest_pot, EXCLUDED.biggest_pot), "
                    "net_chips = player_stats.net_chips + EXCLUDED.net_chips",
                    (
                        stat.user_id,
                        int(stat.won),
                        stat.pot_won,
                        stat.net,
                    ),
                )

    async def record_hand_stats(self, stats: list[HandStat]) -> None:
        await self._call(self._record_hand_stats, stats)

    # ---------------------------------------------------------------- profile
    def _profile(self, user_id: int, now: datetime) -> ProfileData:
        row = self._connection.execute(
            "SELECT u.chips, u.last_daily, "
            "COALESCE(s.games_played, 0) AS games_played, "
            "COALESCE(s.hands_played, 0) AS hands_played, "
            "COALESCE(s.hands_won, 0) AS hands_won, "
            "COALESCE(s.biggest_pot, 0) AS biggest_pot, "
            "COALESCE(s.net_chips, 0) AS net_chips "
            "FROM users u LEFT JOIN player_stats s "
            "ON s.user_id = u.user_id WHERE u.user_id = %s",
            (user_id,),
        ).fetchone()
        if row is None:
            return ProfileData(0, True, now, 0, 0, 0, 0, 0)
        last_daily = row["last_daily"]
        if last_daily is None:
            can_claim = True
            next_claim = now
        else:
            next_claim = last_daily + DAILY_COOLDOWN
            can_claim = now >= next_claim
        return ProfileData(
            balance=int(row["chips"]),
            can_claim_daily=can_claim,
            next_claim=next_claim,
            games_played=int(row["games_played"]),
            hands_played=int(row["hands_played"]),
            hands_won=int(row["hands_won"]),
            biggest_pot=int(row["biggest_pot"]),
            net_chips=int(row["net_chips"]),
        )

    async def profile(
        self, user_id: int, now: datetime | None = None
    ) -> ProfileData:
        now = now or datetime.now(UTC)
        return await self._call(self._profile, user_id, now)

    # ---------------------------------------------------------------- audience
    def _remember_user(
        self, user_id: int, username: str, display_name: str | None
    ) -> None:
        with self._connection.transaction():
            self._connection.execute(
                "INSERT INTO users (user_id, username, display_name) "
                "VALUES (%s, %s, %s) "
                "ON CONFLICT (user_id) DO UPDATE SET "
                "username = EXCLUDED.username, "
                "display_name = EXCLUDED.display_name, "
                "updated_at = now()",
                (user_id, username, display_name),
            )

    async def remember_user(
        self, user_id: int, username: str, display_name: str | None = None
    ) -> None:
        """Store (or refresh) a player who used the bot."""
        await self._call(self._remember_user, user_id, username, display_name)

    def _sync_guilds(self, guilds: list[tuple[int, str]]) -> None:
        current_ids = {guild_id for guild_id, _ in guilds}
        with self._connection.transaction():
            rows = self._connection.execute(
                "SELECT guild_id, name, removed_at FROM discord_guilds"
            ).fetchall()
            known = {
                int(row["guild_id"]): (row["name"], row["removed_at"])
                for row in rows
            }

            added: list[tuple[int, str]] = []
            renamed: list[tuple[str, int]] = []
            for guild_id, name in guilds:
                previous = known.get(guild_id)
                if previous is None or previous[1] is not None:
                    # First time we see a server, or the bot is back after
                    # being kicked: start a new tenancy.
                    added.append((guild_id, name))
                elif previous[0] != name:
                    renamed.append((name, guild_id))

            removed = [
                (guild_id, name)
                for guild_id, (name, removed_at) in known.items()
                if removed_at is None and guild_id not in current_ids
            ]

            # ``executemany`` pipelines these statements, so the number of
            # network round trips does not grow with the server count.
            with self._connection.cursor() as cursor:
                if added:
                    cursor.executemany(
                        "INSERT INTO discord_guilds "
                        "(guild_id, name, added_at, removed_at) "
                        "VALUES (%s, %s, now(), NULL) "
                        "ON CONFLICT (guild_id) DO UPDATE SET "
                        "name = EXCLUDED.name, added_at = EXCLUDED.added_at, "
                        "removed_at = NULL",
                        added,
                    )
                    cursor.executemany(
                        "INSERT INTO guild_events (guild_id, name, event) "
                        "VALUES (%s, %s, 'added')",
                        added,
                    )
                if renamed:
                    cursor.executemany(
                        "UPDATE discord_guilds SET name = %s WHERE guild_id = %s",
                        renamed,
                    )
                if removed:
                    cursor.executemany(
                        "UPDATE discord_guilds SET removed_at = now() "
                        "WHERE guild_id = %s",
                        [(guild_id,) for guild_id, _ in removed],
                    )
                    cursor.executemany(
                        "INSERT INTO guild_events (guild_id, name, event) "
                        "VALUES (%s, %s, 'removed')",
                        removed,
                    )

    async def sync_guilds(self, guilds: list[tuple[int, str]]) -> None:
        """Reconcile stored servers with the ones the bot is currently in.

        This catches changes that happened while the bot was offline, including
        being kicked from a server.
        """
        await self._call(self._sync_guilds, guilds)

    def _record_guild_added(self, guild_id: int, name: str) -> None:
        with self._connection.transaction():
            self._connection.execute(
                "INSERT INTO discord_guilds (guild_id, name, added_at, removed_at) "
                "VALUES (%s, %s, now(), NULL) "
                "ON CONFLICT (guild_id) DO UPDATE SET "
                "name = EXCLUDED.name, added_at = EXCLUDED.added_at, "
                "removed_at = NULL",
                (guild_id, name),
            )
            self._connection.execute(
                "INSERT INTO guild_events (guild_id, name, event) "
                "VALUES (%s, %s, 'added')",
                (guild_id, name),
            )

    async def record_guild_added(self, guild_id: int, name: str) -> None:
        await self._call(self._record_guild_added, guild_id, name)

    def _record_guild_removed(self, guild_id: int, name: str) -> None:
        with self._connection.transaction():
            self._connection.execute(
                "UPDATE discord_guilds SET name = %s, removed_at = now() "
                "WHERE guild_id = %s",
                (name, guild_id),
            )
            self._connection.execute(
                "INSERT INTO guild_events (guild_id, name, event) "
                "VALUES (%s, %s, 'removed')",
                (guild_id, name),
            )

    async def record_guild_removed(self, guild_id: int, name: str) -> None:
        await self._call(self._record_guild_removed, guild_id, name)

    async def close(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._connection.close)
