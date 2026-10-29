from __future__ import annotations

import unittest

from pi_agent_py import Message, ProviderState


class MessageTests(unittest.TestCase):
    def test_rejects_thinking_on_a_non_assistant_message(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "only assistant messages may contain thinking",
        ):
            Message(role="user", content="hello", thinking="hidden")

    def test_rejects_provider_state_on_a_non_assistant_message(self) -> None:
        state = ProviderState("timicc-responses", {"type": "reasoning"})

        with self.assertRaisesRegex(
            ValueError,
            "only assistant messages may contain provider state",
        ):
            Message(role="user", content="hello", provider_state=(state,))


if __name__ == "__main__":
    unittest.main()
