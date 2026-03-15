import subprocess
import unittest
from types import SimpleNamespace
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
        with patch("velora.acpx.get_config", return_value=SimpleNamespace(acpx_cmd=None, acpx_fallback=None)), patch(
            "velora.acpx.which", return_value=None
        ), patch("velora.acpx._fallback_acpx_exists", return_value=False):
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

    def test_run_gemini_review_returns_raw_text_for_malformed_format(self):
        class DummyResp:
            def __init__(self, payload: str):
                self._payload = payload.encode("utf-8")

            def read(self):
                return self._payload

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        fake_payload = '{"candidates":[{"content":{"parts":[{"text":"Looks good overall but missing required prefix"}]}}]}'

        with patch("velora.acpx.get_vault_key", return_value="dummy"), patch(
            "velora.acpx.urllib.request.urlopen", return_value=DummyResp(fake_payload)
        ):
            res = run_gemini_review("diff")

        self.assertEqual(res.returncode, 0)
        self.assertIn("Looks good overall", res.stdout)
        self.assertIn("malformed format", res.stderr)

    def test_run_gemini_review_prompt_is_strictly_non_explanatory(self):
        seen: dict[str, str] = {}

        def fake_generate_content(*, api_key, model, prompt, max_output_tokens):
            seen["prompt"] = prompt
            return "OK: Looks good."

        with patch("velora.acpx.get_vault_key", return_value="dummy"), patch(
            "velora.acpx._gemini_generate_content", side_effect=fake_generate_content
        ):
            res = run_gemini_review("diff with orbital math")

        self.assertEqual(res.returncode, 0)
        self.assertIn("strict code-review classifier, not a tutor or explainer", seen["prompt"])
        self.assertIn("Do not explain math, formulas, or algorithms from the diff.", seen["prompt"])
        self.assertIn("If you are tempted to be more helpful than that, stop and emit the required format instead.", seen["prompt"])
        self.assertIn(
            "Even if the diff contains mathematical notation or scientific code, do not translate it into prose or symbolic notation.",
            seen["prompt"],
        )
        self.assertTrue(seen["prompt"].endswith("diff with orbital math"))

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


def _make_review_brief(*, reviewer="gemini", model=None):
    """Helper: build a minimal ReviewBrief for testing."""
    from velora.protocol import ReviewBrief, ReviewScope

    return ReviewBrief(
        id="RB-0001",
        reviewer=reviewer,
        model=model,
        objective="Verify the diff is correct and safe",
        acceptance_criteria=["All tests pass", "No security regressions"],
        rejection_criteria=["Introduces a crash", "Breaks existing API"],
        areas_of_concern=["error handling in run.py", "protocol validation"],
        scope=ReviewScope(
            kind="full_diff",
            base_ref="main",
            head_sha="abc1234",
            files=["velora/run.py"],
        ),
    )


class TestRunStructuredReview(unittest.TestCase):
    def test_builds_prompt_with_brief_fields(self):
        """Verify the prompt includes objective, acceptance_criteria, areas_of_concern."""
        seen: dict[str, str] = {}

        def fake_generate_content(*, api_key, model, prompt, max_output_tokens):
            seen["prompt"] = prompt
            return '{"review_brief_id":"RB-0001","verdict":"approve","findings":[],"summary":"ok"}'

        brief = _make_review_brief()

        with patch("velora.acpx.get_vault_key", return_value="dummy"), patch(
            "velora.acpx._gemini_generate_content", side_effect=fake_generate_content
        ):
            from velora.acpx import run_structured_review

            res = run_structured_review(brief, "diff text here")

        self.assertEqual(res.returncode, 0)
        prompt = seen["prompt"]

        # Objective
        self.assertIn("Verify the diff is correct and safe", prompt)
        # Acceptance criteria
        self.assertIn("All tests pass", prompt)
        self.assertIn("No security regressions", prompt)
        # Areas of concern
        self.assertIn("error handling in run.py", prompt)
        self.assertIn("protocol validation", prompt)
        # Rejection criteria
        self.assertIn("Introduces a crash", prompt)
        self.assertIn("Breaks existing API", prompt)
        # Diff
        self.assertIn("diff text here", prompt)

    def test_includes_schema_instructions(self):
        """Verify the prompt tells the reviewer to output JSON with ReviewResult structure."""
        seen: dict[str, str] = {}

        def fake_generate_content(*, api_key, model, prompt, max_output_tokens):
            seen["prompt"] = prompt
            return '{"review_brief_id":"RB-0001","verdict":"approve","findings":[],"summary":"ok"}'

        brief = _make_review_brief()

        with patch("velora.acpx.get_vault_key", return_value="dummy"), patch(
            "velora.acpx._gemini_generate_content", side_effect=fake_generate_content
        ):
            from velora.acpx import run_structured_review

            run_structured_review(brief, "some diff")

        prompt = seen["prompt"]

        # Must instruct JSON output
        self.assertIn("review_brief_id", prompt)
        self.assertIn('"verdict"', prompt)
        self.assertIn('"findings"', prompt)
        self.assertIn('"severity"', prompt)
        self.assertIn('"category"', prompt)
        self.assertIn('"approve"', prompt)
        self.assertIn('"reject"', prompt)
        self.assertIn('"blocker"', prompt)
        self.assertIn('"nit"', prompt)
        self.assertIn("summary", prompt)
        self.assertIn("JSON", prompt)

    def test_dispatches_to_gemini(self):
        """Verify Gemini is called when reviewer='gemini'."""
        gemini_called = {"count": 0}

        def fake_generate_content(*, api_key, model, prompt, max_output_tokens):
            gemini_called["count"] += 1
            return '{"review_brief_id":"RB-0001","verdict":"approve","findings":[],"summary":"ok"}'

        brief = _make_review_brief(reviewer="gemini")

        with patch("velora.acpx.get_vault_key", return_value="dummy"), patch(
            "velora.acpx._gemini_generate_content", side_effect=fake_generate_content
        ):
            from velora.acpx import run_structured_review

            res = run_structured_review(brief, "diff")

        self.assertEqual(res.returncode, 0)
        self.assertEqual(gemini_called["count"], 1)

    def test_dispatches_to_claude(self):
        """Verify Claude is called when reviewer='claude'."""
        from velora.acpx import CmdResult, run_structured_review

        brief = _make_review_brief(reviewer="claude")

        with patch("velora.acpx.run_claude", return_value=CmdResult(returncode=0, stdout="ok\n", stderr="")) as mock_claude:
            res = run_structured_review(brief, "diff")

        self.assertEqual(res.returncode, 0)
        mock_claude.assert_called_once()
        call_kwargs = mock_claude.call_args
        # Verify the session name includes the brief ID
        self.assertIn("RB-0001", call_kwargs[1]["session_name"])

    def test_rejects_non_review_brief(self):
        """Verify TypeError if brief is not a ReviewBrief."""
        from velora.acpx import run_structured_review

        with self.assertRaises(TypeError):
            run_structured_review({"not": "a brief"}, "diff")

    def test_gemini_error_returns_failure(self):
        """Verify Gemini API errors are caught and returned as rc=1."""

        def fake_generate_content(*, api_key, model, prompt, max_output_tokens):
            raise RuntimeError("API is down")

        brief = _make_review_brief(reviewer="gemini")

        with patch("velora.acpx.get_vault_key", return_value="dummy"), patch(
            "velora.acpx._gemini_generate_content", side_effect=fake_generate_content
        ):
            from velora.acpx import run_structured_review

            res = run_structured_review(brief, "diff")

        self.assertEqual(res.returncode, 1)
        self.assertIn("API is down", res.stderr)


if __name__ == "__main__":
    unittest.main()
