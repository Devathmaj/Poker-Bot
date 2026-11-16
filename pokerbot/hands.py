"""Accurate hand-ranking reference shared by ``/hands`` and game starts."""

from __future__ import annotations

import discord

from .visuals import HELP_RANKINGS

EMBED_COLOR = 0x145A43

_SUIT_SYMBOLS = {"s": "♠", "h": "♥", "d": "♦", "c": "♣"}
_TEN = "10"

# Printed next to each row so new players can see what the hand beats, not
# just its place in the list. ``HELP_RANKINGS`` is already ordered strongest
# to weakest, so the two tuples must stay aligned.
_STRENGTH = (
    "the highest straight flush",
    "beats four of a kind",
    "beats a full house",
    "beats a flush",
    "beats a straight",
    "beats three of a kind",
    "beats two pair",
    "beats one pair",
    "beats high card",
    "the weakest hand",
)


def format_cards(cards: tuple[str, ...]) -> str:
    """Render card codes such as ``("As", "Th")`` as ``A♠ 10♥``."""
    return " ".join(
        f"{_TEN if card[0] == 'T' else card[0]}{_SUIT_SYMBOLS[card[1]]}"
        for card in cards
    )


def hand_rankings_embed() -> discord.Embed:
    """Build the shared, strongest-to-weakest hand reference."""
    rows = []
    for index, (row, strength) in enumerate(
        zip(HELP_RANKINGS, _STRENGTH), start=1
    ):
        cards = format_cards(row.cards)
        rows.append(
            f"**{index}. {row.name}** · {strength}\n"
            f"{cards} — {row.description}"
        )

    embed = discord.Embed(
        title="♠ Poker hand rankings",
        description=(
            "Your hand is the best **five cards** you can make from your two "
            "hole cards and the five community cards. Ranked strongest (1) to "
            "weakest (10):\n\n" + "\n\n".join(rows)
        ),
        color=EMBED_COLOR,
    )
    embed.add_field(
        name="Ties, kickers, and straights",
        value=(
            "• If two players have the same kind of hand, compare the cards "
            "that make it, then the kickers (**A-A-K** beats **A-A-Q**).\n"
            "• If all five cards are equal, the pot is split.\n"
            "• A-2-3-4-5 is the lowest straight; A-K-Q-J-10 is the highest.\n"
            "• Suits are never stronger than one another.\n"
            "• You can use both, one, or none of your hole cards — the best "
            "five cards win.\n"
            "• Hands do not pay fixed chip amounts; the best hand takes the "
            "pot."
        ),
        inline=False,
    )
    embed.set_footer(text="Use /hands any time to review these rankings.")
    return embed
