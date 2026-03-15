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
                patch(
                    "velora.runners._call_anthropic_api",
                    return_value=CmdResult(
                        0,
                        '{"protocol_version":1,"decision":"finalize_success","reason":"done","selected_specialist":{"role":"implementer","runner":"claude"}}',
                        "",
                    ),
                ) as mock_api,
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
        mock_api.assert_called_once_with("PROMPT")

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


class TestLocalBackend(unittest.TestCase):
    def test_normalize_coordinator_backend_accepts_direct_local(self) -> None:
        self.assertEqual(normalize_coordinator_backend(backend="direct-local"), "direct-local")

    def test_normalize_worker_backend_accepts_direct_local(self) -> None:
        self.assertEqual(normalize_worker_backend(backend="direct-local", runner="codex"), "direct-local")
        self.assertEqual(normalize_worker_backend(backend="direct-local", runner="claude"), "direct-local")

    def test_run_worker_routes_direct_local_to_harness(self) -> None:
        """Verify direct-local calls run_local_worker instead of run_local_llm."""
        from unittest.mock import MagicMock
        fake = CmdResult(0, "", "")
        wi = MagicMock(name="WorkItem")
        with patch("velora.runners.run_local_worker", return_value=fake) as mock_harness:
            result = run_worker(
                session_name="test",
                cwd=Path("/tmp/repo"),
                prompt="ignored for local harness",
                runner="codex",
                backend="direct-local",
                work_item=wi,
                work_branch="fix/test",
                exchange_dir=Path("/tmp/exchange"),
                repo_ref="owner/repo",
                run_id="run-1",
                verb="fix",
                objective="fix the bug",
                iteration=1,
            )
        self.assertIs(result, fake)
        mock_harness.assert_called_once_with(
            work_item=wi,
            repo_root=Path("/tmp/repo"),
            work_branch="fix/test",
            exchange_dir=Path("/tmp/exchange"),
            repo_ref="owner/repo",
            run_id="run-1",
            verb="fix",
            objective="fix the bug",
            iteration=1,
        )

    def test_run_worker_direct_local_requires_work_item(self) -> None:
        """Verify direct-local raises ValueError without required params."""
        with self.assertRaises(ValueError) as cm:
            run_worker(
                session_name="test",
                cwd=Path("/tmp/repo"),
                prompt="ignored",
                runner="codex",
                backend="direct-local",
                # work_item and exchange_dir omitted
            )
        self.assertIn("work_item", str(cm.exception))

    def test_run_local_llm_parses_openai_response(self) -> None:
        from velora.acpx import run_local_llm
        import json

        fake_response = json.dumps({
            "choices": [{"message": {"content": "hello world"}}],
        }).encode("utf-8")

        with patch("velora.acpx.urllib.request.urlopen") as mock_urlopen:
            from unittest.mock import MagicMock
            mock_resp = MagicMock()
            mock_resp.read.return_value = fake_response
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = lambda s, *a: None
            mock_urlopen.return_value = mock_resp
            result = run_local_llm("test prompt")

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "hello world")

    def test_run_local_llm_returns_error_on_connection_failure(self) -> None:
        from velora.acpx import run_local_llm
        import urllib.error

        with patch("velora.acpx.urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            result = run_local_llm("test prompt")

        self.assertEqual(result.returncode, 1)
        self.assertIn("connection failed", result.stderr)


if __name__ == "__main__":
    unittest.main()
