import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from velora.acpx import CmdResult
from velora.runners import normalize_coordinator_backend, normalize_worker_backend, run_coordinator, run_worker


class TestRunners(unittest.TestCase):
    def test_normalize_coordinator_backend_defaults_from_runner(self) -> None:
        self.assertEqual(normalize_coordinator_backend(runner="claude"), "acp-claude")
        self.assertEqual(normalize_coordinator_backend(runner="codex"), "acp-codex")

    def test_normalize_coordinator_backend_accepts_explicit_backend(self) -> None:
        self.assertEqual(normalize_coordinator_backend(backend="acp-claude", runner="codex"), "acp-claude")
        self.assertEqual(normalize_coordinator_backend(backend="direct-claude", runner="claude"), "direct-claude")

    def test_normalize_coordinator_backend_rejects_unknown_values(self) -> None:
        with self.assertRaises(ValueError):
            normalize_coordinator_backend(runner="gemini")

        with self.assertRaises(ValueError):
            normalize_coordinator_backend(backend="direct-codex")

    def test_normalize_worker_backend_defaults_from_runner(self) -> None:
        self.assertEqual(normalize_worker_backend(runner="claude"), "acp-claude")
        self.assertEqual(normalize_worker_backend(runner="codex"), "acp-codex")

    def test_normalize_worker_backend_accepts_explicit_backend(self) -> None:
        self.assertEqual(normalize_worker_backend(backend="acp-claude", runner="claude"), "acp-claude")
        self.assertEqual(normalize_worker_backend(backend="direct-claude", runner="claude"), "direct-claude")
        self.assertEqual(normalize_worker_backend(backend="direct-codex", runner="codex"), "direct-codex")

    def test_normalize_worker_backend_rejects_unknown_values(self) -> None:
        with self.assertRaises(ValueError):
            normalize_worker_backend(runner="gemini")

        with self.assertRaises(ValueError):
            normalize_worker_backend(backend="direct-gemini", runner="codex")

    def test_normalize_worker_backend_rejects_backend_runner_mismatch(self) -> None:
        with self.assertRaises(ValueError):
            normalize_worker_backend(backend="direct-claude", runner="codex")

        with self.assertRaises(ValueError):
            normalize_worker_backend(backend="direct-codex", runner="claude")

    def test_run_coordinator_routes_to_acp_claude(self) -> None:
        fake = SimpleNamespace(response={"ok": True}, cmd=CmdResult(0, "", ""))
        with patch("velora.runners.run_coordinator_v1_with_cmd", return_value=fake) as mock_run:
            result = run_coordinator(
                session_name="coord-session",
                cwd=Path("/tmp/repo"),
                request={"x": 1},
                runner="claude",
            )

        self.assertIs(result, fake)
        mock_run.assert_called_once_with(
            session_name="coord-session",
            cwd=Path("/tmp/repo"),
            request={"x": 1},
            runner="claude",
        )

    def test_run_coordinator_routes_to_acp_codex(self) -> None:
        fake = SimpleNamespace(response={"ok": True}, cmd=CmdResult(0, "", ""))
        with patch("velora.runners.run_coordinator_v1_with_cmd", return_value=fake) as mock_run:
            result = run_coordinator(
                session_name="coord-session",
                cwd=Path("/tmp/repo"),
                request={"x": 1},
                runner="codex",
            )

        self.assertIs(result, fake)
        mock_run.assert_called_once_with(
            session_name="coord-session",
            cwd=Path("/tmp/repo"),
            request={"x": 1},
            runner="codex",
        )

    def test_run_coordinator_routes_to_direct_claude_and_reads_replay_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp) / "repo"
            memory_dir = repo_path / ".velora" / "exchange" / "runs" / "task123"
            memory_dir.mkdir(parents=True)
            (memory_dir / "coordinator-memory.md").write_text("# Coordinator Replay\n\nhello", encoding="utf-8")
            (memory_dir / "coordinator-brief.json").write_text(
                '{"run_id":"task123","status":{"state":"running"}}',
                encoding="utf-8",
            )

            request = {"run_id": "task123", "policy": {}}
            with (
                patch("velora.runners.render_coordinator_prompt_v1", return_value="PROMPT") as mock_render,
                patch("velora.runners._ensure_anthropic_auth") as mock_auth,
                patch(
                    "velora.runners.run_cmd",
                    return_value=CmdResult(
                        0,
                        '{"protocol_version":1,"decision":"finalize_success","reason":"done","selected_specialist":{"role":"implementer","runner":"claude"}}',
                        "",
                    ),
                ) as mock_run_cmd,
            ):
                result = run_coordinator(
                    session_name="coord-session",
                    cwd=repo_path,
                    request=request,
                    backend="direct-claude",
                )

        self.assertEqual(result.response.decision, "finalize_success")
        mock_render.assert_called_once_with(
            request,
            replay_memory="# Coordinator Replay\n\nhello",
            brief={"run_id": "task123", "status": {"state": "running"}},
        )
        mock_auth.assert_called_once()
        env = mock_run_cmd.call_args.kwargs["env"]
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")
        mock_run_cmd.assert_called_once_with(
            ["claude", "--print", "--permission-mode", "bypassPermissions", "-p", "PROMPT"],
            cwd=repo_path,
            env=env,
        )

    def test_run_worker_routes_to_acp_claude(self) -> None:
        fake = CmdResult(0, "ok", "")
        with patch("velora.runners.run_claude", return_value=fake) as mock_run:
            result = run_worker(
                session_name="worker-session",
                cwd=Path("/tmp/repo"),
                prompt="PROMPT",
                runner="claude",
            )

        self.assertIs(result, fake)
        mock_run.assert_called_once_with(session_name="worker-session", cwd=Path("/tmp/repo"), prompt="PROMPT")

    def test_run_worker_routes_to_acp_codex(self) -> None:
        fake = CmdResult(0, "ok", "")
        with patch("velora.runners.run_codex", return_value=fake) as mock_run:
            result = run_worker(
                session_name="worker-session",
                cwd=Path("/tmp/repo"),
                prompt="PROMPT",
                runner="codex",
            )

        self.assertIs(result, fake)
        mock_run.assert_called_once_with(session_name="worker-session", cwd=Path("/tmp/repo"), prompt="PROMPT")

    def test_run_worker_routes_to_direct_claude_and_injects_auth(self) -> None:
        fake = CmdResult(0, "ok", "")
        with (
            patch("velora.runners._ensure_anthropic_auth") as mock_auth,
            patch("velora.runners.run_cmd", return_value=fake) as mock_run_cmd,
        ):
            result = run_worker(
                session_name="worker-session",
                cwd=Path("/tmp/repo"),
                prompt="PROMPT",
                runner="claude",
                backend="direct-claude",
            )

        self.assertIs(result, fake)
        mock_auth.assert_called_once()
        env = mock_run_cmd.call_args.kwargs["env"]
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")
        mock_run_cmd.assert_called_once_with(
            ["claude", "--print", "--permission-mode", "bypassPermissions", "-p", "PROMPT"],
            cwd=Path("/tmp/repo"),
            env=env,
        )

    def test_run_worker_routes_to_direct_codex_and_injects_auth(self) -> None:
        fake = CmdResult(0, "ok", "")
        with (
            patch("velora.runners.get_vault_key", return_value="sk-test") as mock_key,
            patch("velora.runners.run_cmd", return_value=fake) as mock_run_cmd,
        ):
            result = run_worker(
                session_name="worker-session",
                cwd=Path("/tmp/repo"),
                prompt="PROMPT",
                runner="codex",
                backend="direct-codex",
            )

        self.assertIs(result, fake)
        mock_key.assert_called_once()
        env = mock_run_cmd.call_args.kwargs["env"]
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertEqual(env["OPENAI_API_KEY"], "sk-test")
        mock_run_cmd.assert_called_once_with(
            ["codex", "exec", "--full-auto", "-C", "/tmp/repo", "-"],
            cwd=Path("/tmp/repo"),
            input_text="PROMPT",
            env=env,
        )


if __name__ == "__main__":
    unittest.main()
