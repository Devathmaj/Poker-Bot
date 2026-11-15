"""Discord UI components for the poker game."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from .config import TURN_SECONDS

if TYPE_CHECKING:
    from .game import PokerGame


class LobbyView(discord.ui.View):
    def __init__(self, game: PokerGame) -> None:
        super().__init__(timeout=None)
        self.game = game

    async def _valid(self, interaction: discord.Interaction) -> bool:
        if (
            self.game.lobby_message is None
            or interaction.message is None
            or interaction.message.id != self.game.lobby_message.id
        ):
            await interaction.response.send_message(
                "This lobby is no longer active.", ephemeral=True
            )
            return False
        if self.game.state != "lobby":
            await interaction.response.send_message(
                "This game has already started.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Join game", style=discord.ButtonStyle.success)
    async def join(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not await self._valid(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        _ok, message = await self.game.join(interaction.user)
        await interaction.followup.send(message, ephemeral=True)

    @discord.ui.button(label="Start game", style=discord.ButtonStyle.primary)
    async def start(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not await self._valid(interaction):
            return
        if interaction.user.id != self.game.host_id:
            await interaction.response.send_message(
                "Only the host can start the game.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        _ok, message = await self.game.start(interaction.user)
        await interaction.followup.send(message, ephemeral=True)


class RaiseModal(discord.ui.Modal, title="Raise the bet"):
    amount = discord.ui.TextInput(
        label="Raise to (total chips)",
        placeholder="Enter an amount",
        required=True,
        max_length=12,
    )

    def __init__(
        self,
        game: PokerGame,
        user: discord.abc.User,
        minimum: int,
        maximum: int,
    ) -> None:
        super().__init__()
        self.game = game
        self.user = user
        self.amount.placeholder = f"{minimum:,} – {maximum:,}"

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message(
                "This raise window belongs to another player.", ephemeral=True
            )
            return
        raw = str(self.amount.value).replace(",", "").replace(" ", "").strip()
        try:
            value = int(raw)
        except ValueError:
            await interaction.response.send_message(
                "Enter a whole number of chips.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        _ok, message = await self.game.submit_action(
            interaction.user, "raise", value
        )
        await interaction.followup.send(message, ephemeral=True)


class ActionView(discord.ui.View):
    def __init__(self, game: PokerGame) -> None:
        super().__init__(timeout=TURN_SECONDS + 20)
        self.game = game

        hand = game.table.hand
        legal = None
        if hand is not None and hand.actor_id is not None:
            legal = hand.legal_actions()

        call_label = "Call"
        if legal is not None:
            call_label = (
                "Check" if legal.call_amount == 0 else f"Call {legal.call_amount:,}"
            )

        self.add_item(
            self._action_button("Fold", discord.ButtonStyle.danger, "fold")
        )
        self.add_item(
            self._action_button(
                call_label, discord.ButtonStyle.primary, "call"
            )
        )
        raise_button = self._action_button(
            "Raise",
            discord.ButtonStyle.secondary,
            "raise",
            disabled=legal is None or not legal.can_raise,
        )
        raise_button.callback = self._raise
        self.add_item(raise_button)
        self.add_item(
            self._action_button(
                "All-in", discord.ButtonStyle.secondary, "allin"
            )
        )

    def _action_button(self, label, style, action, *, disabled=False):
        button = discord.ui.Button(label=label, style=style, disabled=disabled)

        async def callback(interaction: discord.Interaction) -> None:
            await self._submit(interaction, action)

        button.callback = callback
        return button

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if self.game.state != "running":
            await interaction.response.send_message(
                "This game has ended.", ephemeral=True
            )
            return False
        if (
            self.game.table_message is None
            or interaction.message is None
            or interaction.message.id != self.game.table_message.id
        ):
            await interaction.response.send_message(
                "This table view is out of date.", ephemeral=True
            )
            return False
        if not self.game.is_pending_actor(interaction.user.id):
            await interaction.response.send_message(
                "It is not your turn.", ephemeral=True
            )
            return False
        return True

    async def _submit(self, interaction: discord.Interaction, action: str) -> None:
        if not await self._guard(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        _ok, message = await self.game.submit_action(interaction.user, action)
        await interaction.followup.send(message, ephemeral=True)

    async def _raise(self, interaction: discord.Interaction) -> None:
        if not await self._guard(interaction):
            return
        bounds = self.game.raise_bounds(interaction.user.id)
        if bounds is None:
            await interaction.response.send_message(
                "You cannot raise right now.", ephemeral=True
            )
            return
        minimum, maximum = bounds
        await interaction.response.send_modal(
            RaiseModal(self.game, interaction.user, minimum, maximum)
        )
