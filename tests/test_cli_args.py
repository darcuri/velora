import unittest

from velora.cli import build_parser


class TestCliArgs(unittest.TestCase):
    def test_run_args_parsing_unsafe_task(self):
        parser = build_parser()
        args = parser.parse_args(["run", "octocat/velora", "feature", "--unsafe-task", "add cool thing", "--json"])
        self.assertEqual(args.cmd, "run")
        self.assertEqual(args.repo, "octocat/velora")
        self.assertEqual(args.verb, "feature")
        self.assertEqual(args.unsafe_task, "add cool thing")
        self.assertIsNone(args.spec)
        self.assertTrue(args.json)


if __name__ == "__main__":
    unittest.main()
