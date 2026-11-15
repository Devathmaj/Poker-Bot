"""Focused tests for the Hold'em table engine (no Discord involved)."""

from __future__ import annotations

import random
import unittest

from pokerbot.engine import IllegalAction, Player, Table


def check_call_round(hand):
    """Have every actor check or call until the hand is over."""
    guard = 0
    while not hand.is_over:
        guard += 1
        if guard > 200:
            raise AssertionError("hand did not finish")
        if hand.state.can_deal_board():
            hand.deal_pending_board()
            continue
        hand.act(hand.actor_id, "call")


class DeckTests(unittest.TestCase):
    def test_dealing_is_deterministic_with_injected_rng(self):
        def deal(seed):
            players = [Player(1, "A", 1000), Player(2, "B", 1000)]
            return Table(players, 5, 10, rng=random.Random(seed)).start_hand().hole_cards

        self.assertEqual(deal(7), deal(7))
        self.assertNotEqual(deal(7), deal(8))


class HeadsUpTests(unittest.TestCase):
    def test_blinds_and_action_order(self):
        players = [Player(1, "A", 1000), Player(2, "B", 1000)]
        table = Table(players, 5, 10, rng=random.Random(2))
        hand = table.start_hand()

        # Player 1 is the button, so they post SB and act first pre-flop.
        self.assertEqual(hand.actor_id, 1)
        legal = hand.legal_actions()
        self.assertEqual(legal.call_amount, 5)
        self.assertEqual(legal.min_raise_to, 20)
        self.assertEqual(legal.current_bet, 10)

        self.assertEqual(hand.act(1, "call"), "calls 5")
        self.assertEqual(hand.actor_id, 2)
        self.assertEqual(hand.act(2, "check"), "checks")

        board = hand.deal_pending_board()
        self.assertEqual(len(board), 3)
        self.assertTrue(all(len(card) == 2 for card in board))
        self.assertTrue(all(len(card) == 2 for card in hand.hole_cards[1]))
        self.assertEqual(hand.street, "Flop")
        # After the flop the non-button player acts first.
        self.assertEqual(hand.actor_id, 2)

    def test_invalid_raise_is_rejected(self):
        players = [Player(1, "A", 1000), Player(2, "B", 1000)]
        hand = Table(players, 5, 10, rng=random.Random(3)).start_hand()

        with self.assertRaises(IllegalAction):
            hand.act(1, "raise", 15)
        self.assertEqual(hand.act(1, "raise", 20), "raises to 20")
        self.assertEqual(hand.legal_actions().call_amount, 10)
        with self.assertRaises(IllegalAction):
            hand.act(1, "fold")  # out of turn

    def test_showdown_reveals_all_non_folded_players(self):
        players = [Player(1, "A", 1000), Player(2, "B", 1000)]
        table = Table(players, 5, 10, rng=random.Random(11))
        hand = table.start_hand()
        check_call_round(hand)
        result = table.finish_hand()
        self.assertEqual(sorted(result.revealed), [1, 2])
        self.assertEqual(len(result.hand_labels), 2)

    def test_abort_refunds_full_stacks(self):
        players = [Player(1, "A", 1000), Player(2, "B", 1000)]
        table = Table(players, 5, 10, rng=random.Random(4))
        hand = table.start_hand()
        hand.act(1, "raise", 100)
        refunds = table.abort_refunds()
        self.assertEqual(refunds, {1: 1000, 2: 1000})
        self.assertEqual(table.bonus_pot, 0)


class SidePotTests(unittest.TestCase):
    def test_all_in_side_pot_conserves_chips(self):
        players = [
            Player(1, "A", 1000),
            Player(2, "B", 1000),
            Player(3, "C", 300),
        ]
        table = Table(players, 5, 10, rng=random.Random(5))
        hand = table.start_hand()

        self.assertEqual(hand.act(1, "allin"), "is all-in for 1,000")
        self.assertEqual(hand.act(2, "call"), "calls 995")
        self.assertEqual(hand.act(3, "call"), "calls 290")

        while hand.state.can_deal_board():
            hand.deal_pending_board()
        self.assertTrue(hand.is_over)

        result = table.finish_hand()
        self.assertEqual(result.pot, 2300)
        self.assertEqual(sum(p.chips for p in players), 2300)
        self.assertTrue(result.winners)
        self.assertTrue(
            {uid for uid, _ in result.winners}.issubset(set(result.revealed))
        )
        self.assertEqual(table.bonus_pot, 0)
        self.assertEqual(len(result.board), 5)


class TimeoutTests(unittest.TestCase):
    def test_fourth_timeout_forfeits_stack_and_removes_player(self):
        players = [
            Player(1, "A", 1000),
            Player(2, "B", 1000),
            Player(3, "C", 1000),
        ]
        table = Table(players, 5, 10, rng=random.Random(6))
        hand = table.start_hand()

        # The button acts first pre-flop; remove them as a timed-out player
        # would be removed, then fold their hand.
        self.assertEqual(hand.actor_id, 1)
        self.assertEqual(table.forfeit(1), 1000)
        self.assertEqual(table.bonus_pot, 1000)
        hand.act(1, "fold")

        check_call_round(hand)
        result = table.finish_hand()

        self.assertEqual(players[0].chips, 0)
        self.assertEqual(result.dead_money, 1000)
        self.assertEqual(sum(p.chips for p in players), 3000)
        self.assertEqual(table.bonus_pot, 0)
        self.assertEqual(len(table.active_players()), 2)

    def test_aborting_after_a_kick_conserves_chips(self):
        players = [
            Player(1, "A", 1000),
            Player(2, "B", 1000),
            Player(3, "C", 1000),
        ]
        table = Table(players, 5, 10, rng=random.Random(12))
        table.start_hand()
        table.forfeit(1)
        refunds = table.abort_refunds()
        self.assertNotIn(1, refunds)
        self.assertEqual(sum(refunds.values()), 3000)
        self.assertEqual(table.bonus_pot, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
