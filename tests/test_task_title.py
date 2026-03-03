import unittest

from velora.run import _task_title


class TestTaskTitle(unittest.TestCase):
    def test_title_override_used(self):
        title = _task_title("feature", "do thing", title_override="Short title")
        self.assertEqual(title, "[feature] Short title")

    def test_long_task_is_compacted_and_truncated(self):
        task = (
            "IMPORTANT: For this run, use ONLY codex. "
            "Mode A complex dogfood: Improve evaluation evidence + progress signals. "
            "When CI polling ends in failure, fetch GitHub check-runs for the PR head SHA and populate lots of stuff. "
            "Keep stdlib-only."
        )
        title = _task_title("feature", task)
        self.assertTrue(title.startswith("[feature] "))
        self.assertLessEqual(len(title), 96)
        self.assertNotIn("IMPORTANT", title)
        # Ensure we didn't just dump the entire prompt in the title.
        self.assertNotIn("fetch GitHub check-runs", title)


if __name__ == "__main__":
    unittest.main()
