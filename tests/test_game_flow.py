"""Headless smoke tests for the Discord game loop using fake objects."""

from __future__ import annotations

import asyncio
import itertools
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pokerbot.game import PokerGame
from pokerbot.store import ChipStore


class FakeUser:
    _ids = itertools.count(1000)

    def __init__(self, name: str) -> None:
        self.id = next(self._ids)
        self.display_name = name
        self.dms: list = []

    async def send(self, **kwargs):
        self.dms.append(kwargs)


class FakeMessage:
    _ids = itertools.count(1)

    def __init__(self, content=None, **kwargs) -> None:
        self.id = next(self._ids)
        self.content = content
        self.edits: list[dict] = []
        self.kwargs = kwargs

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        return self


class FakeChannel:
    def __init__(self) -> None:
        self.id = 424242
        self.messages: list[FakeMessage] = []

    async def send(self, content=None, **kwargs):
        message = FakeMessage(content, **kwargs)
        self.messages.append(message)
        return message


class FakeBot:
    def __init__(self, store: ChipStore) -> None:
        self.store = store
        self.games: dict[int, PokerGame] = {}


class GameFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().set_debug(False)
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ChipStore(Path(self._tmp.name) / "chips.sqlite3")
        self.bot = FakeBot(self.store)
        self.channel = FakeChannel()
        self.host = FakeUser("Host")
        self.guest = FakeUser("Guest")
        await self.store.credit(self.host.id, 1000)
        await self.store.credit(self.guest.id, 1000)

    async def asyncTearDown(self) -> None:
        for game in list(self.bot.games.values()):
            await game.end("test cleanup")
        await self.store.close()
        self._tmp.cleanup()

    async def _new_table(self) -> PokerGame:
        game, error = await PokerGame.create(
            self.bot, self.channel, self.host, 500
        )
        self.assertIsNone(error)
        self.assertIsNotNone(game)
        ok, message = await game.join(self.guest)
        self.assertTrue(ok, message)
        ok, message = await game.start(self.host)
        self.assertTrue(ok, message)
        return game

    async def _next_actor(self, game: PokerGame, timeout: float = 5.0) -> int | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if game.state == "ended":
                return None
            if game.pending is not None and not game.pending.future.done():
                return game.pending.user_id
            await asyncio.sleep(0.01)
        raise AssertionError("the game did not ask anybody to act")

    async def test_check_call_hand_completes_and_chips_cash_out(self) -> None:
        with patch("pokerbot.game.SHOWDOWN_PAUSE", 0.01), patch(
            "pokerbot.game.STREET_PAUSE", 0.01
        ):
            game = await self._new_table()
            users = {self.host.id: self.host, self.guest.id: self.guest}

            for _ in range(120):
                actor = await self._next_actor(game)
                if actor is None:
                    break
                ok, message = await game.submit_action(users[actor], "call")
                self.assertTrue(ok, message)
                await asyncio.sleep(0.01)
                if game.table.hand_number >= 2:
                    break

            self.assertGreaterEqual(game.table.hand_number, 1)
            await game.end("test over")
            self.assertTrue(game._settled)
            self.assertNotIn(self.channel.id, self.bot.games)

        total = await self.store.balance(self.host.id) + await self.store.balance(
            self.guest.id
        )
        self.assertEqual(total, 2000)
        self.assertTrue(any(m.kwargs.get("embed") for m in self.channel.messages))

    async def test_repeated_timeouts_remove_player_and_close_table(self) -> None:
        # The production threshold is 3 timeout warnings, then removal on the
        # fourth. Lower it here so the full removal path runs quickly.
        with patch("pokerbot.game.TURN_SECONDS", 0.05), patch(
            "pokerbot.game.MAX_TIMEOUTS", 1
        ), patch("pokerbot.game.SHOWDOWN_PAUSE", 0.01), patch(
            "pokerbot.game.STREET_PAUSE", 0.01
        ):
            game = await self._new_table()
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 20
            while loop.time() < deadline and game.state != "ended":
                await asyncio.sleep(0.05)

            self.assertEqual(game.state, "ended")
            self.assertTrue(any(p.kicked for p in game.table.players))
            self.assertTrue(game._settled)

        total = await self.store.balance(self.host.id) + await self.store.balance(
            self.guest.id
        )
        self.assertEqual(total, 2000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
