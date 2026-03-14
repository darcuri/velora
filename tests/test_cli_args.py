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

    def test_resume_args_parsing(self):
        parser = build_parser()
        args = parser.parse_args(["resume", "task123", "--json"])
        self.assertEqual(args.cmd, "resume")
        self.assertEqual(args.task_id, "task123")
        self.assertTrue(args.json)

    def test_coord_request_args_parsing(self):
        parser = build_parser()
        args = parser.parse_args(
            [
                "coord",
                "request",
                "octocat/velora",
                "fix",
                "--unsafe-task",
                "do thing",
                "--json",
            ]
        )
        self.assertEqual(args.cmd, "coord")
        self.assertEqual(args.coord_cmd, "request")
        self.assertEqual(args.repo, "octocat/velora")
        self.assertEqual(args.verb, "fix")
        self.assertEqual(args.unsafe_task, "do thing")
        self.assertIsNone(args.spec)
        self.assertTrue(args.json)

    def test_audit_inspect_args_parsing(self):
        parser = build_parser()
        args = parser.parse_args(["audit", "inspect", "--run", "run-123", "--json"])
        self.assertEqual(args.cmd, "audit")
        self.assertEqual(args.audit_cmd, "inspect")
        self.assertEqual(args.run_id, "run-123")
        self.assertTrue(args.json)


if __name__ == "__main__":
    unittest.main()
