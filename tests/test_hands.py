"""Tests for the shared hand-ranking reference."""

from __future__ import annotations

import unittest
from io import BytesIO

from PIL import Image

from pokerbot.hands import format_cards, hand_rankings_embed
from pokerbot.visuals import HELP_RANKINGS, render_help_chart


class HandRankingTests(unittest.TestCase):
    def test_cards_render_ranks_and_suits(self) -> None:
        self.assertEqual(
            format_cards(("As", "Th", "2d", "9c")), "A♠ 10♥ 2♦ 9♣"
        )

    def test_every_hand_is_listed_in_order(self) -> None:
        description = hand_rankings_embed().description or ""
        positions = [
            description.index(f"**{index}. {row.name}**")
            for index, row in enumerate(HELP_RANKINGS, start=1)
        ]
        self.assertEqual(positions, sorted(positions))

    def test_examples_match_each_hand(self) -> None:
        description = hand_rankings_embed().description or ""
        for row in HELP_RANKINGS:
            with self.subTest(hand=row.name):
                self.assertIn(format_cards(row.cards), description)

    def test_strength_clauses_match_standard_ranking(self) -> None:
        description = hand_rankings_embed().description or ""
        self.assertIn(
            "Royal flush** · the highest straight flush", description
        )
        self.assertIn(
            "Straight flush** · beats four of a kind", description
        )
        self.assertIn(
            "Four of a kind** · beats a full house", description
        )
        self.assertIn("Full house** · beats a flush", description)
        self.assertIn("Flush** · beats a straight", description)
        self.assertIn("Straight** · beats three of a kind", description)
        self.assertIn("Three of a kind** · beats two pair", description)
        self.assertIn("Two pair** · beats one pair", description)
        self.assertIn("One pair** · beats high card", description)
        self.assertIn("High card** · the weakest hand", description)

    def test_notes_are_accurate(self) -> None:
        notes = hand_rankings_embed().fields[0].value
        self.assertIn("pot is split", notes)
        self.assertIn("A-2-3-4-5 is the lowest straight", notes)
        self.assertIn("A-K-Q-J-10 is the highest", notes)
        self.assertIn("Suits are never stronger", notes)
        self.assertIn("both, one, or none of your hole cards", notes)
        self.assertIn("best hand takes the pot", notes)

    def test_enlarged_chart_doubles_the_dimensions(self) -> None:
        base_bytes = render_help_chart()
        large_bytes = render_help_chart(2)
        with Image.open(BytesIO(base_bytes)) as base, Image.open(
            BytesIO(large_bytes)
        ) as large:
            self.assertEqual(
                large.size, (base.width * 2, base.height * 2)
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
