"""QR.API setup must stay inactive, separate, and safe to inspect."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.sber_qr_config import SberQRConfig


class SberQRConfigTests(unittest.TestCase):
    def test_empty_environment_is_disabled_and_incomplete_in_sandbox(self):
        config = SberQRConfig.from_env({})
        status = config.configuration_status()
        self.assertFalse(status.enabled)
        self.assertEqual(status.environment, "sandbox")
        self.assertEqual(config.certificate_format, "p12-base64")
        self.assertFalse(status.configuration_complete)
        self.assertEqual(set(status.issues), {
            "SBER_QR_CLIENT_ID:missing", "SBER_QR_CLIENT_SECRET:missing",
            "SBER_QR_CERT_PASSWORD:missing", "SBER_QR_CERT_FILE:missing",
        })

    def test_old_acquiring_credentials_do_not_configure_qr_api(self):
        config = SberQRConfig.from_env({
            "SBER_PAYMENTS_ENABLED": "1",
            "SBER_PAYMENTS_TEST": "0",
            "SBER_MERCHANT_LOGIN": "synthetic-merchant",
            "SBER_MERCHANT_PASSWORD": "synthetic-merchant-password",
        })
        self.assertFalse(config.enabled)
        self.assertEqual(config.environment, "sandbox")
        self.assertFalse(config.configuration_status().configuration_complete)

    def test_three_saved_credentials_do_not_activate_or_complete_setup(self):
        config = SberQRConfig.from_env({
            "SBER_QR_CLIENT_ID": "synthetic-client-id",
            "SBER_QR_CLIENT_SECRET": "synthetic-client-secret",
            "SBER_QR_CERT_PASSWORD": "synthetic-certificate-password",
        })
        status = config.configuration_status()
        self.assertFalse(status.enabled)
        self.assertFalse(status.configuration_complete)
        self.assertEqual(status.issues, ("SBER_QR_CERT_FILE:missing",))

    def test_explicit_environment_does_not_replace_the_enable_switch(self):
        for environment in ("sandbox", "production"):
            with self.subTest(environment=environment):
                config = SberQRConfig.from_env({"SBER_QR_ENV": environment})
                self.assertEqual(config.environment, environment)
                self.assertFalse(config.enabled)
        self.assertTrue(SberQRConfig.from_env({"SBER_QR_ENABLED": "1"}).enabled)

    def test_invalid_switch_and_environment_fail_without_echoing_values(self):
        for name, value in (
            ("SBER_QR_ENABLED", "synthetic-secret-in-wrong-field"),
            ("SBER_QR_ENV", "synthetic-secret-in-wrong-field"),
            ("SBER_QR_CERT_FORMAT", "synthetic-secret-in-wrong-field"),
        ):
            with self.subTest(name=name):
                with self.assertRaises(ValueError) as caught:
                    SberQRConfig.from_env({name: value})
                self.assertIn(name, str(caught.exception))
                self.assertNotIn(value, str(caught.exception))

    def test_secret_values_are_preserved_but_hidden_in_representations(self):
        values = {
            "SBER_QR_CLIENT_ID": "synthetic-private-client-id",
            "SBER_QR_CLIENT_SECRET": " synthetic-private-secret ",
            "SBER_QR_CERT_PASSWORD": " synthetic-private-password ",
            "SBER_QR_CERT_FILE": "/synthetic-private-certificate-path/client.p12",
            "SBER_QR_CA_FILE": "/synthetic-private-ca-path/root.pem",
        }
        config = SberQRConfig.from_env(values)
        self.assertEqual(config.client_secret, values["SBER_QR_CLIENT_SECRET"])
        self.assertEqual(config.certificate_password, values["SBER_QR_CERT_PASSWORD"])
        for value in values.values():
            self.assertNotIn(value, repr(config))
            self.assertNotIn(value, repr(config.configuration_status()))

    def test_blank_credentials_cannot_count_as_complete(self):
        config = SberQRConfig.from_env({
            "SBER_QR_CLIENT_ID": " ",
            "SBER_QR_CLIENT_SECRET": "\t",
            "SBER_QR_CERT_PASSWORD": "\n",
            "SBER_QR_CERT_FILE": " ",
        })
        status = config.configuration_status()
        self.assertFalse(status.configuration_complete)
        self.assertEqual(len(status.issues), 4)
        self.assertTrue(all(issue.endswith(":missing") for issue in status.issues))

    def test_certificate_must_be_an_available_nonempty_file(self):
        with tempfile.TemporaryDirectory() as directory:
            certificate = Path(directory) / "test-only.p12"
            values = {
                "SBER_QR_CLIENT_ID": "synthetic-client-id",
                "SBER_QR_CLIENT_SECRET": "synthetic-client-secret",
                "SBER_QR_CERT_PASSWORD": "synthetic-certificate-password",
                "SBER_QR_CERT_FILE": str(certificate),
            }
            config = SberQRConfig.from_env(values)
            self.assertEqual(config.configuration_status().issues,
                             ("SBER_QR_CERT_FILE:unavailable",))
            certificate.touch()
            self.assertEqual(config.configuration_status().issues,
                             ("SBER_QR_CERT_FILE:empty",))
            certificate.write_bytes(b"synthetic fixture; not a valid certificate")
            with patch.object(Path, "read_bytes", side_effect=AssertionError("must not read credentials")):
                status = config.configuration_status()
            self.assertTrue(status.configuration_complete)
            self.assertFalse(status.enabled, "Complete staging must not enable payments")
            self.assertEqual(status.issues, ())
            with patch("app.sber_qr_config.os.access", return_value=False):
                self.assertEqual(config.configuration_status().issues,
                                 ("SBER_QR_CERT_FILE:unavailable",))
            values["SBER_QR_CERT_FILE"] = directory
            self.assertEqual(SberQRConfig.from_env(values).configuration_status().issues,
                             ("SBER_QR_CERT_FILE:unavailable",))

    def test_invalid_certificate_path_is_reported_without_a_traceback(self):
        config = SberQRConfig.from_env({"SBER_QR_CERT_FILE": "synthetic\0path"})
        self.assertIn("SBER_QR_CERT_FILE:unavailable", config.configuration_status().issues)

    def test_default_reader_uses_environment_without_modifying_it(self):
        values = {"SBER_QR_CLIENT_ID": "synthetic-from-environment"}
        with patch.dict(os.environ, values, clear=True):
            config = SberQRConfig.from_env()
            self.assertEqual(config.client_id, values["SBER_QR_CLIENT_ID"])
            self.assertEqual(dict(os.environ), values)

    def test_optional_ca_file_must_be_available_if_configured(self):
        config = SberQRConfig.from_env({"SBER_QR_CA_FILE": "/synthetic-missing-ca.pem"})
        self.assertIn("SBER_QR_CA_FILE:unavailable", config.configuration_status().issues)


if __name__ == "__main__":
    unittest.main()
