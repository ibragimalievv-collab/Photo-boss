"""Load an encrypted QR.API client certificate without weakening server TLS.

Only the returned SSLContext leaves this module. It does not make bank calls or
enable payments. PKCS12 bytes, passwords, keys and temporary paths are private.
"""

import base64
import binascii
import os
import ssl
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID

from .sber_qr_config import SberQRConfig

MAX_CERTIFICATE_FILE_BYTES = 1024 * 1024
_ASCII_WHITESPACE = b" \t\r\n\v\f"


class SberQRCertificateError(ValueError):
    """An allowlisted setup error; never include provider errors or paths."""


def _read_bounded_file(path: str) -> bytes:
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise SberQRCertificateError("certificate_file_invalid")
        if info.st_size > MAX_CERTIFICATE_FILE_BYTES:
            raise SberQRCertificateError("certificate_file_too_large")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            encoded = stream.read(MAX_CERTIFICATE_FILE_BYTES + 1)
        if len(encoded) > MAX_CERTIFICATE_FILE_BYTES:
            raise SberQRCertificateError("certificate_file_too_large")
        return encoded
    except SberQRCertificateError:
        raise
    except (OSError, ValueError):
        raise SberQRCertificateError("certificate_file_unavailable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _decode_pkcs12(encoded: bytes) -> bytes:
    compact = encoded.translate(None, _ASCII_WHITESPACE)
    if not compact:
        raise SberQRCertificateError("certificate_file_empty")
    try:
        payload = base64.b64decode(compact, validate=True)
    except (ValueError, binascii.Error):
        raise SberQRCertificateError("certificate_base64_invalid") from None
    if base64.b64encode(payload) != compact:
        raise SberQRCertificateError("certificate_base64_invalid")
    return payload


def _load_client_identity(payload: bytes, password: str):
    # Reject containers that open without a password. This does not attest the
    # encryption of every PKCS12 bag; callers must supply the encrypted bank file.
    try:
        pkcs12.load_key_and_certificates(payload, None)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        pass
    else:
        raise SberQRCertificateError("pkcs12_password_protection_required")
    try:
        key, certificate, chain = pkcs12.load_key_and_certificates(
            payload, password.encode("utf-8")
        )
    except (ValueError, TypeError, UnsupportedAlgorithm):
        raise SberQRCertificateError("pkcs12_decryption_failed") from None
    if key is None or certificate is None:
        raise SberQRCertificateError("client_identity_missing")
    public_format = serialization.PublicFormat.SubjectPublicKeyInfo
    encoding = serialization.Encoding.DER
    if (key.public_key().public_bytes(encoding, public_format)
            != certificate.public_key().public_bytes(encoding, public_format)):
        raise SberQRCertificateError("client_identity_mismatch")
    now = datetime.now(UTC)
    if not certificate.not_valid_before_utc <= now < certificate.not_valid_after_utc:
        raise SberQRCertificateError("client_certificate_not_current")
    try:
        usages = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    except x509.ExtensionNotFound:
        pass
    else:
        if (ExtendedKeyUsageOID.CLIENT_AUTH not in usages
                and ExtendedKeyUsageOID.ANY_EXTENDED_KEY_USAGE not in usages):
            raise SberQRCertificateError("client_auth_usage_required")
    return key, certificate, chain


def _write_private_file(path: Path, content: bytes):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(content)


def build_sber_qr_ssl_context(config: SberQRConfig) -> ssl.SSLContext:
    """Build a verified mTLS context from base64 of an encrypted PKCS12 file.

    This is an offline local check, not bank acceptance. Temporary PKCS8 files
    are additionally encrypted with a one-use random password, are mode 0600 in
    a private directory, and are removed before returning or raising. Native
    Python/OpenSSL requires paths for load_cert_chain; no key file is retained.
    Only safe error codes may be logged by callers, never config or exceptions
    from the underlying parser, filesystem or SSL stack.
    """
    try:
        if config.certificate_format != "p12-base64":
            raise SberQRCertificateError("certificate_format_unsupported")
        if not config.certificate_file or not config.certificate_file.strip():
            raise SberQRCertificateError("certificate_file_missing")
        if not config.certificate_password or not config.certificate_password.strip():
            raise SberQRCertificateError("certificate_password_missing")
        payload = _decode_pkcs12(_read_bounded_file(config.certificate_file))
        key, certificate, chain = _load_client_identity(payload, config.certificate_password)
        context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
        context.minimum_version = max(context.minimum_version, ssl.TLSVersion.TLSv1_2)
        if config.ca_file:
            try:
                ca_pem = _read_bounded_file(config.ca_file).decode("ascii")
                context.load_verify_locations(cadata=ca_pem)
            except (OSError, ValueError, TypeError):
                raise SberQRCertificateError("server_ca_load_failed") from None
        if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
            raise SberQRCertificateError("server_verification_required")
        # Additional PKCS12 certificates are sent as the client chain. They are
        # deliberately NOT added to the trust store for the bank's server.
        certificate_pem = certificate.public_bytes(serialization.Encoding.PEM)
        certificate_pem += b"".join(cert.public_bytes(serialization.Encoding.PEM) for cert in chain)
        one_use_password = base64.urlsafe_b64encode(os.urandom(32))
        private_key_pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(one_use_password),
        )
        with tempfile.TemporaryDirectory(prefix="pb-sber-qr-") as directory:
            os.chmod(directory, 0o700)
            certificate_path = Path(directory) / "client-chain.pem"
            key_path = Path(directory) / "client-key.pem"
            _write_private_file(certificate_path, certificate_pem)
            _write_private_file(key_path, private_key_pem)
            context.load_cert_chain(
                certfile=certificate_path, keyfile=key_path, password=one_use_password
            )
        return context
    except SberQRCertificateError:
        raise
    except Exception:  # noqa: BLE001 -- redact native/parser errors at the secret boundary
        # Never attach filesystem paths, password/parser text, or native SSL
        # errors to the public exception. No bank credential leaves this call.
        raise SberQRCertificateError("certificate_context_load_failed") from None
