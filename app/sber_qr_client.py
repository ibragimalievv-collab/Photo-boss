"""Bounded Sber QR.API client; no accounting or unverified sandbox defaults.

The QR v3 request schemas come from Sber's current order-create/status-request
reference. Deployment must supply the bank-approved sandbox endpoints and code
mapping: current public examples contain placeholders rather than those values.
Only check_connection() can run while the payment feature is disabled.
"""

import base64
import json
import re
import ssl
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import uuid4

import aiohttp

from .sber_qr_certificates import SberQRCertificateError, build_sber_qr_ssl_context
from .sber_qr_config import SberQRConfig

PRODUCTION_API_BASE = "https://mc.api.sberbank.ru/prod"
PRODUCTION_TOKEN_URL = "https://mc.api.sberbank.ru/prod/tokens/v3/oauth"
SCOPE_CREATE = "https://api.sberbank.ru/qr/order.create"
SCOPE_STATUS = "https://api.sberbank.ru/qr/order.status"
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_AMOUNT_KOPECKS = 999999999999999
_API_HOSTS = {"mc.api.sberbank.ru", "dev.api.sberbank.ru"}
_PAYMENT_HOSTS = {"qr.sberbank.ru", "qr.nspk.ru"}
_IDENTIFIER = re.compile(r"[a-zA-Z0-9_\-\\]+\Z")
_STATES = {"PAID", "CREATED", "ON_PAYMENT", "REVERSED", "REFUNDED", "REVOKED", "DECLINED", "EXPIRED"}


class SberQRClientError(ValueError):
    """Static, non-secret diagnostic code suitable for owner diagnostics."""

    def __init__(self, code: str, *, http_status: int | None = None):
        self.code = code
        self.http_status = http_status
        super().__init__(code)


def _https_parts(value):
    if not isinstance(value, str) or not value or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        return None
    try:
        p = urlsplit(value)
        if (p.scheme != "https" or p.username is not None or p.password is not None
                or p.port not in (None, 443) or not p.hostname):
            return None
        return p
    except ValueError:
        return None


def is_payment_url(value) -> bool:
    """Conservative QR payload policy; no arbitrary bank-returned web hosts."""
    parts = _https_parts(value)
    return bool(parts and len(value) <= 256 and parts.hostname in _PAYMENT_HOSTS
                and not parts.fragment and parts.path not in {"", "/"})


def _valid_api_base(value):
    parts = _https_parts(value)
    return bool(parts and parts.hostname in _API_HOSTS and not parts.query and not parts.fragment
                and re.fullmatch(r"/(?:ru/)?(?:prod|test|sandbox|sbx)", parts.path.rstrip("/")))


def _valid_token_url(value):
    parts = _https_parts(value)
    return bool(parts and parts.hostname in _API_HOSTS and not parts.query and not parts.fragment
                and re.fullmatch(r"/(?:ru/)?(?:prod|test|sandbox|sbx)/tokens/v[23]/oauth", parts.path))


def _endpoints(config):
    if config.environment == "production":
        return (config.api_base_url or PRODUCTION_API_BASE).rstrip("/"), config.token_url or PRODUCTION_TOKEN_URL
    return config.api_base_url.rstrip("/"), config.token_url


def qr_client_issues(config: SberQRConfig, *, for_order: bool = True) -> tuple[str, ...]:
    """Readiness only: no certificate parsing, token requests, or bank calls.

    Presence checks never mean that Sber has accepted a certificate, credentials,
    merchant identifiers, tariff, endpoint, or response-code mapping.
    """
    issues = list(config.configuration_status().issues)
    api_base, token_url = _endpoints(config)
    if config.environment not in {"sandbox", "production"}:
        issues.append("SBER_QR_ENV:invalid")
    if config.environment == "sandbox" and not config.endpoints_confirmed:
        issues.append("SBER_QR_ENDPOINTS_CONFIRMED:required")
    if not _valid_token_url(token_url):
        issues.append("SBER_QR_TOKEN_URL:unconfirmed_or_invalid")
    elif config.environment == "production" and token_url != PRODUCTION_TOKEN_URL:
        issues.append("SBER_QR_TOKEN_URL:production_profile_mismatch")
    elif config.environment == "sandbox" and "/prod/" in urlsplit(token_url).path:
        issues.append("SBER_QR_TOKEN_URL:production_forbidden_in_sandbox")
    if for_order:
        if not _valid_api_base(api_base):
            issues.append("SBER_QR_API_BASE_URL:unconfirmed_or_invalid")
        if config.environment == "sandbox" and _valid_api_base(api_base) and urlsplit(api_base).path.endswith("/prod"):
            issues.append("SBER_QR_API_BASE_URL:production_forbidden_in_sandbox")
        if config.environment == "production" and api_base != PRODUCTION_API_BASE:
            issues.append("SBER_QR_API_BASE_URL:production_profile_mismatch")
        for name, value, maximum in (
            ("SBER_QR_MEMBER_ID", config.member_id, 8),
            ("SBER_QR_ID_QR", config.id_qr, 36),
            ("SBER_QR_TID", config.tid, 8),
        ):
            if not isinstance(value, str) or not 1 <= len(value) <= maximum or not _IDENTIFIER.fullmatch(value):
                issues.append(name + ":missing_or_invalid")
        for name, value, maximum in (
            ("SBER_QR_SUCCESS_CODE", config.success_code, 6),
            ("SBER_QR_OPERATION_SUCCESS_CODE", config.operation_success_code, 2),
        ):
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_\-]{1," + str(maximum) + r"}", value):
                issues.append(name + ":unconfirmed_or_invalid")
    return tuple(dict.fromkeys(issues))


def _identifier(value, maximum):
    return isinstance(value, str) and 1 <= len(value) <= maximum and bool(_IDENTIFIER.fullmatch(value))


def _order_number(value):
    return isinstance(value, str) and 1 <= len(value) <= 36 and not any(c in value for c in "\r\n\t\x00")


def _amount(value, *, positive=True):
    return type(value) is int and (1 if positive else 0) <= value <= MAX_AMOUNT_KOPECKS


def _timestamp_value(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return False
    return True


def _json_object(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise ValueError("duplicate_json_key")
        data[key] = value
    return data


class QRClient:
    """One-shot requests; uncertain registration must be reconciled, not retried."""

    def __init__(self, config: SberQRConfig, *, ssl_context=None,
                 session_factory: Callable = aiohttp.ClientSession, clock: Callable | None = None):
        self.config = config
        self._ssl_context = ssl_context
        self._session_factory = session_factory
        self._clock = clock or (lambda: datetime.now(UTC))

    @classmethod
    def from_config(cls, config: SberQRConfig, **kwargs):
        return cls(config, **kwargs)

    def _ready(self, *, for_order, require_enabled=True):
        if for_order and require_enabled and not self.config.enabled:
            raise SberQRClientError("qr_payments_disabled")
        if qr_client_issues(self.config, for_order=for_order):
            raise SberQRClientError("qr_configuration_incomplete")

    def _tls(self):
        if self._ssl_context is None:
            try:
                self._ssl_context = build_sber_qr_ssl_context(self.config)
            except SberQRCertificateError:
                raise SberQRClientError("qr_certificate_setup_failed") from None
        if (not isinstance(self._ssl_context, ssl.SSLContext)
                or self._ssl_context.verify_mode != ssl.CERT_REQUIRED
                or not self._ssl_context.check_hostname):
            raise SberQRClientError("qr_verified_tls_required")
        return self._ssl_context

    def _timestamp(self):
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise SberQRClientError("qr_clock_invalid")
        return now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    async def _post(self, url, *, headers, auth=None, data=None, body=None):
        # No response, exception, token, certificate, or arbitrary server message
        # is logged here. Never follow a redirect carrying bank credentials.
        try:
            async with (
                self._session_factory(timeout=aiohttp.ClientTimeout(total=25, connect=10),
                                      trust_env=False) as session,
                session.post(url, headers=headers, auth=auth, data=data, json=body,
                             ssl=self._tls(), allow_redirects=False) as response,
            ):
                if 300 <= response.status < 400:
                    raise SberQRClientError("qr_redirect_refused", http_status=response.status)
                if response.status != 200:
                    raise SberQRClientError("qr_http_error", http_status=response.status)
                mime = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if mime != "application/json" and not (mime.startswith("application/") and mime.endswith("+json")):
                    raise SberQRClientError("qr_json_response_required")
                chunks, size = [], 0
                while True:
                    chunk = await response.content.read(min(65536, MAX_RESPONSE_BYTES + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise SberQRClientError("qr_response_too_large")
                parsed = json.loads(b"".join(chunks), object_pairs_hook=_json_object)
            if not isinstance(parsed, dict):
                raise SberQRClientError("qr_response_invalid")
            return parsed
        except SberQRClientError:
            raise
        except (aiohttp.ClientError, TimeoutError, OSError, ValueError, TypeError):
            raise SberQRClientError("qr_request_failed") from None

    async def _token(self, scope):
        self._ready(for_order=False)
        _, token_url = _endpoints(self.config)
        if ":" in self.config.client_id or any(c in self.config.client_id for c in "\r\n\x00"):
            raise SberQRClientError("qr_credentials_invalid") from None
        encoded = base64.b64encode((self.config.client_id + ":" + self.config.client_secret).encode("utf-8")).decode("ascii")
        data = await self._post(token_url, headers={"RqUID": uuid4().hex, "Accept": "application/json",
            "Authorization": "Basic " + encoded},
            data={"grant_type": "client_credentials", "scope": scope})
        token, token_type = data.get("access_token"), data.get("token_type")
        lifetime = data.get("expires_in")
        if isinstance(lifetime, str) and len(lifetime) <= 8 and lifetime.isascii() and lifetime.isdigit():
            lifetime = int(lifetime)
        if (not isinstance(token, str) or not 1 <= len(token) <= 8192
                or not token.isascii() or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in token)
                or not isinstance(token_type, str) or token_type.lower() != "bearer"
                or type(lifetime) is not int or lifetime <= 0
                or ("scope" in data and (not isinstance(data["scope"], str) or scope not in data["scope"].split()))):
            raise SberQRClientError("qr_oauth_response_invalid")
        return token

    async def check_connection(self):
        """OAuth-only diagnostic: no order, payment, transfer, or cash receipt."""
        await self._token(SCOPE_STATUS)
        return {"environment": self.config.environment, "authenticated": True, "scope": SCOPE_STATUS}

    async def _call(self, method, scope, fields):
        # Disabling new QR invoices must not strand a previously issued bill.
        self._ready(for_order=True, require_enabled=method == "creation")
        uid = uuid4().hex
        payload = {"rq_uid": uid, "rq_tm": self._timestamp(), **fields}
        token = await self._token(scope)
        api_base, _ = _endpoints(self.config)
        data = await self._post(api_base + "/qr/order/v3/" + method,
            headers={"RqUID": uid, "Accept": "application/json", "Authorization": "Bearer " + token},
            body=payload)
        if data.get("rq_uid") != uid:
            raise SberQRClientError("qr_response_correlation_mismatch")
        if not _timestamp_value(data.get("rq_tm")):
            raise SberQRClientError("qr_response_timestamp_invalid")
        if data.get("error_code") != self.config.success_code:
            raise SberQRClientError("qr_bank_rejected_request")
        return data

    async def create_order(self, order_number, amount_kopecks, description=""):
        if (not _order_number(order_number) or not _amount(amount_kopecks)
                or not isinstance(description, str) or len(description) > 256 or "\x00" in description):
            raise SberQRClientError("qr_order_input_invalid")
        fields = {"member_id": self.config.member_id, "order_number": order_number,
            "order_create_date": self._timestamp(), "id_qr": self.config.id_qr,
            "order_sum": amount_kopecks, "currency": "643", "description": description,
            "sbp_member_id": "100000000111"}
        data = await self._call("creation", SCOPE_CREATE, fields)
        if (data.get("order_number") != order_number or not _identifier(data.get("order_id"), 36)
                or not isinstance(data.get("order_state"), str) or data["order_state"] not in _STATES
                or not is_payment_url(data.get("order_form_url"))):
            raise SberQRClientError("qr_creation_response_invalid")
        # Even a PAID creation reply is not settlement evidence. Query status
        # with operation sums before confirming money, including in sandbox.
        return {"bank_order_id": data["order_id"], "payment_url": data["order_form_url"],
                "status": "PENDING", "test_mode": self.config.environment == "sandbox"}

    async def get_status(self, order_id, order_number, amount_kopecks):
        if ((order_id is not None and not _identifier(order_id, 36))
                or not _order_number(order_number) or not _amount(amount_kopecks)):
            raise SberQRClientError("qr_status_input_invalid")
        fields = {"tid": self.config.tid, "partner_order_number": order_number}
        if order_id is not None:
            fields["order_id"] = order_id
        data = await self._call("status", SCOPE_STATUS, fields)
        if (not _identifier(data.get("order_id"), 36)
                or (order_id is not None and data["order_id"] != order_id)
                or data.get("tid") != self.config.tid or data.get("id_qr") != self.config.id_qr):
            raise SberQRClientError("qr_order_identity_mismatch")
        state = data.get("order_state")
        if not isinstance(state, str) or state not in _STATES:
            raise SberQRClientError("qr_order_state_invalid")
        operations = data.get("order_operation_params", [])
        if not isinstance(operations, list) or len(operations) > 1000:
            raise SberQRClientError("qr_operations_invalid")
        totals, seen = {"PAY": 0, "REVERSE": 0, "REFUND": 0}, set()
        for op in operations:
            if (not isinstance(op, dict) or not _identifier(op.get("operation_id"), 50)
                    or op["operation_id"] in seen or not isinstance(op.get("operation_type"), str)
                    or op["operation_type"] not in totals
                    or not _timestamp_value(op.get("operation_date_time"))
                    or not _identifier(op.get("rrn"), 12) or not _identifier(op.get("auth_code"), 8)
                    or not _amount(op.get("operation_sum"), positive=False)
                    or op.get("operation_currency") != "643"
                    or not isinstance(op.get("response_code"), str)
                    or not re.fullmatch(r"[A-Za-z0-9_\-]{1,2}", op["response_code"])):
                raise SberQRClientError("qr_operation_evidence_invalid")
            seen.add(op["operation_id"])
            if op["response_code"] == self.config.operation_success_code:
                totals[op["operation_type"]] += op["operation_sum"]
        paid = totals["PAY"]
        refunded = totals["REVERSE"] + totals["REFUND"]
        if state in {"PAID", "REFUNDED", "REVERSED"}:
            if paid != amount_kopecks or not 0 <= refunded <= paid:
                raise SberQRClientError("qr_payment_amount_mismatch")
            if state in {"REFUNDED", "REVERSED"} and refunded <= 0:
                raise SberQRClientError("qr_refund_evidence_missing")
            net = paid - refunded
            status = "REFUNDED" if not net else "PARTIAL_REFUND" if refunded else "PAID"
            if self.config.environment == "sandbox" and net:
                status = "TEST_PAID"
        else:
            if paid or refunded:
                raise SberQRClientError("qr_state_evidence_mismatch")
            net = 0
            status = {"CREATED": "PENDING", "ON_PAYMENT": "PENDING", "REVOKED": "CANCELLED",
                      "DECLINED": "DECLINED", "EXPIRED": "CANCELLED"}[state]
        return {"bank_order_id": data["order_id"], "order_number": order_number, "status": status,
                "currency": "643", "amount_kopecks": amount_kopecks, "paid_kopecks": paid,
                "refunded_kopecks": refunded, "test_mode": self.config.environment == "sandbox",
                "settlement_verified": state in {"PAID", "REFUNDED", "REVERSED"}}
