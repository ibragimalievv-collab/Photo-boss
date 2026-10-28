"""Offline certificate/TLS tests use generated identities, never bank secrets."""

import base64
import os
import ssl
import stat
import tempfile
import traceback
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from app.sber_qr_certificates import (
    MAX_CERTIFICATE_FILE_BYTES,
    SberQRCertificateError,
    build_sber_qr_ssl_context,
)
from app.sber_qr_config import SberQRConfig

TEST_PASSWORD = "synthetic-only-certificate-password"


def issue_certificate(key, common_name, issuer, signing_key, *, ca=False,
                      usage=None, starts=None, ends=None):
    now = datetime.now(UTC)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    builder = (x509.CertificateBuilder().subject_name(subject)
        .issuer_name(issuer.subject if issuer is not None else subject)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(starts or now - timedelta(days=1))
        .not_valid_after(ends or now + timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=ca,
            crl_sign=ca, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(signing_key.public_key()),
                       critical=False))
    if usage is not None:
        builder = builder.add_extension(x509.ExtendedKeyUsage([usage]), critical=False)
    if usage == ExtendedKeyUsageOID.SERVER_AUTH:
        builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName("bank.test")]),
                                        critical=False)
    return builder.sign(signing_key, hashes.SHA256())


def handshake_in_memory(client_context, server_context, hostname="bank.test"):
    """Exercise real mTLS without opening a socket or contacting any service."""
    incoming_client, outgoing_client = ssl.MemoryBIO(), ssl.MemoryBIO()
    incoming_server, outgoing_server = ssl.MemoryBIO(), ssl.MemoryBIO()
    client = client_context.wrap_bio(incoming_client, outgoing_client,
                                     server_side=False, server_hostname=hostname)
    server = server_context.wrap_bio(incoming_server, outgoing_server, server_side=True)
    client_done = server_done = False
    for _ in range(50):
        if not client_done:
            try:
                client.do_handshake()
                client_done = True
            except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                pass
        pending = outgoing_client.read()
        if pending:
            incoming_server.write(pending)
        if not server_done:
            try:
                server.do_handshake()
                server_done = True
            except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                pass
        pending = outgoing_server.read()
        if pending:
            incoming_client.write(pending)
        if client_done and server_done:
            return client, server
    raise AssertionError("synthetic TLS handshake did not finish")


class SberQRCertificateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root_key = ec.generate_private_key(ec.SECP256R1())
        cls.root = issue_certificate(cls.root_key, "Synthetic QR Root", None,
                                     cls.root_key, ca=True)
        cls.client_key = ec.generate_private_key(ec.SECP256R1())
        cls.client = issue_certificate(cls.client_key, "Synthetic QR Client", cls.root,
                                       cls.root_key, usage=ExtendedKeyUsageOID.CLIENT_AUTH)
        cls.server_key = ec.generate_private_key(ec.SECP256R1())
        cls.server = issue_certificate(cls.server_key, "bank.test", cls.root,
                                       cls.root_key, usage=ExtendedKeyUsageOID.SERVER_AUTH)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.certificate_file = self.base / "synthetic-client.p12.b64"
        self.ca_file = self.base / "synthetic-root.pem"
        self.ca_file.write_bytes(self.root.public_bytes(serialization.Encoding.PEM))
        self.write_bundle()

    def write_bundle(self, *, key=None, certificate=None, password=TEST_PASSWORD,
                     encryption=None):
        data = pkcs12.serialize_key_and_certificates(
            b"synthetic-client", key if key is not None else self.client_key,
            certificate if certificate is not None else self.client, [self.root],
            encryption or serialization.BestAvailableEncryption(password.encode("utf-8")))
        encoded = base64.b64encode(data)
        self.certificate_file.write_bytes(b"\r\n".join(
            encoded[offset:offset + 64] for offset in range(0, len(encoded), 64)))

    def config(self, **overrides):
        values = {"certificate_file": str(self.certificate_file),
                  "certificate_password": TEST_PASSWORD}
        values.update(overrides)
        return SberQRConfig(**values)

    def server_context(self):
        certificate_file = self.base / "synthetic-server.pem"
        key_file = self.base / "synthetic-server-key.pem"
        certificate_file.write_bytes(self.server.public_bytes(serialization.Encoding.PEM))
        key_file.write_bytes(self.server_key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate_file, key_file)
        context.load_verify_locations(cafile=self.ca_file)
        context.verify_mode = ssl.CERT_REQUIRED
        return context

    def assert_safe_failure(self, config, code=None, forbidden=()):
        with self.assertRaises(SberQRCertificateError) as caught:
            build_sber_qr_ssl_context(config)
        error = caught.exception
        if code is not None:
            self.assertEqual(str(error), code)
        rendered = "".join(traceback.format_exception(error)) + repr(error)
        for value in (TEST_PASSWORD, str(self.certificate_file), *forbidden):
            self.assertNotIn(value, rendered)
        self.assertTrue(error.__suppress_context__ or error.__context__ is None)
        return error

    def test_real_context_uses_private_encrypted_temporary_files_and_removes_them(self):
        original = ssl.SSLContext.load_cert_chain
        paths = []
        def inspect(context, certfile, keyfile, password):
            for path in (Path(certfile), Path(keyfile)):
                paths.append(path)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertIn(b"BEGIN ENCRYPTED PRIVATE KEY", Path(keyfile).read_bytes())
            self.assertNotEqual(password, TEST_PASSWORD.encode())
            return original(context, certfile, keyfile, password)
        with patch.object(ssl.SSLContext, "load_cert_chain", new=inspect):
            context = build_sber_qr_ssl_context(self.config())
        self.assertIsInstance(context, ssl.SSLContext)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertGreaterEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
        self.assertEqual(len(paths), 2)
        self.assertTrue(all(not path.exists() and not path.parent.exists() for path in paths))

    def test_real_mutual_tls_handshake_works_after_temporary_files_are_removed(self):
        context = build_sber_qr_ssl_context(self.config(ca_file=str(self.ca_file)))
        client, server = handshake_in_memory(context, self.server_context())
        self.assertEqual(client.getpeercert()["subject"][0][0][1], "bank.test")
        self.assertEqual(server.getpeercert()["subject"][0][0][1], "Synthetic QR Client")

    def test_custom_ca_extends_system_roots_and_pkcs12_chain_is_not_trusted(self):
        original_roots = set(ssl.create_default_context().get_ca_certs(binary_form=True))
        root_der = self.root.public_bytes(serialization.Encoding.DER)
        default = build_sber_qr_ssl_context(self.config())
        self.assertNotIn(root_der, default.get_ca_certs(binary_form=True))
        extended = build_sber_qr_ssl_context(self.config(ca_file=str(self.ca_file)))
        roots = set(extended.get_ca_certs(binary_form=True))
        self.assertTrue(original_roots.issubset(roots))
        self.assertIn(root_der, roots)
        self.assertEqual(extended.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(extended.check_hostname)

    def test_server_from_pkcs12_chain_is_rejected_without_explicit_trust(self):
        context = build_sber_qr_ssl_context(self.config())
        with self.assertRaises(ssl.SSLCertVerificationError):
            handshake_in_memory(context, self.server_context())

    def test_server_hostname_mismatch_is_rejected(self):
        context = build_sber_qr_ssl_context(self.config(ca_file=str(self.ca_file)))
        with self.assertRaises(ssl.SSLCertVerificationError):
            handshake_in_memory(context, self.server_context(), hostname="wrong-bank.test")

    def test_wrong_password_and_invalid_pkcs12_are_sanitized(self):
        self.assert_safe_failure(self.config(certificate_password="wrong-synthetic-password"),
                                 "pkcs12_decryption_failed", ("wrong-synthetic-password",))
        self.certificate_file.write_bytes(base64.b64encode(b"synthetic-corrupt-pkcs12"))
        self.assert_safe_failure(self.config(), "pkcs12_decryption_failed",
                                 ("synthetic-corrupt-pkcs12",))

    def test_empty_noncanonical_and_invalid_base64_are_rejected(self):
        for content, code in ((b" \r\n\t", "certificate_file_empty"),
                              (b"not-base64!", "certificate_base64_invalid"),
                              (b"YQ===", "certificate_base64_invalid"),
                              (b"YR==", "certificate_base64_invalid")):
            with self.subTest(code=code, content=content):
                self.certificate_file.write_bytes(content)
                self.assert_safe_failure(self.config(), code)

    def test_unprotected_pkcs12_is_not_accepted_as_encrypted_storage(self):
        self.write_bundle(encryption=serialization.NoEncryption())
        self.assert_safe_failure(self.config(), "pkcs12_password_protection_required")

    def test_certificate_only_bundle_cannot_act_as_client_identity(self):
        data = pkcs12.serialize_key_and_certificates(b"synthetic-certs-only", None,
            None, [self.root], serialization.BestAvailableEncryption(TEST_PASSWORD.encode()))
        self.certificate_file.write_bytes(base64.b64encode(data))
        self.assert_safe_failure(self.config(), "client_identity_missing")

    def test_expired_and_future_client_certificates_are_rejected(self):
        now = datetime.now(UTC)
        for starts, ends in ((now - timedelta(days=3), now - timedelta(days=1)),
                             (now + timedelta(days=1), now + timedelta(days=3))):
            with self.subTest(starts=starts):
                certificate = issue_certificate(self.client_key, "Synthetic QR Client",
                    self.root, self.root_key, usage=ExtendedKeyUsageOID.CLIENT_AUTH,
                    starts=starts, ends=ends)
                self.write_bundle(certificate=certificate)
                self.assert_safe_failure(self.config(), "client_certificate_not_current")

    def test_explicit_server_only_usage_is_rejected_but_missing_eku_is_accepted(self):
        certificate = issue_certificate(self.client_key, "Synthetic QR Client",
            self.root, self.root_key, usage=ExtendedKeyUsageOID.SERVER_AUTH)
        self.write_bundle(certificate=certificate)
        self.assert_safe_failure(self.config(), "client_auth_usage_required")
        certificate = issue_certificate(self.client_key, "Synthetic QR Client", self.root, self.root_key)
        self.write_bundle(certificate=certificate)
        self.assertIsInstance(build_sber_qr_ssl_context(self.config()), ssl.SSLContext)

    def test_mismatched_key_and_certificate_are_rejected(self):
        wrong_key = ec.generate_private_key(ec.SECP256R1())
        with patch("app.sber_qr_certificates.pkcs12.load_key_and_certificates",
                   side_effect=[ValueError("synthetic-private-detail"),
                                (wrong_key, self.client, [self.root])]):
            self.assert_safe_failure(self.config(), "client_identity_mismatch",
                                     ("synthetic-private-detail",))

    def test_file_size_and_type_are_checked_and_render_symlinks_work(self):
        link = self.base / "render-like-link.b64"
        link.symlink_to(self.certificate_file)
        self.assertIsInstance(build_sber_qr_ssl_context(self.config(certificate_file=str(link))),
                              ssl.SSLContext)
        self.certificate_file.write_bytes(b"A" * (MAX_CERTIFICATE_FILE_BYTES + 1))
        self.assert_safe_failure(self.config(), "certificate_file_too_large")
        self.assert_safe_failure(self.config(certificate_file=str(self.base)),
                                 "certificate_file_invalid", (str(self.base),))
        if hasattr(os, "mkfifo"):
            fifo = self.base / "synthetic-fifo"
            os.mkfifo(fifo)
            self.assert_safe_failure(self.config(certificate_file=str(fifo)),
                                     "certificate_file_invalid", (str(fifo),))

    def test_missing_settings_and_unsupported_format_fail_closed(self):
        for overrides, code in (({"certificate_password": ""}, "certificate_password_missing"),
                                ({"certificate_file": ""}, "certificate_file_missing"),
                                ({"certificate_format": "pem"}, "certificate_format_unsupported")):
            with self.subTest(code=code):
                self.assert_safe_failure(self.config(**overrides), code)
        missing = self.base / "synthetic-missing-file.b64"
        self.assert_safe_failure(self.config(certificate_file=str(missing)),
                                 "certificate_file_unavailable", (str(missing),))

    def test_failed_tls_load_cleans_temporary_files_and_hides_underlying_error(self):
        paths = []
        def fail(context, certfile, keyfile, password):
            paths.extend([Path(certfile), Path(keyfile)])
            raise OSError("synthetic-private-native-error " + str(keyfile) + " " + TEST_PASSWORD)
        with patch.object(ssl.SSLContext, "load_cert_chain", new=fail):
            self.assert_safe_failure(self.config(), "certificate_context_load_failed",
                                     ("synthetic-private-native-error",))
        self.assertEqual(len(paths), 2)
        self.assertTrue(all(not path.exists() and not path.parent.exists() for path in paths))

    def test_bad_optional_ca_fails_without_disabling_server_verification(self):
        self.ca_file.write_bytes(b"synthetic-invalid-ca")
        self.assert_safe_failure(self.config(ca_file=str(self.ca_file)),
                                 "server_ca_load_failed", (str(self.ca_file),))
        self.ca_file.write_bytes(b"A" * (MAX_CERTIFICATE_FILE_BYTES + 1))
        self.assert_safe_failure(self.config(ca_file=str(self.ca_file)),
                                 "server_ca_load_failed", (str(self.ca_file),))
        if hasattr(os, "mkfifo"):
            fifo = self.base / "synthetic-ca-fifo"
            os.mkfifo(fifo)
            self.assert_safe_failure(self.config(ca_file=str(fifo)),
                                     "server_ca_load_failed", (str(fifo),))

    def test_insecure_context_cannot_be_returned(self):
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        with patch("app.sber_qr_certificates.ssl.create_default_context", return_value=context):
            self.assert_safe_failure(self.config(), "server_verification_required")


if __name__ == "__main__":
    unittest.main()
