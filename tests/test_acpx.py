import unittest
from unittest.mock import patch

from velora.acpx import parse_codex_footer, resolve_acpx_cmd


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

    def test_raises_if_missing_everywhere(self):
        with patch("velora.acpx.which", return_value=None), patch("velora.acpx._fallback_acpx_exists", return_value=False):
            with self.assertRaises(RuntimeError):
                resolve_acpx_cmd(env={})


if __name__ == "__main__":
    unittest.main()
