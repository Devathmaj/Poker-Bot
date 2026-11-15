"""Tests for the SQLite chip wallet."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from pokerbot.store import ChipStore


class StoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ChipStore(Path(self._tmp.name) / "chips.sqlite3")

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmp.cleanup()

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
