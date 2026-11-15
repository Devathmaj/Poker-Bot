"""Tests for the PostgreSQL chip wallet."""

from __future__ import annotations

import os
import unittest
import uuid
from datetime import date
from pathlib import Path
from urllib.parse import quote

import psycopg
from dotenv import load_dotenv

from pokerbot.store import ChipStore

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

    async def test_daily_claim_is_once_per_day(self) -> None:
        first = await self.store.claim_daily(1, 1000, date(2026, 11, 16))
        self.assertTrue(first.claimed)
        self.assertEqual(first.balance, 1000)

        second = await self.store.claim_daily(1, 1000, date(2026, 11, 16))
        self.assertFalse(second.claimed)
        self.assertEqual(second.balance, 1000)

        tomorrow = await self.store.claim_daily(1, 1000, date(2026, 11, 17))
        self.assertTrue(tomorrow.claimed)
        self.assertEqual(tomorrow.balance, 2000)

    async def test_debit_is_atomic_and_credit_adds(self) -> None:
        self.assertIsNone(await self.store.debit(2, 500))
        await self.store.credit(2, 700)
        self.assertEqual(await self.store.balance(2), 700)
        self.assertEqual(await self.store.debit(2, 500), 200)
        self.assertIsNone(await self.store.debit(2, 500))
        self.assertEqual(await self.store.balance(2), 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
