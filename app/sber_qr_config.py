"""Private configuration for Sber QR.API, separate from hosted-page acquiring.

This module does not connect to Sber, validate a certificate cryptographically,
or enable payments. The certificate loader and API adapter perform their own
validation; complete settings alone do not prove that the bank has granted access.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class SberQRConfigurationStatus:
    """Non-secret setup information; completeness is not bank acceptance."""

    enabled: bool
    environment: str
    configuration_complete: bool
    issues: tuple[str, ...]


@dataclass(frozen=True)
class SberQRConfig:
    """Private credential container; serialize only configuration_status()."""

    enabled: bool = False
    environment: str = "sandbox"
    client_id: str = field(default="", repr=False)
    client_secret: str = field(default="", repr=False)
    certificate_password: str = field(default="", repr=False)
    certificate_file: str = field(default="", repr=False)
    certificate_format: str = "p12-base64"
    ca_file: str = field(default="", repr=False)
    member_id: str = field(default="", repr=False)
    id_qr: str = field(default="", repr=False)
    tid: str = field(default="", repr=False)
    api_base_url: str = field(default="", repr=False)
    token_url: str = field(default="", repr=False)
    endpoints_confirmed: bool = False
    success_code: str = field(default="", repr=False)
    operation_success_code: str = field(default="", repr=False)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "SberQRConfig":
        """Read QR.API keys only; never fall back to ecom merchant credentials.

        Secret values are preserved verbatim. Configuration errors identify the
        setting but never interpolate its value, even when a value is invalid.
        """
        source = os.environ if environ is None else environ
        flag = source.get("SBER_QR_ENABLED", "0")
        if flag not in {"0", "1"}:
            raise ValueError("SBER_QR_ENABLED: expected 0 or 1")
        environment = source.get("SBER_QR_ENV", "sandbox")
        if environment not in {"sandbox", "production"}:
            raise ValueError("SBER_QR_ENV: expected sandbox or production")
        certificate_format = source.get("SBER_QR_CERT_FORMAT", "p12-base64")
        if certificate_format != "p12-base64":
            raise ValueError("SBER_QR_CERT_FORMAT: expected p12-base64")
        endpoints_confirmed = source.get("SBER_QR_ENDPOINTS_CONFIRMED", "0")
        if endpoints_confirmed not in {"0", "1"}:
            raise ValueError("SBER_QR_ENDPOINTS_CONFIRMED: expected 0 or 1")
        return cls(
            enabled=flag == "1",
            environment=environment,
            client_id=source.get("SBER_QR_CLIENT_ID", ""),
            client_secret=source.get("SBER_QR_CLIENT_SECRET", ""),
            certificate_password=source.get("SBER_QR_CERT_PASSWORD", ""),
            certificate_file=source.get("SBER_QR_CERT_FILE", ""),
            certificate_format=certificate_format,
            ca_file=source.get("SBER_QR_CA_FILE", ""),
            member_id=source.get("SBER_QR_MEMBER_ID", ""),
            id_qr=source.get("SBER_QR_ID_QR", ""),
            tid=source.get("SBER_QR_TID", ""),
            api_base_url=source.get("SBER_QR_API_BASE_URL", ""),
            token_url=source.get("SBER_QR_TOKEN_URL", ""),
            endpoints_confirmed=endpoints_confirmed == "1",
            success_code=source.get("SBER_QR_SUCCESS_CODE", ""),
            operation_success_code=source.get("SBER_QR_OPERATION_SUCCESS_CODE", ""),
        )

    def configuration_status(self) -> SberQRConfigurationStatus:
        """Check presence and file availability without reading credentials.

        The certificate file is not parsed. This check cannot establish that
        credentials match, the certificate is valid, or a bank tariff is active.
        """
        issues = []
        for name, value in (
            ("SBER_QR_CLIENT_ID", self.client_id),
            ("SBER_QR_CLIENT_SECRET", self.client_secret),
            ("SBER_QR_CERT_PASSWORD", self.certificate_password),
            ("SBER_QR_CERT_FILE", self.certificate_file),
        ):
            if not value or not value.strip():
                issues.append(f"{name}:missing")
        if self.certificate_file and self.certificate_file.strip():
            try:
                path = Path(self.certificate_file)
                if not path.is_file() or not os.access(path, os.R_OK):
                    issues.append("SBER_QR_CERT_FILE:unavailable")
                elif path.stat().st_size == 0:
                    issues.append("SBER_QR_CERT_FILE:empty")
            except (OSError, ValueError):
                issues.append("SBER_QR_CERT_FILE:unavailable")
        if self.ca_file:
            try:
                ca_path = Path(self.ca_file)
                if (not ca_path.is_file() or not os.access(ca_path, os.R_OK)
                        or ca_path.stat().st_size == 0):
                    issues.append("SBER_QR_CA_FILE:unavailable")
            except (OSError, ValueError):
                issues.append("SBER_QR_CA_FILE:unavailable")
        return SberQRConfigurationStatus(
            enabled=self.enabled,
            environment=self.environment,
            configuration_complete=not issues,
            issues=tuple(issues),
        )
