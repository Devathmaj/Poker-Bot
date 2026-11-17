"""Refusals are only shown to the player who asked."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import discord

from pokerbot.cog import PokerCog
from pokerbot.game import PokerGame
from pokerbot.ui import LobbyView


class FakeUser:
    def __init__(self, user_id: int, name: str = "Player") -> None:
        self.id = user_id
        self.display_name = name


class FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.id = message_id


class FakeResponse:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.deferred: list[dict] = []

    async def defer(self, **kwargs) -> None:
        self.deferred.append(kwargs)

    async def send_message(self, content=None, **kwargs) -> None:
        self.sent.append({"content": content, **kwargs})

    def is_done(self) -> bool:
        return bool(self.sent or self.deferred)


class FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, content=None, **kwargs) -> None:
        self.sent.append({"content": content, **kwargs})


class FakeInteraction:
    def __init__(self, user: FakeUser, message: FakeMessage | None = None) -> None:
        self.user = user
        self.message = message
        self.response = FakeResponse()
        self.followup = FakeFollowup()


class FakeGame:
    def __init__(self, host_id: int = 1, state: str = "lobby") -> None:
        self.host_id = host_id
        self.state = state
        self.lobby_message = FakeMessage(77)
        self.join_result = (True, "joined")
        self.start_result = (True, "started")
        self.launched = False

    async def join(self, user: FakeUser) -> tuple[bool, str]:
        return self.join_result

    async def start(self, user: FakeUser) -> tuple[bool, str]:
        return self.start_result

    async def launch(self) -> None:
        self.launched = True


class LobbyPrivacyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.game = FakeGame()
        self.view = LobbyView(self.game)
        self.message = FakeMessage(77)

    async def test_start_refusal_is_private(self) -> None:
        interaction = FakeInteraction(FakeUser(2), self.message)

        await self.view.start.callback(interaction)

        self.assertEqual(
            interaction.response.sent,
            [
                {
                    "content": "Only the host can start the game.",
                    "ephemeral": True,
                }
            ],
        )
        self.assertFalse(interaction.followup.sent)

    async def test_join_refusal_is_private(self) -> None:
        self.game.join_result = (
            False,
            (
                "You need **1,000** chips to join, but you have **0**. "
                "Use `/daily` to claim more."
            ),
        )
        interaction = FakeInteraction(FakeUser(2), self.message)

        await self.view.join.callback(interaction)

        self.assertTrue(interaction.followup.sent[-1]["ephemeral"])
        self.assertIn("You need", interaction.followup.sent[-1]["content"])

    async def test_join_confirmation_stays_public(self) -> None:
        interaction = FakeInteraction(FakeUser(2), self.message)

        await self.view.join.callback(interaction)

        self.assertFalse(
            interaction.followup.sent[-1].get("ephemeral", False)
        )

    async def test_start_refusal_from_the_game_is_private(self) -> None:
        self.game.start_result = (
            False,
            "You need at least 2 players to start.",
        )
        interaction = FakeInteraction(FakeUser(1), self.message)

        await self.view.start.callback(interaction)

        self.assertTrue(interaction.followup.sent[-1]["ephemeral"])
        self.assertFalse(self.game.launched)

    async def test_stale_lobby_button_is_private(self) -> None:
        interaction = FakeInteraction(FakeUser(2), FakeMessage(999))

        await self.view.join.callback(interaction)

        self.assertTrue(interaction.response.sent[-1]["ephemeral"])


class DailyReplyPrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def test_daily_reply_is_private(self) -> None:
        async def claim_daily(user_id: int, amount: int):
            return SimpleNamespace(
                claimed=True,
                balance=1_000,
                next_claim=datetime.now(UTC) + timedelta(hours=24),
            )

        cog = PokerCog.__new__(PokerCog)
        cog.bot = SimpleNamespace(store=SimpleNamespace(claim_daily=claim_daily))
        interaction = FakeInteraction(FakeUser(3))

        await PokerCog.daily.callback(cog, interaction)

        self.assertEqual(interaction.response.deferred, [{"ephemeral": True}])
        self.assertFalse(interaction.followup.sent[-1].get("ephemeral", False))


class GameNoticePrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def test_illegal_action_notice_is_never_posted_in_the_channel(
        self,
    ) -> None:
        member = SimpleNamespace(sent=[])

        async def send(content=None, **kwargs) -> None:
            member.sent.append({"content": content, **kwargs})

        member.send = send
        guild = SimpleNamespace(get_member=lambda user_id: member)
        # Deliberately no channel.send: a public post would raise instead of
        # silently leaking the refusal.
        game = PokerGame.__new__(PokerGame)
        game.channel = SimpleNamespace(guild=guild)

        await game._notify_player(7, "⚠️ That raise is too small.")

        self.assertEqual(
            member.sent[-1]["content"], "⚠️ That raise is too small."
        )


def closed_dm_error() -> discord.Forbidden:
    response = SimpleNamespace(status=403, reason="Forbidden")
    return discord.Forbidden(response, "Cannot send messages to this user.")


class FakeAuthor:
    def __init__(self, dms_open: bool = True) -> None:
        self.dms_open = dms_open
        self.sent: list[dict] = []

    async def send(self, content=None, **kwargs) -> None:
        if not self.dms_open:
            raise closed_dm_error()
        self.sent.append({"content": content, **kwargs})


class FakeChatMessage:
    def __init__(self, author: FakeAuthor) -> None:
        self.author = author
        self.replies: list[dict] = []
        self.reactions: list[str] = []

    async def reply(self, content=None, **kwargs) -> None:
        self.replies.append({"content": content, **kwargs})

    async def add_reaction(self, emoji: str) -> None:
        self.reactions.append(emoji)


class ChatRefusalPrivacyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # _reply_private never touches the bot instance.
        self.cog = PokerCog.__new__(PokerCog)

    async def test_chat_refusal_is_dmed(self) -> None:
        author = FakeAuthor()
        message = FakeChatMessage(author)

        await self.cog._reply_private(message, "It is not your turn.")

        self.assertEqual(author.sent[-1]["content"], "It is not your turn.")
        self.assertIsNotNone(author.sent[-1]["allowed_mentions"])
        self.assertEqual(message.reactions, ["❌"])
        self.assertFalse(message.replies)

    async def test_closed_dms_fall_back_to_a_channel_reply(self) -> None:
        author = FakeAuthor(dms_open=False)
        message = FakeChatMessage(author)

        await self.cog._reply_private(message, "It is not your turn.")

        self.assertFalse(author.sent)
        self.assertEqual(
            message.replies[0]["content"], "It is not your turn."
        )
        self.assertFalse(message.replies[0]["mention_author"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
