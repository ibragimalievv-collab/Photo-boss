"""QR.API contract and failure boundaries using only synthetic replies."""

import base64
import json
import ssl
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from app.sber_qr_client import (
    MAX_RESPONSE_BYTES,
    PRODUCTION_API_BASE,
    PRODUCTION_TOKEN_URL,
    QRClient,
    SberQRClientError,
    is_payment_url,
    qr_client_issues,
)
from app.sber_qr_config import SberQRConfig


class Content:
    def __init__(self, data):
        self.data = data

    async def read(self, limit):
        result, self.data = self.data[:limit], self.data[limit:]
        return result


class Response:
    def __init__(self, value, *, status=200, mime="application/json"):
        self.status = status
        self.headers = {"Content-Type": mime}
        self.content = Content(value if isinstance(value, bytes) else json.dumps(value).encode())

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class Session:
    def __init__(self, factory):
        self.factory = factory

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def post(self, url, **kwargs):
        self.factory.calls.append((url, kwargs))
        result = self.factory.results.pop(0)
        if callable(result):
            result = result(kwargs)
        if isinstance(result, Response):
            return result
        return Response(result)


class Factory:
    def __init__(self, results):
        self.results, self.calls, self.session_options = list(results), [], []

    def __call__(self, **kwargs):
        self.session_options.append(kwargs)
        return Session(self)


def token(**extra):
    return {"access_token": "synthetic-only-token", "token_type": "Bearer", "expires_in": "60", **extra}


def created(request, **extra):
    body = request["json"]
    return {"rq_uid": body["rq_uid"], "rq_tm": body["rq_tm"], "error_code": "0",
            "order_number": body["order_number"], "order_id": "bank-order-1",
            "order_state": "CREATED", "order_form_url": "https://qr.nspk.ru/synthetic-order", **extra}


def operation(kind="PAY", amount=12345, **extra):
    return {"operation_id": "synthetic-" + kind, "operation_type": kind,
            "operation_sum": amount, "operation_currency": "643", "response_code": "0",
            "operation_date_time": "2026-10-28T12:00:00Z", "rrn": "123456123456", "auth_code": "12345678", **extra}


def status(request, **extra):
    return {"rq_uid": request["json"]["rq_uid"], "rq_tm": request["json"]["rq_tm"],
            "error_code": "0", "order_id": "bank-order-1", "tid": "12345678", "id_qr": "qr-device-1",
            "order_state": "PAID", "order_operation_params": [operation()], **extra}


class QRClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.certificate = Path(self.directory.name) / "synthetic-certificate.b64"
        self.certificate.write_text("fixture-readiness-only")
        self.config = SberQRConfig(enabled=True, environment="sandbox", client_id="synthetic-client",
            client_secret="synthetic-secret-never-log", certificate_password="synthetic-password",
            certificate_file=str(self.certificate), member_id="000001", id_qr="qr-device-1", tid="12345678",
            # Explicit test fixtures, NOT claimed to be bank-confirmed sandbox URLs.
            api_base_url="https://dev.api.sberbank.ru/sandbox",
            token_url="https://dev.api.sberbank.ru/sandbox/tokens/v3/oauth", endpoints_confirmed=True,
            success_code="0", operation_success_code="0")

    def client(self, results, *, config=None, context=None):
        factory = Factory(results)
        client = QRClient.from_config(config or self.config, ssl_context=context or ssl.create_default_context(),
            session_factory=factory, clock=lambda: datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC))
        return client, factory

    async def test_diagnostic_is_oauth_only_and_works_while_new_payments_disabled(self):
        config = replace(self.config, enabled=False, member_id="", id_qr="", tid="", api_base_url="",
                         success_code="", operation_success_code="")
        client, factory = self.client([token()], config=config)
        result = await client.check_connection()
        self.assertTrue(result["authenticated"])
        self.assertNotIn("access_token", result)
        self.assertEqual(len(factory.calls), 1)
        url, call = factory.calls[0]
        self.assertEqual(url, config.token_url)
        self.assertEqual(call["data"], {"grant_type": "client_credentials", "scope": result["scope"]})
        self.assertEqual(base64.b64decode(call["headers"]["Authorization"][6:]).decode(),
                         config.client_id + ":" + config.client_secret)
        self.assertFalse(call["allow_redirects"])
        self.assertEqual(len(call["headers"]["RqUID"]), 32)
        self.assertFalse(factory.session_options[0]["trust_env"])
        self.assertEqual(factory.session_options[0]["timeout"].total, 25)

    async def test_unknown_sandbox_endpoint_and_merchant_identifiers_fail_before_network(self):
        for changes in ({"endpoints_confirmed": False}, {"api_base_url": ""}, {"token_url": ""},
                        {"member_id": ""}, {"id_qr": ""}, {"tid": ""},
                        {"success_code": ""}, {"operation_success_code": ""}):
            with self.subTest(changes=tuple(changes)):
                client, factory = self.client([], config=replace(self.config, **changes))
                with self.assertRaises(SberQRClientError) as caught:
                    await client.create_order("pb-synthetic-1", 12345)
                self.assertEqual(caught.exception.code, "qr_configuration_incomplete")
                self.assertFalse(factory.calls)

    async def test_sandbox_credentials_cannot_be_sent_to_any_production_profile(self):
        for token_url in (PRODUCTION_TOKEN_URL, "https://mc.api.sberbank.ru/ru/prod/tokens/v2/oauth",
                          "https://dev.api.sberbank.ru/prod/tokens/v3/oauth"):
            with self.subTest(token_url=token_url):
                client, factory = self.client([], config=replace(self.config, token_url=token_url))
                with self.assertRaises(SberQRClientError) as caught:
                    await client.check_connection()
                self.assertEqual(caught.exception.code, "qr_configuration_incomplete")
                self.assertFalse(factory.calls)
        for api_base in (PRODUCTION_API_BASE, "https://mc.api.sberbank.ru/ru/prod",
                         "https://dev.api.sberbank.ru/prod"):
            with self.subTest(api_base=api_base):
                client, factory = self.client([], config=replace(self.config, api_base_url=api_base))
                with self.assertRaises(SberQRClientError):
                    await client.create_order("pb-synthetic-1", 12345)
                self.assertFalse(factory.calls)

    async def test_create_exact_contract_and_even_paid_creation_does_not_settle(self):
        client, factory = self.client([token(), lambda r: created(r, order_state="PAID")])
        result = await client.create_order("pb-synthetic-1", 12345, "Synthetic deposit")
        request = factory.calls[1][1]
        body = request["json"]
        self.assertEqual(set(body), {"rq_uid", "rq_tm", "member_id", "order_number", "order_create_date",
                                     "id_qr", "order_sum", "currency", "description", "sbp_member_id"})
        self.assertEqual(body["order_number"], "pb-synthetic-1")
        self.assertEqual(body["order_sum"], 12345)
        self.assertEqual(body["currency"], "643")
        self.assertEqual(body["rq_tm"], "2026-10-28T12:00:00Z")
        self.assertEqual(body["rq_uid"], request["headers"]["RqUID"])
        self.assertEqual(body["sbp_member_id"], "100000000111")
        self.assertTrue(factory.calls[1][0].endswith("/qr/order/v3/creation"))
        self.assertEqual(result["status"], "PENDING")
        self.assertTrue(result["test_mode"])
        self.assertNotIn("paid_kopecks", result)

    async def test_creation_identity_or_url_mismatch_never_returns_a_payable_bill(self):
        for changes in ({"order_number": "other-order"}, {"order_id": []}, {"order_state": []},
                        {"order_form_url": "https://evil.example/qr"}, {"rq_uid": "0" * 32}):
            with self.subTest(changes=tuple(changes)):
                client, _ = self.client([token(), lambda r, changes=changes: created(r, **changes)])
                with self.assertRaises(SberQRClientError):
                    await client.create_order("pb-synthetic-1", 12345)

    async def test_paid_sandbox_is_test_paid_with_verified_gross_and_no_payer_data(self):
        client, _ = self.client([token(), lambda r: status(r, client_name="Synthetic private payer")])
        result = await client.get_status("bank-order-1", "pb-synthetic-1", 12345)
        self.assertEqual(result["status"], "TEST_PAID")
        self.assertEqual(result["paid_kopecks"], 12345)
        self.assertEqual(result["refunded_kopecks"], 0)
        self.assertTrue(result["settlement_verified"])
        self.assertTrue(result["test_mode"])
        self.assertNotIn("client_name", result)

    async def test_paid_production_uses_only_the_fixed_production_profile(self):
        config = replace(self.config, environment="production", api_base_url="", token_url="")
        client, factory = self.client([token(), status], config=config)
        result = await client.get_status("bank-order-1", "pb-synthetic-1", 12345)
        self.assertEqual(result["status"], "PAID")
        self.assertFalse(result["test_mode"])
        self.assertEqual(factory.calls[0][0], PRODUCTION_TOKEN_URL)
        self.assertEqual(factory.calls[1][0], PRODUCTION_API_BASE + "/qr/order/v3/status")

    async def test_pending_reports_expectations_without_settlement_evidence(self):
        client, _ = self.client([token(), lambda r: status(r, order_state="CREATED", order_operation_params=[])])
        result = await client.get_status("bank-order-1", "pb-synthetic-1", 12345)
        self.assertEqual(result["status"], "PENDING")
        self.assertFalse(result["settlement_verified"])
        self.assertEqual(result["paid_kopecks"], 0)

    async def test_uncertain_creation_can_be_looked_up_without_reregistering(self):
        client, factory = self.client([token(), status])
        result = await client.get_status(None, "pb-synthetic-1", 12345)
        self.assertEqual(result["bank_order_id"], "bank-order-1")
        request = factory.calls[1][1]["json"]
        self.assertNotIn("order_id", request)
        self.assertEqual(request["partner_order_number"], "pb-synthetic-1")
        self.assertEqual(request["tid"], self.config.tid)
        self.assertEqual(len(factory.calls), 2)

    async def test_disabling_new_orders_still_allows_reconciliation(self):
        client, factory = self.client([token(), status], config=replace(self.config, enabled=False))
        with self.assertRaises(SberQRClientError) as caught:
            await client.create_order("pb-synthetic-1", 12345)
        self.assertEqual(caught.exception.code, "qr_payments_disabled")
        self.assertFalse(factory.calls)
        self.assertEqual((await client.get_status("bank-order-1", "pb-synthetic-1", 12345))["status"], "TEST_PAID")

    async def test_payment_identity_amount_currency_and_success_evidence_are_required(self):
        for changes in ({"order_id": "another-order"}, {"tid": "87654321"}, {"id_qr": "other-device"},
                        {"rq_uid": "f" * 32}, {"order_operation_params": []},
                        {"order_operation_params": [operation(amount=12344)]},
                        {"order_operation_params": [operation(operation_currency="840")]},
                        {"order_operation_params": [operation(response_code="99")]},
                        {"order_operation_params": [operation(amount=True)]},
                        {"order_operation_params": [operation(operation_type=[])]},
                        {"order_operation_params": [operation(operation_date_time="2026-02-31T12:00:00Z")]},
                        {"order_operation_params": [operation(rrn=None)]},
                        {"order_operation_params": [operation(auth_code="")]},
                        {"order_operation_params": [operation(), operation()]},
                        {"order_state": []}, {"order_state": "UNKNOWN"}, {"order_operation_params": None}):
            with self.subTest(changes=tuple(changes)):
                client, _ = self.client([token(), lambda r, changes=changes: status(r, **changes)])
                with self.assertRaises(SberQRClientError):
                    await client.get_status("bank-order-1", "pb-synthetic-1", 12345)

    async def test_refunds_require_operations_and_keep_original_gross(self):
        config = replace(self.config, environment="production", api_base_url="", token_url="")
        for refund, expected in ((1000, "PARTIAL_REFUND"), (12345, "REFUNDED")):
            with self.subTest(refund=refund):
                client, _ = self.client([token(), lambda r, refund=refund: status(r, order_state="REFUNDED",
                    order_operation_params=[operation(), operation("REFUND", refund)])], config=config)
                result = await client.get_status("bank-order-1", "pb-synthetic-1", 12345)
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["paid_kopecks"], 12345)
                self.assertEqual(result["refunded_kopecks"], refund)
                self.assertTrue(result["settlement_verified"])
        for ops in ([operation()], [operation(), operation("REFUND", 12346)]):
            client, _ = self.client([token(), lambda r, ops=ops: status(r, order_state="REFUNDED", order_operation_params=ops)])
            with self.assertRaises(SberQRClientError):
                await client.get_status("bank-order-1", "pb-synthetic-1", 12345)

    async def test_schema_required_operation_fields_cannot_be_omitted(self):
        # Sber's current status-response schema lists all eight fields in
        # order_operation_params.items.required, rather than only its example.
        for field in ("operation_id", "operation_date_time", "rrn", "operation_type",
                      "operation_sum", "operation_currency", "auth_code", "response_code"):
            with self.subTest(field=field):
                evidence = operation()
                del evidence[field]
                client, _ = self.client([token(), lambda r, evidence=evidence: status(r, order_operation_params=[evidence])])
                with self.assertRaises(SberQRClientError) as caught:
                    await client.get_status("bank-order-1", "pb-synthetic-1", 12345)
                self.assertEqual(caught.exception.code, "qr_operation_evidence_invalid")

    async def test_failure_states_never_confirm_money(self):
        for state, expected in (("ON_PAYMENT", "PENDING"), ("DECLINED", "DECLINED"),
                                ("EXPIRED", "CANCELLED"), ("REVOKED", "CANCELLED")):
            with self.subTest(state=state):
                client, _ = self.client([token(), lambda r, state=state: status(r, order_state=state, order_operation_params=[])])
                result = await client.get_status("bank-order-1", "pb-synthetic-1", 12345)
                self.assertEqual(result["status"], expected)
                self.assertFalse(result["settlement_verified"])
        client, _ = self.client([token(), lambda r: status(r, order_state="DECLINED")])
        with self.assertRaises(SberQRClientError):
            await client.get_status("bank-order-1", "pb-synthetic-1", 12345)

    async def test_invalid_oauth_cannot_progress_to_an_order(self):
        for changes in ({"token_type": "Basic"}, {"expires_in": True}, {"expires_in": 0}, {"expires_in": "9" * 5000},
                        {"scope": "another-scope"}, {"access_token": "private\r\nheader"},
                        {"access_token": "private token"}):
            with self.subTest(changes=tuple(changes)):
                client, factory = self.client([token(**changes)])
                with self.assertRaises(SberQRClientError) as caught:
                    await client.create_order("pb-synthetic-1", 12345)
                self.assertEqual(caught.exception.code, "qr_oauth_response_invalid")
                self.assertEqual(len(factory.calls), 1)

    async def test_oauth_rejects_every_ascii_control_and_del_before_authorization_header(self):
        for codepoint in (*range(32), 127):
            with self.subTest(codepoint=codepoint):
                client, factory = self.client([token(access_token="synthetic" + chr(codepoint) + "token")])
                with self.assertRaises(SberQRClientError) as caught:
                    await client.create_order("pb-synthetic-1", 12345)
                self.assertEqual(caught.exception.code, "qr_oauth_response_invalid")
                self.assertEqual(len(factory.calls), 1)

    async def test_http_redirect_oversize_invalid_json_and_provider_errors_are_safe(self):
        for reply in (Response({}, status=302), Response({}, status=503), Response({}, mime="text/html"),
                      Response(b"x" * (MAX_RESPONSE_BYTES + 1)), Response(b"{bad-json"),
                      Response(b'{"access_token":"secret-one","access_token":"secret-two"}'),
                      {"error": "private-provider-message", "error_description": self.config.client_secret}):
            with self.subTest(reply=type(reply).__name__):
                client, factory = self.client([reply])
                with self.assertRaises(SberQRClientError) as caught:
                    await client.check_connection()
                self.assertNotIn(self.config.client_secret, str(caught.exception))
                self.assertNotIn("private-provider", str(caught.exception))
                self.assertEqual(len(factory.calls), 1, "Never retry an uncertain request automatically")

    async def test_unverified_ssl_is_never_used(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        client, factory = self.client([token()], context=context)
        with self.assertRaises(SberQRClientError) as caught:
            await client.check_connection()
        self.assertEqual(caught.exception.code, "qr_verified_tls_required")
        self.assertFalse(factory.calls)

    async def test_streaming_json_is_read_to_eof_without_unbounded_reads(self):
        class SmallChunks(Content):
            async def read(self, limit):
                return await super().read(min(limit, 7))
        response = Response(token())
        response.content = SmallChunks(json.dumps(token()).encode())
        client, _ = self.client([response])
        self.assertTrue((await client.check_connection())["authenticated"])

    async def test_invalid_request_inputs_do_not_make_network_requests(self):
        for number, amount in (("", 100), ("x" * 37, 100), ("line\nbreak", 100), ("ok", True), ("ok", 0), ("ok", 1.2)):
            client, factory = self.client([])
            with self.assertRaises(SberQRClientError):
                await client.create_order(number, amount)
            self.assertFalse(factory.calls)


class QRPolicyTests(unittest.TestCase):
    def test_payment_payload_hosts_and_tls_fail_closed(self):
        self.assertTrue(is_payment_url("https://qr.nspk.ru/synthetic?sum=100"))
        self.assertTrue(is_payment_url("https://qr.sberbank.ru/synthetic"))
        for value in (None, [], {}, True, "http://qr.nspk.ru/synthetic", "https://qr.nspk.ru.evil.example/test",
                      "https://user:secret@qr.nspk.ru/test", "https://qr.nspk.ru:444/test",
                      "https://qr.nspk.ru:invalid/test", "https://qr.nspk.ru/test#other", "https://qr.nspk.ru/",
                      "https://qr.nspk.ru/test\x7f"):
            with self.subTest(value=type(value).__name__):
                self.assertFalse(is_payment_url(value))

    def test_no_sandbox_fallback_or_cross_environment_profile(self):
        base = SberQRConfig(client_id="fixture", client_secret="fixture", certificate_password="fixture",
                            certificate_file="missing")
        self.assertIn("SBER_QR_TOKEN_URL:unconfirmed_or_invalid", qr_client_issues(base, for_order=False))
        self.assertIn("SBER_QR_ENDPOINTS_CONFIRMED:required", qr_client_issues(base, for_order=False))
        wrong_sandbox = replace(base, api_base_url=PRODUCTION_API_BASE, token_url=PRODUCTION_TOKEN_URL,
                                endpoints_confirmed=True)
        self.assertIn("SBER_QR_API_BASE_URL:production_forbidden_in_sandbox", qr_client_issues(wrong_sandbox))
        self.assertIn("SBER_QR_TOKEN_URL:production_forbidden_in_sandbox", qr_client_issues(wrong_sandbox, for_order=False))
        wrong_production = replace(base, environment="production", api_base_url="https://dev.api.sberbank.ru/sandbox")
        self.assertIn("SBER_QR_API_BASE_URL:production_profile_mismatch", qr_client_issues(wrong_production))

    def test_config_reads_metadata_without_inventing_values_or_echoing_secrets(self):
        config = SberQRConfig.from_env({"SBER_QR_MEMBER_ID": "000001", "SBER_QR_ID_QR": "id-1",
            "SBER_QR_TID": "12345678", "SBER_QR_CLIENT_SECRET": "synthetic-private",
            "SBER_QR_ENDPOINTS_CONFIRMED": "1", "SBER_QR_SUCCESS_CODE": "0",
            "SBER_QR_OPERATION_SUCCESS_CODE": "0"})
        self.assertEqual(config.member_id, "000001")
        self.assertEqual(config.tid, "12345678")
        self.assertTrue(config.endpoints_confirmed)
        self.assertEqual(config.api_base_url, "")
        self.assertNotIn("synthetic-private", repr(config))
        with self.assertRaises(ValueError):
            SberQRConfig.from_env({"SBER_QR_ENDPOINTS_CONFIRMED": "yes"})


if __name__ == "__main__":
    unittest.main()
