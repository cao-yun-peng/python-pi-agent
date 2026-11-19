import re
import unittest

from pi_agent_py.api import TruncateToolResults, ToolOutput


class TruncationTests(unittest.IsolatedAsyncioTestCase):
    async def test_identity_modes_and_exact_budget(self):
        original = ToolOutput("short", {"unchanged": [1]}, is_error=True)
        self.assertIs(await TruncateToolResults(64).transform(None, original), original)
        text = "头" * 100 + "尾" * 100
        for mode in ("head", "tail", "head_tail"):
            output = ToolOutput(text, original.structured_content, is_error=True)
            final = await TruncateToolResults(64, mode).transform(None, output)
            marker = "\n[... truncated 172 characters ...]\n"
            budget = 64 - len(marker)
            head = budget if mode == "head" else 0 if mode == "tail" else budget // 2
            tail = budget - head
            self.assertEqual(
                final.content, text[:head] + marker + (text[-tail:] if tail else "")
            )
            self.assertIs(final.structured_content, output.structured_content)
            self.assertTrue(final.is_error)

    async def test_digit_boundaries_omitted_count_and_lengths(self):
        for size in (65, 100, 133, 999, 1000, 10000):
            for limit in (64, 65, 99, 100):
                text = "x" * size
                final = await TruncateToolResults(limit).transform(
                    None, ToolOutput(text)
                )
                self.assertLessEqual(len(final.content), limit)
                match = re.search(
                    r"\n\[... truncated (\d+) characters ...\]\n", final.content
                )
                if match:
                    self.assertEqual(
                        int(match[1]), size - len(final.content) + len(match[0])
                    )

    def test_invalid_configuration(self):
        for limit in (-1, 63, True, 64.5):
            with self.assertRaises(ValueError):
                TruncateToolResults(limit)
        with self.assertRaises(ValueError):
            TruncateToolResults(64, "middle")
