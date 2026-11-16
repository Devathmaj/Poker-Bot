"""Discord client setup."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from contextlib import suppress
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from .cog import PokerCog
from .config import DATA_DIR, DATABASE_URL, GUILD_ID
from .store import ChipStore

if TYPE_CHECKING:
    from .game import PokerGame

logger = logging.getLogger(__name__)

# Refresh a player's stored name at most once an hour per process.
_TRACK_REFRESH_SECONDS = 3600

# Shown in the member list while the bot is connected.
PRESENCE_TEXT = "Dealing cards | /help"

# Discord limits global command overwrites to a couple of requests per day, so
# the command payload is fingerprinted and only pushed when it actually changed.
_COMMAND_STAMP = DATA_DIR / "command_sync.json"


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
        self.tree.on_error = self.on_app_command_error
        self.tree.interaction_check = self._track_interaction
        self._tracked_users: dict[int, float] = {}
        self._background: set[asyncio.Task] = set()

    # --------------------------------------------------------------- audience
    async def _track_interaction(
        self, interaction: discord.Interaction
    ) -> bool:
        self.track_user(interaction.user)
        return True

    def track_user(self, user: discord.abc.User) -> None:
        """Remember a player who used the bot.

        The write happens in the background so it can never slow down the
        three-second interaction acknowledgement. Names are refreshed at most
        once an hour per process.
        """
        now = time.monotonic()
        last = self._tracked_users.get(user.id)
        if last is not None and now - last < _TRACK_REFRESH_SECONDS:
            return
        self._tracked_users[user.id] = now
        task = asyncio.create_task(self._remember_user(user))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _remember_user(self, user: discord.abc.User) -> None:
        try:
            await self.store.remember_user(
                user.id,
                getattr(user, "name", None) or user.display_name,
                getattr(user, "display_name", None),
            )
        except Exception:
            # Drop the cache entry so the next interaction tries again.
            self._tracked_users.pop(user.id, None)
            logger.exception("Could not record user %s", user.id)

    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        """Answer the interaction even when a command raises.

        An unhandled exception before the acknowledgement turns into Discord's
        generic "The application did not respond" message, which hides the
        real error and leaves the player with no feedback.
        """
        original: BaseException = error
        while isinstance(original, app_commands.CommandInvokeError):
            original = original.original
        logger.error(
            "Command /%s failed",
            getattr(interaction.command, "qualified_name", "?"),
            exc_info=original,
        )

        message = (
            "Something went wrong while handling that command. "
            "It has been logged — please try again."
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            logger.debug("Could not deliver the error response", exc_info=True)

    # ---------------------------------------------------------------- commands
    def _command_fingerprint(self) -> str:
        """Hash the current slash-command payload."""
        payload = [
            command.to_dict(self.tree) for command in self.tree.get_commands()
        ]
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _last_command_fingerprint(self) -> str | None:
        try:
            data = json.loads(_COMMAND_STAMP.read_text(encoding="utf-8"))
            return data["fingerprint"]
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _remember_command_fingerprint(self, fingerprint: str) -> None:
        try:
            _COMMAND_STAMP.parent.mkdir(parents=True, exist_ok=True)
            _COMMAND_STAMP.write_text(
                json.dumps({"fingerprint": fingerprint}), encoding="utf-8"
            )
        except OSError:
            logger.debug(
                "Could not store the command sync fingerprint", exc_info=True
            )

    async def _global_commands_match(self) -> bool:
        """Check that Discord still lists the commands we have locally."""
        try:
            registered = {command.name for command in await self.tree.fetch_commands()}
        except discord.HTTPException:
            # Without an answer, assume a sync is needed.
            return False
        return registered == {command.name for command in self.tree.get_commands()}

    async def _sync_commands(self) -> None:
        """Publish slash commands globally and, optionally, to one guild.

        Commands are registered globally so Discord lists them in the bot's
        profile under "Commands". Because global syncs are rate limited, they
        are skipped when the payload is unchanged. A guild copy is still
        registered when GUILD_ID is set, so updates show up instantly while
        developing.
        """
        fingerprint = self._command_fingerprint()
        up_to_date = fingerprint == self._last_command_fingerprint()
        if up_to_date and await self._global_commands_match():
            logger.info("Global slash commands already up to date")
        else:
            try:
                await self.tree.sync()
            except discord.HTTPException:
                logger.exception("Could not sync global slash commands")
            else:
                self._remember_command_fingerprint(fingerprint)
                logger.info("Global slash commands synced")

        if not GUILD_ID:
            return
        try:
            guild = discord.Object(id=int(GUILD_ID))
        except ValueError:
            logger.warning("GUILD_ID is not a number; skipping the guild sync")
            return

        self.tree.copy_global_to(guild=guild)
        try:
            await self.tree.sync(guild=guild)
            logger.info("Slash commands synced to guild %s", GUILD_ID)
        except discord.HTTPException:
            # A failed sync should not take the whole bot offline; the
            # previously registered commands keep working.
            logger.exception("Could not sync slash commands to guild %s", GUILD_ID)

    async def setup_hook(self) -> None:
        await self.add_cog(PokerCog(self))
        await self._sync_commands()

    async def on_ready(self) -> None:
        user = self.user
        logger.info("Logged in as %s (%s)", user, getattr(user, "id", "?"))

        # Re-sent on every connect so reconnects keep the status.
        try:
            await self.change_presence(
                activity=discord.CustomActivity(name=PRESENCE_TEXT),
                status=discord.Status.online,
            )
        except discord.HTTPException:
            logger.exception("Could not update the bot's status")

        # Reconcile the server list every time we connect, so joins and kicks
        # that happened while the bot was offline are still recorded.
        guilds = [(guild.id, guild.name) for guild in self.guilds]
        try:
            await self.store.sync_guilds(guilds)
            logger.info("Tracking %s server(s)", len(guilds))
        except Exception:
            logger.exception("Could not sync the server list")

    async def on_guild_join(self, guild: discord.Guild) -> None:
        try:
            await self.store.record_guild_added(guild.id, guild.name)
        except Exception:
            logger.exception("Could not record joining %s", guild.id)
        logger.info("Added to server %s (%s)", guild.name, guild.id)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        try:
            await self.store.record_guild_removed(guild.id, guild.name)
        except Exception:
            logger.exception("Could not record leaving %s", guild.id)
        logger.info("Removed from server %s (%s)", guild.name, guild.id)

    async def close(self) -> None:
        # Never strand a table's chips if the bot is stopped mid-game.
        for game in list(self.games.values()):
            with suppress(Exception):
                await game.end("The bot is shutting down; remaining stacks were cashed out.")
        # Let pending bookkeeping writes finish before the pool is closed.
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)
        with suppress(Exception):
            await self.store.close()
        await super().close()
