import subprocess
import unittest
from unittest.mock import patch

from velora.acpx import (
    _parse_acpx_json_prompt_output,
    parse_codex_footer,
    resolve_acpx_cmd,
    review_has_blocker,
    run_codex,
    run_gemini_review,
)


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

    def test_run_gemini_review_uses_rest_api(self):
        class DummyResp:
            def __init__(self, payload: str):
                self._payload = payload.encode("utf-8")

            def read(self):
                return self._payload

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        fake_payload = '{"candidates":[{"content":{"parts":[{"text":"- NIT: looks "},{"text":"fine. This is a sufficiently long review bullet to pass minimum-length validation. Additional context to pad length. Additional context to pad length. Additional context to pad length. "}]}}]}'

        with patch("velora.acpx.get_vault_key", return_value="dummy"), patch(
            "velora.acpx.urllib.request.urlopen", return_value=DummyResp(fake_payload)
        ):
            res = run_gemini_review("diff")

        self.assertEqual(res.returncode, 0)
        self.assertIn("NIT", res.stdout)
        self.assertIn("looks fine", res.stdout)

    def test_run_gemini_review_accepts_ok_single_line(self):
        class DummyResp:
            def __init__(self, payload: str):
                self._payload = payload.encode("utf-8")

            def read(self):
                return self._payload

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        fake_payload = '{"candidates":[{"content":{"parts":[{"text":"OK: Looks good."}]}}]}'

        with patch("velora.acpx.get_vault_key", return_value="dummy"), patch(
            "velora.acpx.urllib.request.urlopen", return_value=DummyResp(fake_payload)
        ):
            res = run_gemini_review("diff")

        self.assertEqual(res.returncode, 0)
        self.assertIn("OK:", res.stdout)

    def test_review_has_blocker_parses_lines(self):
        self.assertTrue(review_has_blocker("BLOCKER: This will crash."))
        self.assertTrue(review_has_blocker("- BLOCKER: This will crash."))
        self.assertFalse(review_has_blocker("NIT: Style."))
        self.assertFalse(review_has_blocker("OK: Looks good."))

    def test_parse_acpx_json_prompt_output_extracts_text_and_usage(self):
        raw = "\n".join(
            [
                '{"jsonrpc":"2.0","id":2,"result":{"models":{"currentModelId":"gpt-5.3-codex/medium"}}}',
                '{"jsonrpc":"2.0","method":"session/update","params":{"update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"hello"}}}}',
                '{"jsonrpc":"2.0","method":"session/update","params":{"update":{"sessionUpdate":"usage_update","used":123,"size":1000}}}',
            ]
        )
        text, usage = _parse_acpx_json_prompt_output(raw)
        self.assertEqual(text, "hello")
        self.assertEqual(usage.used, 123)
        self.assertEqual(usage.size, 1000)
        self.assertEqual(usage.model_id, "gpt-5.3-codex/medium")


if __name__ == "__main__":
    unittest.main()
