"""Headless smoke tests for the Discord game loop using fake objects."""

from __future__ import annotations

import asyncio
import itertools
import unittest
from unittest.mock import patch

from pokerbot.game import PokerGame, ordinal
from pokerbot.ui import ActionView


class FakeUser:
    _ids = itertools.count(1000)

    def __init__(self, name: str) -> None:
        self.id = next(self._ids)
        self.display_name = name


class FakeMessage:
    _ids = itertools.count(1)

    def __init__(self, content=None, **kwargs) -> None:
        self.id = next(self._ids)
        self.content = content
        self.edits: list[dict] = []
        self.kwargs = kwargs
        self.deleted = False

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        return self

    async def delete(self):
        self.deleted = True


class FakeChannel:
    def __init__(self) -> None:
        self.id = 424242
        self.messages: list[FakeMessage] = []
        self.last_message_id: int | None = None

    async def send(self, content=None, **kwargs):
        message = FakeMessage(content, **kwargs)
        self.messages.append(message)
        self.last_message_id = message.id
        return message


def has_cards_button(message: FakeMessage) -> bool:
    view = message.kwargs.get("view")
    if not isinstance(view, ActionView):
        return False
    return any(
        getattr(item, "label", None) == "View my cards" for item in view.children
    )


class MemoryStore:
    """Small in-memory stand-in for the Postgres wallet."""

    def __init__(self) -> None:
        self.balances: dict[int, int] = {}

    async def balance(self, user_id: int) -> int:
        return self.balances.get(user_id, 0)

    async def debit(self, user_id: int, amount: int) -> int | None:
        balance = self.balances.get(user_id, 0)
        if balance < amount:
            return None
        self.balances[user_id] = balance - amount
        return self.balances[user_id]

    async def credit(self, user_id: int, amount: int) -> int:
        self.balances[user_id] = self.balances.get(user_id, 0) + amount
        return self.balances[user_id]

    async def record_games_started(self, user_ids: list[int]) -> None:
        pass

    async def record_hand_stats(self, stats: list) -> None:
        pass

    async def close(self) -> None:
        pass


class FakeBot:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store
        self.games: dict[int, PokerGame] = {}
        self.tracked: list[int] = []

    def track_user(self, user) -> None:
        self.tracked.append(user.id)


class GameFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().set_debug(False)
        self.store = MemoryStore()
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

    async def _new_table(self, hands: int | None = None) -> PokerGame:
        game, error = await PokerGame.create(
            self.bot, self.channel, self.host, 500, hands
        )
        self.assertIsNone(error)
        self.assertIsNotNone(game)
        ok, message = await game.join(self.guest)
        self.assertTrue(ok, message)
        ok, message = await game.start(self.host)
        self.assertTrue(ok, message)
        await game.launch()
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

    async def test_start_posts_hand_chart_before_first_hand(self) -> None:
        game = await self._new_table()

        self.assertEqual(game.state, "running")
        self.assertIsNotNone(game.task)
        # The lobby is message 0; the chart must be posted before the table
        # render so it sits directly under the game-start message.
        message = self.channel.messages[1]
        self.assertIsNone(message.kwargs.get("embed"))
        attachment = message.kwargs.get("file")
        self.assertIsNotNone(attachment)
        self.assertEqual(attachment.filename, "hand-rankings.png")

    async def test_table_message_carries_the_cards_button(self) -> None:
        with patch("pokerbot.game.SHOWDOWN_PAUSE", 0.01), patch(
            "pokerbot.game.STREET_PAUSE", 0.01
        ):
            game = await self._new_table()
            self.assertIsNotNone(await self._next_actor(game))

            table = game.table_message
            self.assertIsNotNone(table)
            self.assertTrue(has_cards_button(table))
            # No extra "hole cards" message is posted under the table.
            self.assertFalse(
                any(
                    "Hole cards are in" in str(message.content)
                    for message in self.channel.messages
                )
            )
            await game.end("test over")

    async def test_table_moves_to_the_bottom_below_chatter(self) -> None:
        with patch("pokerbot.game.SHOWDOWN_PAUSE", 0.01), patch(
            "pokerbot.game.STREET_PAUSE", 0.01
        ):
            game = await self._new_table()
            users = {self.host.id: self.host, self.guest.id: self.guest}
            actor = await self._next_actor(game)
            table = game.table_message
            self.assertIsNotNone(table)

            # A player typed their action in chat, leaving a message under
            # the table and pushing it up the channel.
            await self.channel.send("call")
            ok, message = await game.submit_action(users[actor], "call")
            self.assertTrue(ok, message)

            loop = asyncio.get_running_loop()
            deadline = loop.time() + 10
            while loop.time() < deadline and (
                game.table_message is table or game.table_message is None
            ):
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)

            # The table was re-posted as the newest message, ready for the
            # next player without any scrolling.
            self.assertIsNot(game.table_message, table)
            self.assertTrue(table.deleted)
            self.assertIs(self.channel.messages[-1], game.table_message)
            await game.end("test over")

    async def test_table_is_edited_in_place_without_chatter(self) -> None:
        with patch("pokerbot.game.SHOWDOWN_PAUSE", 0.01), patch(
            "pokerbot.game.STREET_PAUSE", 0.01
        ):
            game = await self._new_table()
            users = {self.host.id: self.host, self.guest.id: self.guest}
            actor = await self._next_actor(game)
            table = game.table_message
            edits_before = len(table.edits)

            ok, message = await game.submit_action(users[actor], "call")
            self.assertTrue(ok, message)

            loop = asyncio.get_running_loop()
            deadline = loop.time() + 5
            while loop.time() < deadline and len(table.edits) == edits_before:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)

            # Nothing was posted under the table, so it stays where it is.
            self.assertEqual(len(table.edits), edits_before + 1)
            self.assertIs(game.table_message, table)
            self.assertFalse(table.deleted)
            await game.end("test over")

    async def test_check_call_hand_completes_and_chips_cash_out(self) -> None:
        with patch("pokerbot.game.SHOWDOWN_PAUSE", 0.01), patch(
            "pokerbot.game.STREET_PAUSE", 0.01
        ):
            game = await self._new_table()
            users = {self.host.id: self.host, self.guest.id: self.guest}

            for _ in range(120):
                actor = await self._next_actor(game)
                if actor is not None:
                    # The hole-card button lives on the table message, so a
                    # hand never needs a separate card message below it.
                    self.assertTrue(
                        has_cards_button(game.table_message)
                    )
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

    async def test_create_game_refuses_a_buy_in_above_the_wallet(self) -> None:
        self.assertEqual(await self.store.balance(self.host.id), 1000)

        game, error = await PokerGame.create(
            self.bot, self.channel, self.host, 1001
        )

        self.assertIsNone(game)
        self.assertIn("You need **1,001** chips", error)
        self.assertEqual(await self.store.balance(self.host.id), 1000)
        self.assertNotIn(self.channel.id, self.bot.games)

    async def test_create_game_rejects_out_of_range_buy_ins(self) -> None:
        for buy_in in (0, -1, 99, 100_001):
            with self.subTest(buy_in=buy_in):
                game, error = await PokerGame.create(
                    self.bot, self.channel, self.host, buy_in
                )
                self.assertIsNone(game)
                self.assertIn("Buy-in must be between", error)
        self.assertEqual(await self.store.balance(self.host.id), 1000)
        self.assertNotIn(self.channel.id, self.bot.games)

    async def test_create_game_rejects_out_of_range_hand_limits(self) -> None:
        for hands in (0, -1, 1001):
            with self.subTest(hands=hands):
                game, error = await PokerGame.create(
                    self.bot, self.channel, self.host, 500, hands
                )
                self.assertIsNone(game)
                self.assertIn("Hand limit must be between", error)
        self.assertEqual(await self.store.balance(self.host.id), 1000)
        self.assertNotIn(self.channel.id, self.bot.games)

    async def test_hand_limit_closes_table_after_configured_hands(self) -> None:
        with patch("pokerbot.game.SHOWDOWN_PAUSE", 0.01), patch(
            "pokerbot.game.STREET_PAUSE", 0.01
        ):
            game = await self._new_table(hands=1)
            self.assertEqual(game.hands_limit, 1)
            users = {self.host.id: self.host, self.guest.id: self.guest}

            loop = asyncio.get_running_loop()
            deadline = loop.time() + 10
            while loop.time() < deadline and game.state != "ended":
                actor = await self._next_actor(game)
                if actor is not None:
                    ok, message = await game.submit_action(
                        users[actor], "call"
                    )
                    self.assertTrue(ok, message)
                await asyncio.sleep(0.01)

            self.assertEqual(game.state, "ended")
            self.assertEqual(game.table.hand_number, 1)
            self.assertTrue(game._settled)
            self.assertNotIn(self.channel.id, self.bot.games)

            closed = [
                message.kwargs["embed"].description
                for message in self.channel.messages
                if message.kwargs.get("embed") is not None
                and getattr(message.kwargs["embed"], "title", "")
                == "♠ Table closed"
            ]
            self.assertTrue(closed)
            self.assertIn("Hand limit reached (1 hand)", closed[-1])

        total = await self.store.balance(self.host.id) + await self.store.balance(
            self.guest.id
        )
        self.assertEqual(total, 2000)

    async def test_timeout_that_cannot_fold_does_not_stall_the_hand(self) -> None:
        # A player who has nothing to call cannot fold in PokerKit. The bot
        # must auto-check instead, or the same player is prompted (and
        # eventually removed) over and over while the hand never advances.
        png = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
            b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        with patch("pokerbot.game.TURN_SECONDS", 0.2), patch(
            "pokerbot.game.MAX_TIMEOUTS", 0
        ), patch("pokerbot.game.SHOWDOWN_PAUSE", 0.01), patch(
            "pokerbot.game.STREET_PAUSE", 0.01
        ), patch("pokerbot.game.render_table", return_value=png), patch(
            "pokerbot.game.render_help_chart", return_value=png
        ):
            game = await self._new_table()
            users = {self.host.id: self.host, self.guest.id: self.guest}

            loop = asyncio.get_running_loop()
            deadline = loop.time() + 10
            while loop.time() < deadline and game.state != "ended":
                pending = game.pending
                # Hand 1: both players act normally. On hand 2 the guest
                # (small blind) limps, which leaves the host (big blind)
                # with a free check — the timeout there must auto-check.
                if (
                    pending is not None
                    and not pending.future.done()
                    and (
                        game.table.hand_number == 1
                        or pending.user_id == self.guest.id
                    )
                ):
                    await game.submit_action(users[pending.user_id], "call")
                await asyncio.sleep(0.01)

            removed = [
                message
                for message in self.channel.messages
                if message.kwargs.get("embed") is not None
                and "Player removed"
                in str(getattr(message.kwargs["embed"], "title", ""))
            ]
            self.assertEqual(len(removed), 1)
            self.assertEqual(game.state, "ended")

            # The table must close normally after the hand finishes, not
            # because the stuck hand crashed the game loop.
            closed = [
                message.kwargs["embed"].description
                for message in self.channel.messages
                if message.kwargs.get("embed") is not None
                and getattr(message.kwargs["embed"], "title", "")
                == "♠ Table closed"
            ]
            self.assertTrue(closed)
            self.assertNotIn("internal error", closed[-1])

        total = await self.store.balance(self.host.id) + await self.store.balance(
            self.guest.id
        )
        self.assertEqual(total, 2000)

    def test_ordinal_suffixes(self) -> None:
        expected = {
            1: "1st",
            2: "2nd",
            3: "3rd",
            4: "4th",
            11: "11th",
            12: "12th",
            13: "13th",
            21: "21st",
            22: "22nd",
            23: "23rd",
            111: "111th",
        }
        for number, word in expected.items():
            with self.subTest(number=number):
                self.assertEqual(ordinal(number), word)


if __name__ == "__main__":
    unittest.main(verbosity=2)
