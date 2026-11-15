"""Texas Hold'em table engine.

The betting rules (minimum raises, all-ins, side pots, odd chips) are handled
by PokerKit. This module owns seat rotation, crypto-shuffled dealing, player
stacks, timeout forfeitures, and cashing a game out.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from random import Random

from pokerkit import Automation, NoLimitTexasHoldem
from pokerkit.hands import StandardHighHand

RANKS = "23456789TJQKA"
SUITS = "cdhs"

_AUTOMATIONS = (
    Automation.ANTE_POSTING,
    Automation.BET_COLLECTION,
    Automation.BLIND_OR_STRADDLE_POSTING,
    Automation.CARD_BURNING,
    Automation.HOLE_CARDS_SHOWING_OR_MUCKING,
    Automation.HAND_KILLING,
    Automation.CHIPS_PUSHING,
    Automation.CHIPS_PULLING,
)

STREET_NAMES = {0: "Pre-flop", 3: "Flop", 4: "Turn", 5: "River"}


class IllegalAction(Exception):
    """Raised when a player tries an action that is not currently legal."""


def _card_code(card) -> str:
    """Return the compact code for a PokerKit card (e.g. ``As``)."""
    return f"{card.rank.value}{card.suit.value}"


@dataclass
class Player:
    user_id: int
    name: str
    chips: int
    timeouts: int = 0
    kicked: bool = False

    @property
    def in_game(self) -> bool:
        return not self.kicked and self.chips > 0


@dataclass
class LegalActions:
    call_amount: int
    current_bet: int
    min_raise_to: int | None
    max_raise_to: int | None
    can_raise: bool
    stack: int


@dataclass
class HandResult:
    pot: int
    board: list[str]
    final_chips: dict[int, int]
    net: dict[int, int]
    winners: list[tuple[int, int]]
    hand_labels: dict[int, str]
    revealed: list[int]
    dead_money: int = 0


class Hand:
    """A single Hold'em hand between two or more players."""

    def __init__(
        self,
        order: list[Player],
        small_blind: int,
        big_blind: int,
        rng: Random | None = None,
    ) -> None:
        if len(order) < 2:
            raise ValueError("A hand needs at least two players.")

        self.order = order
        self.small_blind = small_blind
        self.big_blind = big_blind
        self.index = {player.user_id: i for i, player in enumerate(order)}
        self.start_stacks = {player.user_id: player.chips for player in order}
        self.complete = False
        self.folded_users: set[int] = set()

        self.state = NoLimitTexasHoldem.create_state(
            _AUTOMATIONS,
            True,  # ante trimming
            0,  # no antes
            (small_blind, big_blind),
            big_blind,  # minimum bet
            [player.chips for player in order],
            len(order),
        )

        # PokerKit's own shuffle is not cryptographically secure. Rebuild the
        # deck in canonical order, shuffle it with SystemRandom (or the RNG
        # injected by tests), and let PokerKit draw from that one deck, so
        # burns can never duplicate a dealt card.
        rank_order = {rank: i for i, rank in enumerate(RANKS)}
        deck = sorted(
            self.state.deck_cards,
            key=lambda card: (
                rank_order[card.rank.value],
                card.suit.value,
            ),
        )
        (rng or secrets.SystemRandom()).shuffle(deck)
        self.state.deck_cards.clear()
        self.state.deck_cards.extend(deck)

        self._deal_hole_cards()

    # ------------------------------------------------------------------ setup
    def _deal_hole_cards(self) -> None:
        while self.state.can_deal_hole():
            self.state.deal_hole()
        self.hole_cards: dict[int, list[str]] = {
            player.user_id: [_card_code(card) for card in self.state.hole_cards[i]]
            for i, player in enumerate(self.order)
        }

    # ------------------------------------------------------------------ state
    @property
    def is_over(self) -> bool:
        return not self.state.status

    @property
    def board(self) -> list[str]:
        return [_card_code(card) for card in self.state.get_board_cards(0)]

    @property
    def street(self) -> str:
        return STREET_NAMES.get(len(self.board), "Showdown")

    @property
    def pot(self) -> int:
        return self.state.total_pot_amount

    @property
    def actor_index(self) -> int | None:
        return self.state.actor_index

    @property
    def actor_id(self) -> int | None:
        index = self.state.actor_index
        return None if index is None else self.order[index].user_id

    def player_by_id(self, user_id: int) -> Player:
        return self.order[self.index[user_id]]

    def legal_actions(self) -> LegalActions:
        if self.actor_index is None:
            raise IllegalAction("Nobody can act right now.")
        state = self.state
        return LegalActions(
            call_amount=state.checking_or_calling_amount,
            current_bet=max(state.bets) if state.bets else 0,
            min_raise_to=state.min_completion_betting_or_raising_to_amount,
            max_raise_to=state.max_completion_betting_or_raising_to_amount,
            can_raise=bool(state.can_complete_bet_or_raise_to()),
            stack=state.stacks[self.actor_index],
        )

    # ---------------------------------------------------------------- actions
    def validate_action(
        self, user_id: int, action: str, amount: int | None = None
    ) -> None:
        """Raise IllegalAction if the move is not currently legal."""
        if self.is_over:
            raise IllegalAction("This hand is already over.")
        if self.actor_id != user_id:
            raise IllegalAction("It is not that player's turn.")

        if action == "fold":
            if not self.state.can_fold():
                raise IllegalAction("You cannot fold right now.")
            return
        if action in {"check", "call"}:
            if not self.state.can_check_or_call():
                raise IllegalAction("You cannot check or call right now.")
            return
        if action == "raise":
            legal = self.legal_actions()
            if not legal.can_raise or legal.min_raise_to is None:
                raise IllegalAction("Raising is not available right now.")
            if amount is None:
                raise IllegalAction("A raise amount is required.")
            if legal.max_raise_to is None or not (
                legal.min_raise_to <= amount <= legal.max_raise_to
            ):
                raise IllegalAction(
                    f"Raise must be between {legal.min_raise_to:,} and "
                    f"{legal.max_raise_to or legal.min_raise_to:,} chips."
                )
            if not self.state.can_complete_bet_or_raise_to(amount):
                raise IllegalAction("That raise is not legal.")
            return
        if action == "allin":
            if not (
                self.state.can_check_or_call()
                or self.state.can_complete_bet_or_raise_to()
            ):
                raise IllegalAction("You cannot move all-in right now.")
            return
        raise IllegalAction("Unknown action.")

    def act(self, user_id: int, action: str, amount: int | None = None) -> str:
        """Apply an action and return a short human-readable description."""
        self.validate_action(user_id, action, amount)

        if action == "fold":
            self.state.fold()
            self.folded_users.add(user_id)
            return "folds"

        if action in {"check", "call"}:
            due = self.state.checking_or_calling_amount
            self.state.check_or_call()
            return f"calls {due:,}" if due else "checks"

        if action == "raise":
            assert amount is not None
            self.state.complete_bet_or_raise_to(amount)
            return f"raises to {amount:,}"

        if action == "allin":
            legal = self.legal_actions()
            if legal.can_raise and legal.max_raise_to is not None:
                self.state.complete_bet_or_raise_to(legal.max_raise_to)
                return f"is all-in for {legal.max_raise_to:,}"
            due = legal.call_amount
            self.state.check_or_call()
            return f"calls all-in for {due:,}" if due else "checks"

        raise IllegalAction("Unknown action.")

    def deal_pending_board(self) -> list[str] | None:
        """Deal any cards the state is waiting on. Returns newly dealt cards."""
        if not self.state.can_deal_board():
            return None
        dealt: list[str] = []
        while self.state.can_deal_board():
            operation = self.state.deal_board()
            dealt.extend(_card_code(card) for card in operation.cards)
        return dealt

    # ----------------------------------------------------------------- finish
    def resolve(self) -> HandResult:
        if not self.is_over:
            raise IllegalAction("The hand is still in progress.")

        final_chips = {
            player.user_id: (
                0 if player.kicked else self.state.stacks[i]
            )
            for i, player in enumerate(self.order)
        }
        net = {
            user_id: final_chips[user_id] - self.start_stacks[user_id]
            for user_id in final_chips
        }
        winners = sorted(
            ((user_id, amount) for user_id, amount in net.items() if amount > 0),
            key=lambda item: item[1],
            reverse=True,
        )
        pot = self.state.total_pushed_amount

        # PokerKit auto-mucks losing hands at showdown, so use our own fold
        # tracking to know who actually reached showdown.
        revealed = [
            player.user_id
            for player in self.order
            if player.user_id not in self.folded_users
        ]
        board = self.board
        labels: dict[int, str] = {}
        if len(board) == 5:
            for user_id in revealed:
                try:
                    hand = StandardHighHand.from_game(
                        "".join(self.hole_cards[user_id]), "".join(board)
                    )
                    labels[user_id] = hand.entry.label.value
                except (ValueError, KeyError):
                    pass

        return HandResult(
            pot=pot,
            board=board,
            final_chips=final_chips,
            net=net,
            winners=winners,
            hand_labels=labels,
            revealed=revealed,
        )


@dataclass
class Table:
    """Cash-game table that keeps stacks between hands."""

    players: list[Player]
    small_blind: int
    big_blind: int
    rng: Random | None = None
    hand: Hand | None = None
    bonus_pot: int = 0
    hand_number: int = 0
    _button_seat: int | None = None

    def player(self, user_id: int) -> Player | None:
        return next((p for p in self.players if p.user_id == user_id), None)

    def active_players(self) -> list[Player]:
        return [p for p in self.players if p.in_game]

    @property
    def button_player(self) -> Player | None:
        if self._button_seat is None:
            return None
        return self.players[self._button_seat]

    def start_hand(self) -> Hand:
        active = self.active_players()
        if len(active) < 2:
            raise ValueError("At least two funded players are needed.")

        # The button moves one active seat clockwise each hand. The first
        # hand uses the first funded player as the button.
        if self._button_seat is None:
            button = active[0]
        else:
            button = None
            for offset in range(1, len(self.players) + 1):
                candidate = self.players[(self._button_seat + offset) % len(self.players)]
                if candidate in active:
                    button = candidate
                    break
            if button is None:  # pragma: no cover - active list was checked above
                button = active[0]

        self._button_seat = self.players.index(button)
        button_pos = active.index(button)

        # Rotate so the button is last. PokerKit then puts the small blind at
        # index 0 and big blind at index 1 (heads-up: BB at 0, SB/button at 1).
        order = active[button_pos + 1 :] + active[: button_pos + 1]

        self.hand_number += 1
        self.hand = Hand(order, self.small_blind, self.big_blind, self.rng)
        return self.hand

    def finish_hand(self) -> HandResult:
        if self.hand is None or not self.hand.is_over:
            raise IllegalAction("There is no finished hand to settle.")
        if self.hand.complete:
            raise IllegalAction("This hand has already been settled.")

        result = self.hand.resolve()
        self.hand.complete = True

        # A removed player's remaining stack is dead money. It is paid to the
        # winner(s) of the hand that was running when they were removed.
        bonus_awards: dict[int, int] = {}
        if self.bonus_pot and result.winners:
            dead = self.bonus_pot
            share, remainder = divmod(dead, len(result.winners))
            result.winners = [
                (user_id, amount + share + (remainder if i == 0 else 0))
                for i, (user_id, amount) in enumerate(result.winners)
            ]
            for i, (user_id, amount) in enumerate(result.winners):
                bonus_awards[user_id] = amount - result.net[user_id]
            result.dead_money = dead
        self.bonus_pot = 0

        for player in self.players:
            if player.user_id not in result.final_chips:
                continue
            player.chips = 0 if player.kicked else result.final_chips[player.user_id]
            player.chips += bonus_awards.get(player.user_id, 0)

        return result

    def forfeit(self, user_id: int) -> int:
        """Remove a player and move their remaining stack into the dead pot."""
        player = self.player(user_id)
        if player is None:
            return 0

        forfeited = 0
        if self.hand is not None and not self.hand.complete:
            index = self.hand.index.get(user_id)
            if index is not None:
                forfeited = self.hand.state.stacks[index]
                self.bonus_pot += forfeited

        player.kicked = True
        player.chips = 0
        return forfeited

    def abort_refunds(self) -> dict[int, int]:
        """Cash everybody out. A live hand is voided and its bets returned."""
        dead_money = self.bonus_pot
        if self.hand is not None and not self.hand.complete:
            eligible = [p for p in self.hand.order if not p.kicked]
            refunds = {
                p.user_id: self.hand.start_stacks[p.user_id] for p in eligible
            }
            # Voiding the hand also voids the pot. A kicked player's stack
            # capture only covered their uncommitted chips, so add the chips
            # they already put in the pot to the pool being shared out.
            for player in self.hand.order:
                if not player.kicked:
                    continue
                index = self.hand.index[player.user_id]
                dead_money += (
                    self.hand.start_stacks[player.user_id]
                    - self.hand.state.stacks[index]
                )
        else:
            eligible = [p for p in self.players if not p.kicked and p.chips > 0]
            refunds = {p.user_id: p.chips for p in eligible}

        # Any dead money is shared by the players who are still in the game.
        if dead_money and refunds:
            share, remainder = divmod(dead_money, len(refunds))
            for i, user_id in enumerate(refunds):
                refunds[user_id] += share + (remainder if i == 0 else 0)
        self.bonus_pot = 0

        return refunds
