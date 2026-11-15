"""Tests for exact-match chat actions."""

from __future__ import annotations

import unittest

from pokerbot.cog import parse_action


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
