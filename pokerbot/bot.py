"""Discord client setup."""

from __future__ import annotations

import logging
from contextlib import suppress
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from .cog import PokerCog
from .config import DATABASE_URL, GUILD_ID
from .store import ChipStore

if TYPE_CHECKING:
    from .game import PokerGame

logger = logging.getLogger(__name__)


class PokerBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None,
        )
        if not DATABASE_URL:
            raise RuntimeError(
                "DATABASE_URL is missing. Add your PostgreSQL connection string "
                "to .env."
            )
        self.store = ChipStore(DATABASE_URL)
        self.games: dict[int, PokerGame] = {}

    async def setup_hook(self) -> None:
        await self.add_cog(PokerCog(self))
        if GUILD_ID:
            try:
                guild = discord.Object(id=int(GUILD_ID))
            except ValueError:
                logger.warning("GUILD_ID is not a number; syncing globally")
                await self.tree.sync()
            else:
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                logger.info("Slash commands synced to guild %s", GUILD_ID)
        else:
            await self.tree.sync()
            logger.info("Global slash commands synced")

    async def on_ready(self) -> None:
        user = self.user
        logger.info("Logged in as %s (%s)", user, getattr(user, "id", "?"))

    async def close(self) -> None:
        # Never strand a table's chips if the bot is stopped mid-game.
        for game in list(self.games.values()):
            with suppress(Exception):
                await game.end("The bot is shutting down; remaining stacks were cashed out.")
        with suppress(Exception):
            await self.store.close()
        await super().close()
