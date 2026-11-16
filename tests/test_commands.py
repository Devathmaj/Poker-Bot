"""Tests for slash-command helpers and exact-match chat actions."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from pokerbot.cog import format_countdown, parse_action


class CountdownFormattingTests(unittest.TestCase):
    def test_shows_hours_and_minutes(self) -> None:
        now = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
        self.assertEqual(
            format_countdown(now + timedelta(hours=23, minutes=45), now),
            "in 23h 45m",
        )
        self.assertEqual(
            format_countdown(now + timedelta(hours=24), now), "in 24h"
        )
        self.assertEqual(
            format_countdown(now + timedelta(minutes=59), now), "in 59m"
        )

    def test_rounds_up_partial_minutes(self) -> None:
        now = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
        self.assertEqual(
            format_countdown(now + timedelta(seconds=1), now), "in 1m"
        )
        self.assertEqual(
            format_countdown(
                now + timedelta(hours=23, minutes=59, seconds=30), now
            ),
            "in 24h",
        )

    def test_finished_cooldowns_are_ready(self) -> None:
        now = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
        self.assertEqual(format_countdown(now, now), "ready now")
        self.assertEqual(
            format_countdown(now - timedelta(minutes=1), now), "ready now"
        )


class ActionParsingTests(unittest.TestCase):
    def test_simple_actions(self) -> None:
        self.assertEqual(parse_action("fold"), ("fold", None))
        self.assertEqual(parse_action("check"), ("check", None))
        self.assertEqual(parse_action(" CAll "), ("call", None))
        self.assertEqual(parse_action("all in"), ("allin", None))
        self.assertEqual(parse_action("all-in"), ("allin", None))
        self.assertEqual(parse_action("allin"), ("allin", None))

    def test_raises(self) -> None:
        self.assertEqual(parse_action("raise 100"), ("raise", 100))
        self.assertEqual(parse_action("raise to 1,000"), ("raise", 1000))
        self.assertEqual(parse_action("bet 250"), ("raise", 250))

    def test_only_whole_messages_match(self) -> None:
        for content in (
            "please call",
            "call now",
            "raise 100 please",
            "check call",
            "raise",
            "raise abc",
            "raise 0",
            "i fold",
        ):
            with self.subTest(content=content):
                self.assertIsNone(parse_action(content))


if __name__ == "__main__":
    unittest.main(verbosity=2)
