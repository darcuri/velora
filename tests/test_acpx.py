import subprocess
import unittest
from unittest.mock import patch

from velora.acpx import parse_codex_footer, resolve_acpx_cmd, run_codex


class TestAcpxDiscovery(unittest.TestCase):
    def test_prefers_env_override(self):
        env = {"VELORA_ACPX_CMD": "/custom/acpx"}
        with patch("velora.acpx.which", return_value="/usr/bin/acpx"):
            self.assertEqual(resolve_acpx_cmd(env=env), "/custom/acpx")

    def test_parse_codex_footer(self):
        out = "something\nBRANCH: velora/abc\nHEAD_SHA: deadbeef\nSUMMARY: done\n"
        parsed = parse_codex_footer(out)
        self.assertEqual(parsed["branch"], "velora/abc")
        self.assertEqual(parsed["head_sha"], "deadbeef")
        self.assertEqual(parsed["summary"], "done")

    def test_parse_codex_footer_when_glued_to_previous_sentence(self):
        out = "ok. BRANCH: velora/abc\nHEAD_SHA: deadbeef\nSUMMARY: done\n"
        parsed = parse_codex_footer(out)
        self.assertEqual(parsed["branch"], "velora/abc")

    def test_raises_if_missing_everywhere(self):
        with patch("velora.acpx.which", return_value=None), patch(
            "velora.acpx._fallback_acpx_exists", return_value=False
        ):
            with self.assertRaises(RuntimeError):
                resolve_acpx_cmd(env={})

    def test_run_codex_ensures_session_first(self):
        # Ensure we try to create/ensure a session before prompting.
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, stdout="OK", stderr="")

        with patch("velora.acpx.get_vault_key", return_value="dummy"), patch(
            "velora.acpx.which", return_value="/usr/bin/acpx"
        ), patch("subprocess.run", side_effect=fake_run):
            res = run_codex(session_name="sess", cwd=__import__("pathlib").Path("/tmp"), prompt="hi")

        self.assertEqual(res.returncode, 0)
        self.assertGreaterEqual(len(calls), 2)
        self.assertIn("sessions", calls[0])
        self.assertIn("ensure", calls[0])


if __name__ == "__main__":
    unittest.main()
