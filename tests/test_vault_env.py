import os
import unittest
from pathlib import Path
from unittest.mock import call, patch

import velora.acpx as acpx
from velora.config import get_config


class TestVaultEnvOverrides(unittest.TestCase):
    def setUp(self):
        # Caches: config + Vault keys.
        get_config.cache_clear()
        acpx._load_vault_api_keys.cache_clear()

    def tearDown(self):
        get_config.cache_clear()
        acpx._load_vault_api_keys.cache_clear()

    def test_vault_addr_override(self):
        with patch.dict(os.environ, {"VELORA_VAULT_ADDR": "https://vault.example:8200"}, clear=False):
            get_config.cache_clear()
            self.assertEqual(acpx._vault_addr(), "https://vault.example:8200")

    def test_vault_credential_and_secret_path_overrides(self):
        env = {
            "VELORA_VAULT_ROLE_ID_FILE": "/tmp/role_id",
            "VELORA_VAULT_SECRET_ID_FILE": "/tmp/secret_id",
            "VELORA_VAULT_API_KEYS_PATH": "/v1/secret/data/custom/api-keys",
        }

        def fake_vault_request(method, path, body=None, token=None):
            if method == "POST" and path == "/v1/auth/approle/login":
                return {"auth": {"client_token": "tok"}}
            if method == "GET" and path == env["VELORA_VAULT_API_KEYS_PATH"]:
                return {"data": {"data": {"OPENAI_API_KEY": "x"}}}
            raise AssertionError(f"unexpected vault request: {method} {path}")

        with patch.dict(os.environ, env, clear=False), patch(
            "velora.acpx._read_file", side_effect=["role", "secret"]
        ) as rf, patch("velora.acpx._vault_request", side_effect=fake_vault_request) as vr:
            get_config.cache_clear()
            acpx._load_vault_api_keys.cache_clear()
            keys = acpx._load_vault_api_keys()

        self.assertEqual(keys["OPENAI_API_KEY"], "x")
        rf.assert_has_calls([call(Path("/tmp/role_id")), call(Path("/tmp/secret_id"))], any_order=False)
        # Ensure we used the overridden secret path.
        self.assertEqual(vr.call_args_list[1].args[1], env["VELORA_VAULT_API_KEYS_PATH"])


if __name__ == "__main__":
    unittest.main()
