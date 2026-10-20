from __future__ import annotations

import unittest

from pi_agent_py import Message


class MessageTests(unittest.TestCase):
    def test_rejects_thinking_on_a_non_assistant_message(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "only assistant messages may contain thinking",
        ):
            Message(role="user", content="hello", thinking="hidden")


if __name__ == "__main__":
    unittest.main()
