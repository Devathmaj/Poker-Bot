"""Discord orchestration for a no-limit Texas Hold'em cash game."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from dataclasses import dataclass
from io import BytesIO
from typing import TYPE_CHECKING

import discord
from pokerkit.hands import StandardHighHand

from .config import (
    MAX_BUY_IN,
    MAX_PLAYERS,
    MAX_TIMEOUTS,
    MIN_BUY_IN,
    MIN_PLAYERS,
    SHOWDOWN_PAUSE,
    STREET_PAUSE,
    TURN_SECONDS,
    blinds_for_buy_in,
)
from .engine import Hand, HandResult, IllegalAction, Player, Table
from .store import HandStat
from .ui import ActionView, CardsView, LobbyView
from .visuals import (
    PlayerVisual,
    TableVisual,
    render_help_chart,
    render_hole_cards,
    render_table,
)

if TYPE_CHECKING:
    from .bot import PokerBot

logger = logging.getLogger(__name__)

EMBED_COLOR = 0x145A43
GOLD = 0xE8BC5C
RED = 0xD8584E

RANK_WORD = {
    "A": "Ace",
    "K": "King",
    "Q": "Queen",
    "J": "Jack",
    "T": "Ten",
    "9": "Nine",
    "8": "Eight",
    "7": "Seven",
    "6": "Six",
    "5": "Five",
    "4": "Four",
    "3": "Three",
    "2": "Two",
}
RANK_PLURAL = {
    "A": "Aces",
    "K": "Kings",
    "Q": "Queens",
    "J": "Jacks",
    "T": "Tens",
    "9": "Nines",
    "8": "Eights",
    "7": "Sevens",
    "6": "Sixes",
    "5": "Fives",
    "4": "Fours",
    "3": "Threes",
    "2": "Twos",
}
RANK_ORDER = "23456789TJQKA"


def ordinal(number: int) -> str:
    """Return ``1st``, ``2nd``, ``3rd``, ``4th``, ... ``21st``."""
    if 10 <= number % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def describe_hole_cards(cards: list[str]) -> str:
    if len(cards) < 2:
        return "No cards"
    first, second = cards[0], cards[1]
    if first[0] == second[0]:
        return f"Pocket {RANK_PLURAL.get(first[0], first[0])}"
    high, low = first, second
    if RANK_ORDER.index(low[0]) > RANK_ORDER.index(high[0]):
        high, low = low, high
    suffix = "suited" if high[-1] == low[-1] else "offsuit"
    return f"{RANK_WORD.get(high[0], high[0])}-{RANK_WORD.get(low[0], low[0])} {suffix}"


def best_hand_label(hole_cards: list[str], board: list[str]) -> str:
    if len(board) >= 3:
        try:
            hand = StandardHighHand.from_game(
                "".join(hole_cards), "".join(board)
            )
            return hand.entry.label.value
        except (ValueError, IndexError):
            pass
    return describe_hole_cards(hole_cards)


@dataclass
class PendingTurn:
    user_id: int
    future: asyncio.Future


class PokerGame:
    def __init__(
        self,
        bot: PokerBot,
        channel: discord.TextChannel,
        host: discord.abc.User,
        buy_in: int,
    ) -> None:
        self.bot = bot
        self.channel = channel
        self.host_id = host.id
        self.buy_in = buy_in
        self.small_blind, self.big_blind = blinds_for_buy_in(buy_in)
        self.table = Table([], self.small_blind, self.big_blind)

        self.state = "lobby"
        self.lobby_message: discord.Message | None = None
        self.table_message: discord.Message | None = None
        self.cards_message: discord.Message | None = None
        self.pending: PendingTurn | None = None
        self.task: asyncio.Task | None = None
        self._settled = False
        self._settling = False
        self._settle_lock = asyncio.Lock()

    # ------------------------------------------------------------------ setup
    @classmethod
    async def create(
        cls,
        bot: PokerBot,
        channel: discord.TextChannel,
        host: discord.abc.User,
        buy_in: int,
    ) -> tuple[PokerGame | None, str | None]:
        if channel.id in bot.games:
            return None, "There is already a poker table in this channel."

        if not MIN_BUY_IN <= buy_in <= MAX_BUY_IN:
            return None, (
                f"Buy-in must be between **{MIN_BUY_IN:,}** and "
                f"**{MAX_BUY_IN:,}** chips."
            )

        new_balance = await bot.store.debit(host.id, buy_in)
        if new_balance is None:
            balance = await bot.store.balance(host.id)
            return None, (
                f"You need **{buy_in:,}** chips to host a game, but you have "
                f"**{balance:,}**. Use `/daily` to claim more."
            )

        bot.track_user(host)
        game = cls(bot, channel, host, buy_in)
        game.table.players.append(
            Player(host.id, host.display_name, buy_in)
        )
        bot.games[channel.id] = game
        await game._post_lobby()
        return game, None

    def _lobby_embed(self) -> discord.Embed:
        seats = "\n".join(
            f"`{index + 1}.` <@{player.user_id}> — {player.chips:,} chips"
            for index, player in enumerate(self.table.players)
        )
        embed = discord.Embed(
            title="♠ Texas Hold'em — Lobby",
            description=(
                f"**Host:** <@{self.host_id}>\n"
                f"**Buy-in:** **{self.buy_in:,}** chips\n"
                f"**Blinds:** {self.small_blind:,}/{self.big_blind:,}\n\n"
                "Type `join game` in this channel or press **Join game**.\n"
                "The host starts with `start game` or **Start game**."
            ),
            color=EMBED_COLOR,
        )
        embed.add_field(
            name=f"Players ({len(self.table.players)}/{MAX_PLAYERS})",
            value=seats or "—",
            inline=False,
        )
        if len(self.table.players) < MIN_PLAYERS:
            footer = "Waiting for at least one more player."
        else:
            footer = "Ready. The host can start at any time."
        embed.set_footer(text=footer)
        return embed

    async def _post_lobby(self) -> None:
        self.lobby_message = await self.channel.send(
            embed=self._lobby_embed(), view=LobbyView(self)
        )

    async def _update_lobby(self) -> None:
        if self.lobby_message is None:
            return
        try:
            await self.lobby_message.edit(
                embed=self._lobby_embed(), view=LobbyView(self)
            )
        except discord.HTTPException:
            logger.warning("Could not update lobby in %s", self.channel.id)

    # ------------------------------------------------------------- membership
    async def join(self, user: discord.abc.User) -> tuple[bool, str]:
        if self.state != "lobby":
            return False, "This game has already started."
        if any(player.user_id == user.id for player in self.table.players):
            return False, "You are already sitting at this table."
        if len(self.table.players) >= MAX_PLAYERS:
            return False, "This table is full."

        new_balance = await self.bot.store.debit(user.id, self.buy_in)
        if new_balance is None:
            balance = await self.bot.store.balance(user.id)
            return False, (
                f"You need **{self.buy_in:,}** chips to join, but you have "
                f"**{balance:,}**. Use `/daily` to claim more."
            )

        self.bot.track_user(user)
        self.table.players.append(
            Player(user.id, user.display_name, self.buy_in)
        )
        await self._update_lobby()
        return True, (
            f"✅ **{user.display_name}** joined with **{self.buy_in:,}** chips. "
            f"Wallet: **{new_balance:,}**."
        )

    async def start(self, user: discord.abc.User) -> tuple[bool, str]:
        """Validate the start request and announce the table.

        The caller sends the returned message, then calls :meth:`launch` so the
        hand reference lands directly underneath it before the first hand.
        """
        if user.id != self.host_id:
            return False, "Only the host can start this game."
        if self.state != "lobby":
            return False, "This game has already started."
        if len(self.table.players) < MIN_PLAYERS:
            return False, f"You need at least {MIN_PLAYERS} players to start."

        self.state = "running"
        try:
            await self.bot.store.record_games_started(
                [player.user_id for player in self.table.players]
            )
        except Exception:
            logger.exception("Could not record games started")

        if self.lobby_message is not None:
            with suppress(discord.HTTPException):
                await self.lobby_message.edit(
                    embed=discord.Embed(
                        title="♠ Texas Hold'em — Cards in the air",
                        description=(
                            f"**{len(self.table.players)} players** · "
                            f"**{self.buy_in:,}** chip stacks · "
                            f"blinds **{self.small_blind:,}/{self.big_blind:,}**\n\n"
                            "Your hole cards are always shown privately in "
                            "this channel — press **View my cards** or use "
                            "`/cards`.\n\n"
                            "On your turn, type `check`, `call`, "
                            "`raise <amount>`, `all in`, or `fold`."
                        ),
                        color=EMBED_COLOR,
                    ),
                    view=None,
                )
        return True, "▶️ Game started. Good luck!"

    async def post_hand_chart(self) -> None:
        """Post just the enlarged chart below the game-start message."""
        try:
            image = await asyncio.to_thread(render_help_chart, 2)
            await self.channel.send(
                file=discord.File(
                    BytesIO(image), filename="hand-rankings.png"
                )
            )
        except discord.HTTPException:
            logger.warning(
                "Could not post the hand chart in %s", self.channel.id
            )

    async def launch(self) -> None:
        """Post the enlarged hand chart, then deal the first hand."""
        await self.post_hand_chart()
        self.task = asyncio.create_task(
            self._run(), name=f"poker-game-{self.channel.id}"
        )

    # -------------------------------------------------------------- game loop
    async def _run(self) -> None:
        try:
            while self.state == "running":
                if len(self.table.active_players()) < MIN_PLAYERS:
                    await self._settle(
                        "Only one player still has chips — the table is closing."
                    )
                    return

                hand = self.table.start_hand()
                await self._begin_hand()
                await self._play_hand(hand)
                if self.state != "running":
                    return

                result = self.table.finish_hand()
                await self._record_hand_stats(hand, result)
                await self._announce_result(result)

                if len(self.table.active_players()) < MIN_PLAYERS:
                    await self._settle(
                        "Only one player still has chips — the table is closing."
                    )
                    return

                await asyncio.sleep(SHOWDOWN_PAUSE)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Poker game crashed in channel %s", self.channel.id)
            await self._settle(
                f"The table stopped after an internal error ({type(exc).__name__})."
            )

    async def _begin_hand(self) -> None:
        self.pending = None
        self.table_message = None

        embed = self._table_embed()
        image = await asyncio.to_thread(render_table, self._visual())
        file = discord.File(BytesIO(image), filename="table.png")
        embed.set_image(url="attachment://table.png")
        self.table_message = await self.channel.send(embed=embed, file=file)

        if self.cards_message is not None:
            with suppress(discord.HTTPException):
                await self.cards_message.edit(view=None)
        self.cards_message = await self.channel.send(
            "🃏 **Hole cards are in.** Press **View my cards** or use "
            "`/cards` — only you can see them.",
            view=CardsView(self),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _play_hand(self, hand: Hand) -> None:
        while self.state == "running" and not hand.is_over:
            if hand.deal_pending_board() is not None:
                await self._update_table()
                await asyncio.sleep(STREET_PAUSE)
                continue

            actor_id = hand.actor_id
            if actor_id is None:
                logger.warning("Hand %s stalled with no actor", self.table.hand_number)
                return
            actor = self.table.player(actor_id)
            if actor is None:
                logger.warning(
                    "Hand %s has an unknown actor (%s)",
                    self.table.hand_number,
                    actor_id,
                )
                return
            if actor.kicked:
                # A removed player should never hold the turn, but if they do,
                # keep the hand moving rather than waiting on them forever.
                if not self._auto_act(hand, actor_id):
                    logger.warning(
                        "Hand %s could not be advanced past a removed player",
                        self.table.hand_number,
                    )
                    return
                continue
            await self._prompt_turn(actor_id)

    async def _prompt_turn(self, actor_id: int) -> None:
        hand = self.table.hand
        player = self.table.player(actor_id)
        if hand is None or player is None or player.kicked:
            return

        future = asyncio.get_running_loop().create_future()
        pending = PendingTurn(actor_id, future)
        self.pending = pending
        deadline = time.time() + TURN_SECONDS
        await self._update_table(actor_id=actor_id, deadline=deadline)

        try:
            action, amount = await asyncio.wait_for(future, TURN_SECONDS)
        except TimeoutError:
            if self.pending is pending:
                self.pending = None
            await self._handle_timeout(player)
            return
        except asyncio.CancelledError:
            if self.pending is pending:
                self.pending = None
            raise
        finally:
            if self.pending is pending:
                self.pending = None

        try:
            hand.act(actor_id, action, amount)
        except IllegalAction as exc:
            await self.channel.send(
                f"⚠️ **{player.name}**: {exc}",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        if not hand.is_over:
            await asyncio.sleep(0.35)

    def _auto_act(self, hand: Hand, user_id: int) -> str | None:
        """Resolve a player's turn on their behalf.

        Folding is the normal timeout action, but PokerKit refuses to fold a
        player who can check for free (there is nothing to fold to). In that
        case the player auto-checks instead, otherwise the hand would never
        advance and the turn would be handed back to the same player forever.
        Returns the action applied, or ``None`` if the hand did not move on.
        """
        if hand.is_over or hand.actor_id != user_id:
            return None
        try:
            if hand.state.can_fold():
                hand.act(user_id, "fold")
                return "fold"
            if hand.state.can_check_or_call():
                hand.act(user_id, "check")
                return "check"
        except IllegalAction:
            logger.exception("Could not auto-act for %s", user_id)
        return None

    async def _handle_timeout(self, player: Player) -> None:
        hand = self.table.hand
        if hand is None or player.kicked:
            return

        player.timeouts += 1
        removed = player.timeouts > MAX_TIMEOUTS
        forfeited = self.table.forfeit(player.user_id) if removed else 0
        action = self._auto_act(hand, player.user_id)

        if removed:
            embed = discord.Embed(
                title="🚫 Player removed",
                description=(
                    f"<@{player.user_id}> timed out for the "
                    f"**{ordinal(player.timeouts)}** time "
                    f"(limit: {MAX_TIMEOUTS}).\n"
                    f"They were removed from the table and their remaining "
                    f"**{forfeited:,}** chips went into the pot."
                ),
                color=RED,
            )
            await self.channel.send(embed=embed)
        else:
            done = "auto-checked" if action == "check" else "auto-folded"
            await self.channel.send(
                f"⏰ <@{player.user_id}> timed out "
                f"({player.timeouts}/{MAX_TIMEOUTS}) — {done}."
            )

        if not hand.is_over:
            await asyncio.sleep(0.8)

    async def _record_hand_stats(
        self, hand: Hand, result: HandResult
    ) -> None:
        winners = {user_id for user_id, _ in result.winners}
        pot = result.pot + result.dead_money
        stats = [
            HandStat(
                user_id=player.user_id,
                won=player.user_id in winners,
                pot_won=pot if player.user_id in winners else 0,
                net=player.chips
                - hand.start_stacks.get(player.user_id, player.chips),
            )
            for player in hand.order
        ]
        try:
            await self.bot.store.record_hand_stats(stats)
        except Exception:
            logger.exception("Could not record hand stats")

    async def _announce_result(self, result: HandResult) -> None:
        await self._update_table(result=result)
        await asyncio.sleep(0.4)

    # ---------------------------------------------------------------- actions
    def is_pending_actor(self, user_id: int) -> bool:
        return (
            self.pending is not None
            and self.pending.user_id == user_id
            and not self.pending.future.done()
        )

    def raise_bounds(self, user_id: int) -> tuple[int, int] | None:
        hand = self.table.hand
        if (
            self.state != "running"
            or hand is None
            or not self.is_pending_actor(user_id)
        ):
            return None
        try:
            legal = hand.legal_actions()
        except IllegalAction:
            return None
        if (
            not legal.can_raise
            or legal.min_raise_to is None
            or legal.max_raise_to is None
        ):
            return None
        return legal.min_raise_to, legal.max_raise_to

    async def submit_action(
        self, user: discord.abc.User, action: str, amount: int | None = None
    ) -> tuple[bool, str]:
        pending = self.pending
        hand = self.table.hand
        if self.state != "running" or hand is None:
            return False, "This game is no longer running."
        if pending is None or pending.user_id != user.id:
            return False, "It is not your turn."
        if pending.future.done():
            return False, "That turn has already been resolved."

        try:
            hand.validate_action(user.id, action, amount)
        except IllegalAction as exc:
            return False, str(exc)

        pending.future.set_result((action, amount))
        if action == "fold":
            label = "Fold"
        elif action in {"check", "call"}:
            label = "Check/Call"
        elif action == "raise":
            label = f"Raise to {amount:,}"
        else:
            label = "All-in"
        return True, f"✅ {label} submitted."

    # -------------------------------------------------------------- rendering
    def _visual(self, result: HandResult | None = None) -> TableVisual:
        hand = self.table.hand
        assert hand is not None
        showdown = result is not None and len(result.revealed) >= 2
        players: list[PlayerVisual] = []

        for index, player in enumerate(hand.order):
            in_hand = hand.state.statuses[index]
            stack = hand.state.stacks[index]
            bet = hand.state.bets[index]
            hole: list[str] | None
            status: str
            chips = stack

            if result is not None:
                chips = 0 if player.kicked else player.chips
                bet = 0
                if player.kicked:
                    status = "removed"
                elif chips <= 0:
                    status = "busted"
                elif player.user_id not in result.revealed:
                    status = "folded"
                else:
                    status = "active"
                hole = (
                    hand.hole_cards.get(player.user_id, [])
                    if showdown and player.user_id in result.revealed
                    else []
                )
            else:
                if player.kicked:
                    status = "removed"
                elif not in_hand:
                    status = "folded"
                elif stack <= 0:
                    status = "all-in"
                else:
                    status = "active"
                hole = None if in_hand and not player.kicked else []

            players.append(
                PlayerVisual(
                    name=player.name,
                    chips=chips,
                    bet=bet,
                    status=status,
                    is_turn=(
                        result is None
                        and hand.actor_id == player.user_id
                        and not hand.is_over
                    ),
                    is_button=(
                        self.table.button_player is not None
                        and self.table.button_player.user_id == player.user_id
                    ),
                    hole=hole,
                )
            )

        if result is None:
            pot = hand.pot + self.table.bonus_pot
            street = hand.street
        else:
            pot = result.pot + result.dead_money
            street = "Showdown" if showdown else hand.street

        return TableVisual(
            hand_number=self.table.hand_number,
            street=street,
            pot=pot,
            board=hand.board,
            players=players,
            blinds=(self.small_blind, self.big_blind),
            buy_in=self.buy_in,
        )

    def _table_embed(
        self,
        actor_id: int | None = None,
        deadline: float | None = None,
        result: HandResult | None = None,
    ) -> discord.Embed:
        hand = self.table.hand
        assert hand is not None
        embed = discord.Embed(
            title=f"♠ Texas Hold'em · Hand #{self.table.hand_number}",
            color=GOLD if result is not None else EMBED_COLOR,
        )

        if result is not None:
            showdown = len(result.revealed) >= 2
            shown_street = "Showdown" if showdown else hand.street
            embed.description = (
                f"**{shown_street}** · pot **"
                f"{result.pot + result.dead_money:,}** chips"
            )
            if result.winners:
                lines = []
                for user_id, amount in result.winners:
                    label = result.hand_labels.get(user_id)
                    line = f"🏆 <@{user_id}> **+{amount:,}**"
                    if label:
                        line += f" · *{label}*"
                    lines.append(line)
                embed.add_field(
                    name="Winner" + ("s" if len(result.winners) > 1 else ""),
                    value="\n".join(lines),
                    inline=False,
                )
            else:
                embed.add_field(name="Result", value="No winner.", inline=False)
            if result.dead_money:
                embed.add_field(
                    name="Dead money",
                    value=(
                        f"**{result.dead_money:,}** chips forfeited by "
                        "removed players were added to the pot."
                    ),
                    inline=False,
                )
        else:
            if actor_id is not None and deadline is not None:
                embed.description = (
                    f"**{hand.street}** · pot **{hand.pot + self.table.bonus_pot:,}**\n"
                    f"<@{actor_id}> is up · <t:{int(deadline)}:R>\n"
                    "Type `check`, `call`, `raise <amount>`, `all in`, "
                    "or `fold`."
                )
            else:
                embed.description = (
                    f"**{hand.street}** · pot **{hand.pot + self.table.bonus_pot:,}**"
                )

        embed.set_image(url="attachment://table.png")
        embed.set_footer(
            text=(
                f"Buy-in {self.buy_in:,} · blinds "
                f"{self.small_blind:,}/{self.big_blind:,} · "
                f"{TURN_SECONDS}s per turn · {MAX_TIMEOUTS} timeouts allowed"
            )
        )
        return embed

    async def _update_table(
        self,
        actor_id: int | None = None,
        deadline: float | None = None,
        result: HandResult | None = None,
    ) -> None:
        hand = self.table.hand
        if hand is None:
            return

        image = await asyncio.to_thread(
            render_table, self._visual(result=result)
        )
        file = discord.File(BytesIO(image), filename="table.png")
        embed = self._table_embed(
            actor_id=actor_id, deadline=deadline, result=result
        )
        view = (
            ActionView(self)
            if result is None and self.pending is not None
            else None
        )

        if self.table_message is None:
            self.table_message = await self.channel.send(
                embed=embed, file=file, view=view
            )
            return

        try:
            await self.table_message.edit(
                embed=embed, attachments=[file], view=view
            )
        except discord.NotFound:
            self.table_message = await self.channel.send(
                embed=embed, file=file, view=view
            )
        except discord.HTTPException:
            logger.exception("Could not update table message")

    # ---------------------------------------------------------------- cash out
    def cards_image(self, user_id: int) -> tuple[bytes, str] | None:
        hand = self.table.hand
        if self.state != "running" or hand is None:
            return None
        hole = hand.hole_cards.get(user_id)
        if not hole:
            return None
        label = best_hand_label(hole, hand.board)
        image = render_hole_cards(
            hole,
            subtitle=label,
            note=(
                f"Hand #{self.table.hand_number} · {hand.street} · "
                f"pot {hand.pot + self.table.bonus_pot:,}"
            ),
        )
        return image, label

    async def end(self, reason: str) -> bool:
        if self._settled:
            return False
        self.state = "ended"
        task = self.task
        if task is not None and task is not asyncio.current_task() and not task.done():
            if self._settling:
                # The game loop is already cashing everybody out. Let it
                # finish instead of interrupting a half-completed settlement.
                with suppress(asyncio.CancelledError):
                    await task
            else:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        await self._settle(reason)
        return True

    async def _settle(self, reason: str) -> None:
        async with self._settle_lock:
            if self._settled:
                return
            self._settled = True
            self._settling = True
            self.state = "ended"

            # Remove the table from the registry before any awaits so a
            # canceled shutdown can never leave a zombie game behind.
            self.bot.games.pop(self.channel.id, None)
            self.pending = None

            refunds = self.table.abort_refunds()
            for user_id, amount in refunds.items():
                if amount > 0:
                    await self.bot.store.credit(user_id, amount)

            if self.table_message is not None:
                with suppress(discord.HTTPException):
                    await self.table_message.edit(view=None)
            if self.cards_message is not None:
                with suppress(discord.HTTPException):
                    await self.cards_message.edit(view=None)
            if self.lobby_message is not None:
                with suppress(discord.HTTPException):
                    await self.lobby_message.edit(view=None)

            embed = discord.Embed(
                title="♠ Table closed",
                description=reason,
                color=EMBED_COLOR,
            )
            if refunds:
                lines = [
                    f"<@{user_id}> — **+{amount:,}** chips"
                    for user_id, amount in refunds.items()
                    if amount > 0
                ]
                if lines:
                    embed.add_field(
                        name="Cashed out",
                        value="\n".join(lines)[:1024],
                        inline=False,
                    )
            removed = [p for p in self.table.players if p.kicked]
            if removed:
                embed.add_field(
                    name="Forfeited",
                    value=(
                        "Removed after repeated timeouts: "
                        + ", ".join(f"<@{p.user_id}>" for p in removed)
                    ),
                    inline=False,
                )
            embed.set_footer(text="Thanks for playing. Use /creategame for another table.")
            with suppress(discord.HTTPException):
                await self.channel.send(embed=embed)
