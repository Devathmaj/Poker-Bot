"""Slash commands and channel listeners."""

from __future__ import annotations

import asyncio
import re
from contextlib import suppress
from datetime import UTC, datetime
from io import BytesIO
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from .config import (
    DAILY_CHIPS,
    DEFAULT_BUY_IN,
    MAX_BUY_IN,
    MAX_HANDS,
    MIN_BUY_IN,
    MIN_HANDS,
)
from .game import PokerGame, hands_label
from .hands import hand_rankings_embed
from .visuals import render_help_chart

if TYPE_CHECKING:
    from .bot import PokerBot

EMBED_COLOR = 0x145A43
GOLD = 0xE8BC5C

_RAISE_PATTERN = re.compile(r"(?:raise|bet)(?:\s+to)?\s+(\d[\d,]*)")


def parse_action(content: str) -> tuple[str, int | None] | None:
    """Parse an exact chat action, returning ``None`` for anything else."""
    text = " ".join(content.strip().lower().split())
    if text in {"fold", "check", "call"}:
        return text, None
    if text in {"all in", "all-in", "allin"}:
        return "allin", None

    match = _RAISE_PATTERN.fullmatch(text)
    if match is None:
        return None
    amount = int(match.group(1).replace(",", ""))
    if amount <= 0:
        return None
    return "raise", amount


def format_countdown(until: datetime, now: datetime | None = None) -> str:
    """Describe a cooldown precisely, like ``in 23h 45m``.

    Discord's relative timestamps only get as precise as "in a day" at this
    range, which hides how long is actually left, so daily timers are rendered
    as hours and minutes instead.
    """
    remaining = until - (now or datetime.now(UTC))
    seconds = int(remaining.total_seconds())
    if seconds <= 0:
        return "ready now"
    # Round up so a cooldown that is seconds away never shows "0m".
    minutes = seconds // 60 + (1 if seconds % 60 else 0)
    hours, minutes = divmod(minutes, 60)
    if not hours:
        return f"in {minutes}m"
    if not minutes:
        return f"in {hours}h"
    return f"in {hours}h {minutes:02d}m"


class PokerCog(commands.Cog):
    def __init__(self, bot: PokerBot) -> None:
        self.bot = bot

    # ---------------------------------------------------------------- economy
    @app_commands.command(
        name="daily",
        description=f"Claim {DAILY_CHIPS:,} chips every 24 hours.",
    )
    async def daily(self, interaction: discord.Interaction) -> None:
        # Acknowledge before touching the database. Discord only gives an
        # interaction three seconds, and the wallet lives on a remote
        # PostgreSQL host, so an un-acknowledged query shows up in Discord as
        # "The application did not respond".
        # Wallet details are personal, and a public reply would push the
        # live table up the channel.
        await interaction.response.defer(ephemeral=True)
        claim = await self.bot.store.claim_daily(
            interaction.user.id, DAILY_CHIPS
        )
        if claim.claimed:
            embed = discord.Embed(
                title="💰 Daily chips claimed",
                description=f"You received **{DAILY_CHIPS:,}** chips.",
                color=GOLD,
            )
            embed.add_field(
                name="Balance", value=f"**{claim.balance:,}** chips", inline=True
            )
            embed.add_field(
                name="Next claim",
                value=format_countdown(claim.next_claim),
                inline=True,
            )
        else:
            embed = discord.Embed(
                title="Daily already claimed",
                description=(
                    f"You already collected your daily chips.\n"
                    f"Next claim: {format_countdown(claim.next_claim)}"
                ),
                color=EMBED_COLOR,
            )
            embed.add_field(
                name="Balance", value=f"**{claim.balance:,}** chips", inline=True
            )
        await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="balance",
        description="Check how many chips you have, or another player's.",
    )
    @app_commands.describe(user="Check another player's balance (optional).")
    async def balance(
        self,
        interaction: discord.Interaction,
        user: discord.User | None = None,
    ) -> None:
        target = user or interaction.user
        await interaction.response.defer()
        balance = await self.bot.store.balance(target.id)
        if target.id == interaction.user.id:
            embed = discord.Embed(
                title="💰 Your balance",
                description=f"You have **{balance:,}** chips.",
                color=GOLD,
            )
        else:
            embed = discord.Embed(
                title=f"💰 {target.display_name}'s balance",
                description=f"`{target}` has **{balance:,}** chips.",
                color=GOLD,
            )
            embed.set_thumbnail(url=target.display_avatar.url)
        await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="hands",
        description="Show every poker hand, strongest to weakest.",
    )
    async def hands(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        embed = hand_rankings_embed()
        embed.set_image(url="attachment://hand-rankings.png")
        image = await asyncio.to_thread(render_help_chart)
        await interaction.followup.send(
            embed=embed,
            file=discord.File(BytesIO(image), filename="hand-rankings.png"),
        )

    # ----------------------------------------------------------------- profile
    @app_commands.command(
        name="profile",
        description="View your poker profile, or another player's profile.",
    )
    @app_commands.describe(user="View another player's profile (optional).")
    async def profile(
        self,
        interaction: discord.Interaction,
        user: discord.User | None = None,
    ) -> None:
        target = user or interaction.user
        await interaction.response.defer()
        data = await self.bot.store.profile(target.id)

        embed = discord.Embed(
            title=f"♠ {target.display_name}'s Poker Profile",
            description=f"`{target}`",
            color=EMBED_COLOR,
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(
            name="Wallet", value=f"**{data.balance:,}** chips", inline=True
        )
        if data.can_claim_daily:
            daily = "✅ Ready — use `/daily`"
        else:
            daily = f"⏳ {format_countdown(data.next_claim)}"
        embed.add_field(name="Daily", value=daily, inline=True)
        embed.add_field(
            name="Games played", value=f"{data.games_played:,}", inline=True
        )
        embed.add_field(
            name="Hands played", value=f"{data.hands_played:,}", inline=True
        )
        embed.add_field(
            name="Hands won", value=f"{data.hands_won:,}", inline=True
        )
        embed.add_field(
            name="Hand win rate",
            value=f"{data.hand_win_rate * 100:.1f}%",
            inline=True,
        )
        embed.add_field(
            name="Biggest pot won",
            value=f"{data.biggest_pot:,}",
            inline=True,
        )
        embed.add_field(
            name="Net chips",
            value=f"{data.net_chips:+,} chips",
            inline=True,
        )
        embed.set_footer(
            text=(
                f"Requested by {interaction.user.display_name} · "
                "stats update as hands finish"
            )
        )
        await interaction.followup.send(embed=embed)

    # ------------------------------------------------------------------- help
    @app_commands.command(
        name="help",
        description="Texas Hold'em rules, commands, and a hand-ranking chart.",
    )
    async def help(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        embed = discord.Embed(
            title="♠ Texas Hold'em — Rules & Commands",
            description=(
                "**Goal:** Make the best five-card hand from your two private "
                "hole cards and the five shared community cards."
            ),
            color=EMBED_COLOR,
        )
        embed.add_field(
            name="The hand",
            value=(
                "Everyone gets two hole cards. The dealer reveals the flop "
                "(3 cards), turn (1), then river (1). There are four betting "
                "rounds: pre-flop, flop, turn, river."
            ),
            inline=False,
        )
        embed.add_field(
            name="Betting",
            value=(
                "No-limit. On your turn you can **Fold**, **Check/Call**, "
                "**Raise**, or go **All-in**. A raise must be at least the big "
                "blind (or the size of the previous raise). Side pots and "
                "all-ins are handled automatically."
            ),
            inline=False,
        )
        embed.add_field(
            name="Chips",
            value=(
                f"Use `/daily` every 24 hours for **{DAILY_CHIPS:,}** chips. "
                f"Check your wallet any time with `/balance`. "
                f"`/creategame [buy_in]` opens a table and takes the buy-in "
                f"(default {DEFAULT_BUY_IN:,}). Buy-in chips become your stack; "
                "at the end of the game your remaining stack is paid back to "
                "your wallet. Win a pot and keep the chips."
            ),
            inline=False,
        )
        embed.add_field(
            name="Game length",
            value=(
                "By default a table runs until only one player has chips left. "
                "Pass `hands` to `/creategame` to stop after that many hands; "
                "remaining stacks are cashed out when the limit is reached."
            ),
            inline=False,
        )
        embed.add_field(
            name="Commands",
            value=(
                "`/daily` — claim chips\n"
                "`/balance [user]` — check your or another player's chip balance\n"
                "`/profile [user]` — view your or another player's profile\n"
                "`/hands` — see every hand ranked strongest to weakest\n"
                "`/creategame [buy_in] [hands]` — open a table (host joins automatically)\n"
                "Type `join game` — take a seat in the lobby\n"
                "Type `start game` — host starts the game\n"
                "`/cards` — privately re-check your hole cards and best hand\n"
                "`/endgame` — host or server admin closes the table and cashes out\n"
                "During a hand, type exactly one of: `check`, `call`, "
                "`raise <amount>`, `all in`, or `fold`.\n"
                "The **Fold / Check / Call / Raise / All-in** buttons also work.\n"
                "Only dealt hole cards are private; everything else is public."
            ),
            inline=False,
        )
        embed.add_field(
            name="Turn timer",
            value=(
                "You have **60 seconds** to act. If you miss it, you are "
                "auto-folded. After more than **3 timeouts** you are removed "
                "from the table and your remaining chips are forfeited to the pot."
            ),
            inline=False,
        )
        embed.set_image(url="attachment://hand-rankings.png")
        embed.set_footer(text="Hand rankings are also shown below the commands.")
        image = await asyncio.to_thread(render_help_chart)
        await interaction.followup.send(
            embed=embed,
            file=discord.File(BytesIO(image), filename="hand-rankings.png"),
        )

    # ------------------------------------------------------------------ table
    @app_commands.command(
        name="creategame",
        description="Open a Texas Hold'em table in this channel.",
    )
    @app_commands.describe(
        buy_in=f"Chips each player brings (default {DEFAULT_BUY_IN:,}).",
        hands=(
            "Stop after this many hands. Omit to play until one player "
            "has chips left."
        ),
    )
    async def creategame(
        self,
        interaction: discord.Interaction,
        buy_in: app_commands.Range[int, MIN_BUY_IN, MAX_BUY_IN] = DEFAULT_BUY_IN,
        hands: app_commands.Range[int, MIN_HANDS, MAX_HANDS] | None = None,
    ) -> None:
        await interaction.response.defer()
        if interaction.guild is None or not isinstance(
            interaction.channel, discord.TextChannel
        ):
            await interaction.followup.send(
                "Create games in a normal server text channel.", ephemeral=True
            )
            return

        game, error = await PokerGame.create(
            self.bot, interaction.channel, interaction.user, buy_in, hands
        )
        if error:
            await interaction.followup.send(error, ephemeral=True)
            return

        length = (
            "**unlimited** hands"
            if hands is None
            else f"**{hands_label(hands)}**"
        )
        await interaction.followup.send(
            f"♠ Table created with a **{buy_in:,}** chip buy-in, blinds "
            f"**{game.small_blind:,}/{game.big_blind:,}**, and {length}.\n"
            "Others can type `join game` or press **Join game**."
        )

    @app_commands.command(
        name="endgame",
        description="End the table immediately (host or server admin).",
    )
    async def endgame(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        if interaction.guild is None or interaction.channel is None:
            await interaction.followup.send(
                "There is no table here.", ephemeral=True
            )
            return

        game = self.bot.games.get(interaction.channel.id)
        if game is None:
            await interaction.followup.send(
                "There is no active poker game in this channel.",
                ephemeral=True,
            )
            return

        is_admin = bool(
            isinstance(interaction.user, discord.Member)
            and (
                interaction.user.guild_permissions.administrator
                or interaction.user.guild_permissions.manage_guild
            )
        )
        if interaction.user.id != game.host_id and not is_admin:
            await interaction.followup.send(
                "Only the host or a server admin can end this game.",
                ephemeral=True,
            )
            return

        ended = await game.end(
            f"Game ended by <@{interaction.user.id}>."
        )
        if ended:
            await interaction.followup.send(
                "The table was closed and remaining stacks were cashed out."
            )
        else:
            await interaction.followup.send(
                "That game has already ended.", ephemeral=True
            )

    @app_commands.command(
        name="cards",
        description="Privately view your hole cards and current best hand.",
    )
    async def cards(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if interaction.channel is None:
            await interaction.followup.send("No table here.", ephemeral=True)
            return
        game = self.bot.games.get(interaction.channel.id)
        if game is None:
            await interaction.followup.send(
                "There is no poker game in this channel.", ephemeral=True
            )
            return
        data = game.cards_image(interaction.user.id)
        if data is None:
            await interaction.followup.send(
                "You are not in a live hand right now.", ephemeral=True
            )
            return
        image, label = data
        await interaction.followup.send(
            content=f"**Your hand:** {label}",
            file=discord.File(BytesIO(image), filename="your-hand.png"),
            ephemeral=True,
        )

    # --------------------------------------------------------------- listeners
    async def _reply_private(
        self, message: discord.Message, content: str
    ) -> None:
        """Deliver a refusal without posting it in the channel.

        Discord cannot make a reply to a plain chat message ephemeral, so the
        refusal goes to the player by DM and their message is marked with ❌.
        If DMs are closed there is no private channel left, so the reply falls
        back to the channel.
        """
        try:
            await message.author.send(
                content, allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.HTTPException:
            await message.reply(
                content,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            with suppress(discord.HTTPException):
                await message.add_reaction("❌")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        if not isinstance(message.channel, discord.TextChannel):
            return

        text = " ".join(message.content.strip().lower().split())
        game = self.bot.games.get(message.channel.id)

        if text not in {"join game", "start game"}:
            action = parse_action(message.content)
            if action is None or game is None or game.state != "running":
                return
            self.bot.track_user(message.author)
            action_name, amount = action
            ok, response = await game.submit_action(
                message.author, action_name, amount
            )
            if not ok:
                await self._reply_private(message, response)
                return
            try:
                await message.add_reaction("✅")
            except discord.HTTPException:
                await message.reply(response, mention_author=False)
            return

        if game is None:
            await self._reply_private(
                message,
                "There is no poker game here yet. Use `/creategame` to open one.",
            )
            return

        self.bot.track_user(message.author)
        if text == "join game":
            ok, response = await game.join(message.author)
        else:
            ok, response = await game.start(message.author)

        if not ok:
            await self._reply_private(message, response)
            return

        try:
            await message.reply(
                response,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        finally:
            if text == "start game":
                # Keep the chart directly under the start confirmation.
                await game.launch()
