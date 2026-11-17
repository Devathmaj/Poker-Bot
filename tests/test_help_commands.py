"""The bot guide and the poker guide are separate commands."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from pokerbot.cog import PokerCog

TINY_PNG = b"png"


class FakeResponse:
    def __init__(self) -> None:
        self.deferred: list[dict] = []

    async def defer(self, **kwargs) -> None:
        self.deferred.append(kwargs)


class FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, content=None, **kwargs) -> None:
        self.sent.append({"content": content, **kwargs})


class FakeInteraction:
    def __init__(self) -> None:
        self.response = FakeResponse()
        self.followup = FakeFollowup()


class HelpCommandTests(unittest.IsolatedAsyncioTestCase):
    async def _embed(self, command: str):
        cog = PokerCog.__new__(PokerCog)
        interaction = FakeInteraction()
        with patch("pokerbot.cog.render_help_chart", return_value=TINY_PNG):
            await getattr(PokerCog, command).callback(cog, interaction)
        return interaction.followup.sent[-1]["embed"]

    def test_both_commands_are_registered(self) -> None:
        names = {command.name for command in PokerCog.__cog_app_commands__}
        self.assertIn("help", names)
        self.assertIn("howtoplay", names)

    async def test_help_is_bot_help_and_has_no_image(self) -> None:
        embed = await self._embed("help")
        text = "\n".join(field.value for field in embed.fields)

        self.assertIn("Poker Bot", embed.title)
        self.assertIsNone(embed.image.url)
        self.assertIn("Quick start", [field.name for field in embed.fields])
        self.assertIn("/howtoplay", text)
        self.assertIn("/hands", text)

    async def test_howtoplay_is_the_poker_guide_with_the_chart(self) -> None:
        embed = await self._embed("howtoplay")
        fields = {field.name: field.value for field in embed.fields}
        actions = fields["Your actions"].lower()

        self.assertEqual(embed.image.url, "attachment://hand-rankings.png")
        self.assertIn("Goal", embed.description)
        self.assertIn("The deal", fields)
        for action in ("check", "call", "raise", "all-in", "fold"):
            with self.subTest(action=action):
                self.assertIn(action, actions)
        self.assertIn("/hands", fields["Hand rankings"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
