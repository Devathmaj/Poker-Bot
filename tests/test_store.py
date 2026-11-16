"""Tests for the PostgreSQL chip wallet."""

from __future__ import annotations

import os
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

from pokerbot.store import ChipStore, HandStat

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
DATABASE_URL = os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL")


def with_schema(database_url: str, schema: str) -> str:
    separator = "&" if "?" in database_url else "?"
    return (
        f"{database_url}{separator}options="
        f"{quote(f'-c search_path={schema}')}"
    )


class StoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        if not DATABASE_URL:
            raise unittest.SkipTest(
                "DATABASE_URL or TEST_DATABASE_URL must point at PostgreSQL."
            )

        self.schema = f"pokerbot_test_{uuid.uuid4().hex[:12]}"
        self.admin = psycopg.connect(DATABASE_URL, autocommit=True)
        try:
            self.admin.execute(f'CREATE SCHEMA "{self.schema}"')
        except psycopg.Error as exc:
            self.admin.close()
            raise unittest.SkipTest(f"PostgreSQL is not available: {exc}") from exc

        self.store = ChipStore(with_schema(DATABASE_URL, self.schema))

    async def asyncTearDown(self) -> None:
        if hasattr(self, "store"):
            await self.store.close()
        if hasattr(self, "admin"):
            self.admin.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')
            self.admin.close()

    def query(self, sql: str, params=None) -> list[dict]:
        with psycopg.connect(
            with_schema(DATABASE_URL, self.schema), row_factory=dict_row
        ) as connection:
            return connection.execute(sql, params).fetchall()

    async def test_daily_claim_is_once_per_24_hours(self) -> None:
        claimed_at = datetime(2026, 11, 16, 14, 30, tzinfo=UTC)
        first = await self.store.claim_daily(1, 1000, claimed_at)
        self.assertTrue(first.claimed)
        self.assertEqual(first.balance, 1000)
        self.assertEqual(first.next_claim, claimed_at + timedelta(hours=24))

        # A few minutes later the cooldown is still running.
        second = await self.store.claim_daily(
            1, 1000, claimed_at + timedelta(minutes=5)
        )
        self.assertFalse(second.claimed)
        self.assertEqual(second.balance, 1000)
        self.assertEqual(second.next_claim, claimed_at + timedelta(hours=24))

        # One second before the cooldown ends it is still blocked.
        almost = await self.store.claim_daily(
            1, 1000, claimed_at + timedelta(hours=24, seconds=-1)
        )
        self.assertFalse(almost.claimed)

        # Once 24 hours have passed, the claim works again, and the timer
        # restarts from that new claim rather than a fixed UTC boundary.
        later = claimed_at + timedelta(hours=24, minutes=37)
        third = await self.store.claim_daily(1, 1000, later)
        self.assertTrue(third.claimed)
        self.assertEqual(third.balance, 2000)
        self.assertEqual(third.next_claim, later + timedelta(hours=24))

        blocked = await self.store.claim_daily(
            1, 1000, later + timedelta(hours=23)
        )
        self.assertFalse(blocked.claimed)
        self.assertEqual(blocked.next_claim, later + timedelta(hours=24))

    async def test_profile_tracks_daily_and_poker_progress(self) -> None:
        claimed_at = datetime(2026, 11, 16, 14, 30, tzinfo=UTC)
        profile = await self.store.profile(1, claimed_at)
        self.assertTrue(profile.can_claim_daily)
        self.assertEqual(profile.hands_played, 0)

        await self.store.claim_daily(1, 1000, claimed_at)
        profile = await self.store.profile(1, claimed_at + timedelta(hours=1))
        self.assertFalse(profile.can_claim_daily)
        self.assertEqual(profile.balance, 1000)
        self.assertEqual(
            profile.next_claim, claimed_at + timedelta(hours=24)
        )

        profile = await self.store.profile(
            1, claimed_at + timedelta(hours=24)
        )
        self.assertTrue(profile.can_claim_daily)

        await self.store.record_games_started([1, 2])
        await self.store.record_hand_stats(
            [
                HandStat(1, True, 300, 200),
                HandStat(2, False, 0, -200),
            ]
        )
        await self.store.record_hand_stats(
            [
                HandStat(1, False, 0, -100),
                HandStat(2, True, 200, 100),
            ]
        )

        profile = await self.store.profile(1, claimed_at)
        self.assertEqual(profile.games_played, 1)
        self.assertEqual(profile.hands_played, 2)
        self.assertEqual(profile.hands_won, 1)
        self.assertEqual(profile.biggest_pot, 300)
        self.assertEqual(profile.net_chips, 100)
        self.assertAlmostEqual(profile.hand_win_rate, 0.5)

    async def test_players_are_recorded_with_names(self) -> None:
        await self.store.remember_user(1, "alice", "Alice")
        await self.store.remember_user(1, "alice_renamed", "Alice Renamed")

        rows = self.query(
            "SELECT user_id, username, display_name, added_at, updated_at, chips "
            "FROM users"
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["user_id"], 1)
        self.assertEqual(rows[0]["username"], "alice_renamed")
        self.assertEqual(rows[0]["display_name"], "Alice Renamed")
        self.assertEqual(rows[0]["chips"], 0)
        self.assertIsNotNone(rows[0]["added_at"])
        self.assertGreaterEqual(rows[0]["updated_at"], rows[0]["added_at"])

        # The name refresh must not touch the wallet.
        await self.store.credit(1, 250)
        await self.store.remember_user(1, "alice_third", "Alice Third")
        self.assertEqual(await self.store.balance(1), 250)
        self.assertEqual(
            self.query("SELECT username FROM users WHERE user_id = 1")[0][
                "username"
            ],
            "alice_third",
        )

    async def test_servers_are_tracked_and_kicks_are_recorded(self) -> None:
        await self.store.sync_guilds([(100, "Alpha"), (200, "Beta")])

        rows = self.query(
            "SELECT guild_id, name, added_at, removed_at "
            "FROM discord_guilds ORDER BY guild_id"
        )
        self.assertEqual([row["guild_id"] for row in rows], [100, 200])
        self.assertTrue(all(row["removed_at"] is None for row in rows))
        self.assertIsNotNone(rows[0]["added_at"])

        # Beta was removed while the bot was offline; Alpha was renamed.
        await self.store.sync_guilds([(100, "Alpha Renamed")])
        rows = self.query(
            "SELECT guild_id, name, removed_at FROM discord_guilds "
            "ORDER BY guild_id"
        )
        self.assertEqual(rows[0]["name"], "Alpha Renamed")
        self.assertIsNone(rows[0]["removed_at"])
        self.assertIsNotNone(rows[1]["removed_at"])

        # Beta is added back.
        await self.store.sync_guilds([(100, "Alpha Renamed"), (200, "Beta")])
        rows = self.query(
            "SELECT guild_id, removed_at FROM discord_guilds ORDER BY guild_id"
        )
        self.assertIsNone(rows[1]["removed_at"])

        events = self.query(
            "SELECT guild_id, event FROM guild_events ORDER BY id"
        )
        self.assertEqual(
            [(row["guild_id"], row["event"]) for row in events],
            [
                (100, "added"),
                (200, "added"),
                (200, "removed"),
                (200, "added"),
            ],
        )

    async def test_live_server_join_and_remove_are_recorded(self) -> None:
        await self.store.record_guild_added(300, "Gamma")
        await self.store.record_guild_removed(300, "Gamma")

        row = self.query(
            "SELECT name, added_at, removed_at FROM discord_guilds "
            "WHERE guild_id = 300"
        )[0]
        self.assertEqual(row["name"], "Gamma")
        self.assertIsNotNone(row["added_at"])
        self.assertIsNotNone(row["removed_at"])
        events = self.query(
            "SELECT event FROM guild_events WHERE guild_id = 300 ORDER BY id"
        )
        self.assertEqual([event["event"] for event in events], ["added", "removed"])

    async def test_debit_is_atomic_and_credit_adds(self) -> None:
        self.assertIsNone(await self.store.debit(2, 500))
        await self.store.credit(2, 700)
        self.assertEqual(await self.store.balance(2), 700)
        self.assertEqual(await self.store.debit(2, 500), 200)
        self.assertIsNone(await self.store.debit(2, 500))
        self.assertEqual(await self.store.balance(2), 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
