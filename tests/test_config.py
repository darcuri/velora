import json
import os
import tempfile
import unittest
from unittest.mock import patch

from velora.config import get_config, load_config


class TestConfigLoading(unittest.TestCase):
    def tearDown(self):
        get_config.cache_clear()

    def test_loads_from_config_file_via_env_path(self):
        payload = {
            "allowed_owners": ["octocat"],
            "max_attempts": 2,
            "codex_session_prefix": "x-",
        }
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=True) as fh:
            fh.write(json.dumps(payload))
            fh.flush()
            with patch.dict(os.environ, {"VELORA_CONFIG_PATH": fh.name}, clear=False):
                cfg = load_config()

        self.assertEqual(cfg.allowed_owners, {"octocat"})
        self.assertEqual(cfg.max_attempts, 2)
        self.assertEqual(cfg.codex_session_prefix, "x-")

    def test_env_overrides_config_file(self):
        payload = {"max_attempts": 2}
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=True) as fh:
            fh.write(json.dumps(payload))
            fh.flush()
            with patch.dict(
                os.environ,
                {"VELORA_CONFIG_PATH": fh.name, "VELORA_MAX_ATTEMPTS": "5"},
                clear=False,
            ):
                cfg = load_config()

        self.assertEqual(cfg.max_attempts, 5)


if __name__ == "__main__":
    unittest.main()
