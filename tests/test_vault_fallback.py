import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from velora import acpx
from velora.config import get_config


class TestVaultFallback(unittest.TestCase):
    def setUp(self):
        get_config.cache_clear()
        acpx._load_vault_api_keys.cache_clear()

    def tearDown(self):
        get_config.cache_clear()
        acpx._load_vault_api_keys.cache_clear()

    def test_env_wins_no_vault_attempt(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "env-value"}, clear=False):
            get_config.cache_clear()
            with patch("velora.acpx._load_vault_api_keys", side_effect=AssertionError("Vault should not be called")):
                self.assertEqual(acpx.get_vault_key("OPENAI_API_KEY"), "env-value")

    def test_missing_env_and_missing_vault_config_errors_helpfully(self):
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "",
                "VELORA_VAULT_ROLE_ID_FILE": "/tmp/velora-nope-role-id",
                "VELORA_VAULT_SECRET_ID_FILE": "/tmp/velora-nope-secret-id",
            },
            clear=False,
        ):
            get_config.cache_clear()
            with patch("velora.acpx._load_vault_api_keys", side_effect=AssertionError("Vault should not be called")):
                with self.assertRaises(RuntimeError) as cm:
                    acpx.get_vault_key("OPENAI_API_KEY")
            msg = str(cm.exception)
            self.assertIn("OPENAI_API_KEY", msg)
            self.assertIn("Set OPENAI_API_KEY", msg)
            self.assertIn("Vault fallback is not configured", msg)

    def test_vault_fallback_used_when_configured(self):
        with tempfile.TemporaryDirectory() as td:
            role = Path(td) / "role_id"
            secret = Path(td) / "secret_id"
            role.write_text("role", encoding="utf-8")
            secret.write_text("secret", encoding="utf-8")

            def fake_vault_request(method: str, path: str, body=None, token=None):
                if path == "/v1/auth/approle/login":
                    return {"auth": {"client_token": "tok"}}
                if path == "/v1/secret/data/velora/api-keys":
                    return {"data": {"data": {"OPENAI_API_KEY": "vault-value"}}}
                raise AssertionError(f"Unexpected vault request: {method} {path}")

            with patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "",
                    "VELORA_VAULT_ADDR": "http://vault.local:8200",
                    "VELORA_VAULT_ROLE_ID_FILE": str(role),
                    "VELORA_VAULT_SECRET_ID_FILE": str(secret),
                    "VELORA_VAULT_API_KEYS_PATH": "/v1/secret/data/velora/api-keys",
                },
                clear=False,
            ):
                get_config.cache_clear()
                acpx._load_vault_api_keys.cache_clear()
                with patch("velora.acpx._vault_request", side_effect=fake_vault_request):
                    self.assertEqual(acpx.get_vault_key("OPENAI_API_KEY"), "vault-value")


if __name__ == "__main__":
    unittest.main()
