"""Provider dispatch and QR accounting use synthetic fixtures, never bank I/O."""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import delete, func, select
from test_team import Request
from test_workflow_services import booking_data, fixture

from app.bank_payments import (
    BankPayments,
    payment_loop,
    payment_settings,
    public_bill,
    reconcile,
)
from app.miniapp_security import AccessError
from app.models import (
    BankPayment,
    HotelTariff,
    PayrollEntry,
    Receipt,
    Sale,
    SaleDraft,
    SaleDraftPhoto,
    User,
    UserRole,
)
from app.sber_qr_certificates import SberQRCertificateError
from app.sber_qr_client import SberQRClientError
from app.services.bookings import create_booking_record
from app.services.sale_workflow import complete_sale, set_sale_counts, start_sale_draft
from app.workflow import Workflow

ACTOR = {'id': 1, 'roles': ['MANAGER']}
OWNER = {'id': 1, 'roles': ['OWNER']}


@pytest.fixture(autouse=True)
def synthetic_environment(monkeypatch):
    # Read names only; inherited host credentials must not influence these tests.
    for key in tuple(os.environ):
        if key.startswith('SBER_'):
            monkeypatch.delenv(key)


@pytest.fixture
def qr_environment(monkeypatch, tmp_path):
    certificate = tmp_path / 'synthetic-not-a-real-certificate.p12.b64'
    certificate.write_bytes(b'synthetic metadata fixture; TLS loader is separately tested')
    for key, value in {
        'SBER_QR_ENABLED': '1', 'SBER_QR_ENV': 'sandbox',
        'SBER_QR_CLIENT_ID': 'synthetic-client-id',
        'SBER_QR_CLIENT_SECRET': 'synthetic-client-secret',
        'SBER_QR_CERT_PASSWORD': 'synthetic-certificate-password',
        'SBER_QR_CERT_FILE': str(certificate), 'SBER_QR_CERT_FORMAT': 'p12-base64',
        'SBER_QR_MEMBER_ID': 'MTEST001', 'SBER_QR_ID_QR': 'synthetic-qr-id',
        'SBER_QR_TID': 'TTEST001', 'SBER_QR_ENDPOINTS_CONFIRMED': '1',
        'SBER_QR_API_BASE_URL': 'https://dev.api.sberbank.ru/sandbox',
        'SBER_QR_TOKEN_URL': 'https://dev.api.sberbank.ru/sandbox/tokens/v3/oauth',
        'SBER_QR_SUCCESS_CODE': '0', 'SBER_QR_OPERATION_SUCCESS_CODE': '00',
    }.items():
        monkeypatch.setenv(key, value)
    return certificate


def production_environment(monkeypatch):
    monkeypatch.setenv('SBER_QR_ENV', 'production')
    monkeypatch.setenv('SBER_QR_API_BASE_URL', 'https://mc.api.sberbank.ru/prod')
    monkeypatch.setenv('SBER_QR_TOKEN_URL', 'https://mc.api.sberbank.ru/prod/tokens/v3/oauth')


def normalized(order_id, order_number, amount, *, test_mode=False, status=None, refunded=0):
    return {'bank_order_id': order_id or 'synthetic-recovered-order', 'order_number': order_number,
        'status': status or ('TEST_PAID' if test_mode else 'PAID'), 'currency': '643',
        'amount_kopecks': amount, 'paid_kopecks': amount, 'refunded_kopecks': refunded,
        'test_mode': test_mode, 'settlement_verified': True}


def mock_qr_client(monkeypatch, *, test_mode=True):
    created = []
    async def create(order_number, amount, description):
        created.append(order_number)
        return {'bank_order_id': f'synthetic-bank-order-{len(created)}',
            'payment_url': f'https://qr.nspk.ru/synthetic-order-{len(created)}',
            'status': 'PENDING', 'test_mode': test_mode}
    client = SimpleNamespace(create_order=AsyncMock(side_effect=create),
        get_status=AsyncMock(side_effect=lambda order_id, order_number, amount:
            normalized(order_id, order_number, amount, test_mode=test_mode)),
        check_connection=AsyncMock(return_value={'authenticated': True,
            'environment': 'sandbox' if test_mode else 'production',
            'access_token': 'synthetic-token-must-not-leave-diagnostic'}))
    factory = Mock(return_value=client)
    monkeypatch.setattr('app.bank_payments.QRClient.from_config', factory)
    return client, factory


async def make_service(*, deposit='0', owner=False):
    engine, sessions = await fixture()
    async with sessions() as session:
        actor = await session.get(User, 1)
        if owner:
            session.add(UserRole(user_id=1, role='OWNER'))
        booking = await create_booking_record(session, actor, booking_data() | {'deposit': deposit})
        booking.status, booking.photographer_id = 'READY_FOR_SALE', 2
        await session.commit()
        booking_id = booking.id
    api = SimpleNamespace(engine=engine, body=AsyncMock(side_effect=lambda request: request.body),
                          today=lambda: date(2026, 9, 22))
    return engine, sessions, BankPayments(api), booking_id


def invoice(booking_id, *, purpose='PURCHASE', key='synthetic-invoice-request-0001'):
    return {'bookingId': booking_id, 'total': 10 if purpose == 'PURCHASE' else 0,
        'sold': 2 if purpose == 'PURCHASE' else 0, 'tariffId': None, 'amount': None,
        'purpose': purpose, 'requestKey': key}


async def sale_draft(sessions, bid):
    async with sessions() as session:
        actor = await session.get(User, 1)
        draft = await start_sale_draft(session, actor, {'MANAGER'}, bid)
        await session.commit()
        return draft.id


def test_bank_sale_freezes_package_before_payment_and_retains_price_after_tariff_change(monkeypatch, qr_environment):
    production_environment(monkeypatch)
    client, _ = mock_qr_client(monkeypatch, test_mode=False)
    async def run():
        engine, sessions, service, bid = await make_service()
        try:
            did = await sale_draft(sessions, bid)
            async with sessions() as session:
                tariff = HotelTariff(hotel_id=1, name='Synthetic package', kind='PACKAGE', price=1000,
                                     photo_count=2, active=True)
                session.add(tariff)
                await session.commit()
                tid = tariff.id
            body = invoice(bid) | {'sourceDraftId': did, 'tariffId': tid, 'allowZeroDue': True}
            created = json.loads((await service.create(Request(ACTOR, body))).text)
            assert created['amount'] == '1000.00'
            async with sessions() as session:
                draft = await session.get(SaleDraft, did)
                frozen = json.loads(draft.pricing_snapshot)
                assert frozen['_bankQuote'] is True and frozen['amount'] == '1000.00'
                assert draft.sold_photos is None and draft.declared_photo_count is None
                tariff = await session.get(HotelTariff, tid)
                tariff.price, tariff.active = 2000, False
                await session.commit()
            await service.status(Request(ACTOR, cid=created['id']))
            async with sessions() as session:
                actor, draft = await session.get(User, 1), await session.get(SaleDraft, did)
                assert await set_sale_counts(session, actor, draft, 10, 2, tariff_id=tid) == 1000
                assert draft.status == 'AWAITING_SELECTED' and draft.expected_amount is None
                with pytest.raises(ValueError, match='банковском счёте'):
                    await set_sale_counts(session, actor, draft, 11, 2, tariff_id=tid)
                with pytest.raises(ValueError, match='Скидка уже закреплена'):
                    await set_sale_counts(session, actor, draft, 10, 2, tariff_id=tid, discount_id=1)
                await session.commit()
            assert client.create_order.await_count == 1
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_zero_due_is_explicit_locked_ledger_proof_without_bank_order(monkeypatch, qr_environment):
    client, factory = mock_qr_client(monkeypatch)
    # Ledger proof needs no bank access, including when the bank is unavailable.
    monkeypatch.setenv('SBER_QR_ENABLED', '0')
    async def run():
        engine, sessions, service, bid = await make_service()
        try:
            did = await sale_draft(sessions, bid)
            async with sessions() as session:
                session.add(Receipt(booking_id=bid, uploaded_by_id=1, purpose='DEPOSIT',
                    file_id='', file_unique_id='synthetic-prior-deposit', status='APPROVED',
                    expected_amount=800, verified_amount=800))
                await session.commit()
            body = invoice(bid) | {'sourceDraftId': did, 'allowZeroDue': True}
            for _ in range(2):
                proof = json.loads((await service.create(Request(ACTOR, body))).text)
                assert proof == {'allowZeroDue': True, 'status': 'NO_PAYMENT_REQUIRED',
                    'bookingId': bid, 'sourceDraftId': did, 'amount': '0.00', 'testMode': False}
            async with sessions() as session:
                assert await session.scalar(select(func.count(BankPayment.id))) == 0
                actor, draft = await session.get(User, 1), await session.get(SaleDraft, did)
                assert json.loads(draft.pricing_snapshot)['_bankQuote'] is True
                assert await set_sale_counts(session, actor, draft, 10, 2) == 800
                assert draft.expected_amount is None
            assert factory.call_count == client.create_order.await_count == 0
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize('test_mode', [False, True])
def test_sale_gate_requires_real_full_settlement_before_counts(monkeypatch, qr_environment, test_mode):
    if not test_mode:
        production_environment(monkeypatch)
    client, _ = mock_qr_client(monkeypatch, test_mode=test_mode)
    async def run():
        engine, sessions, service, bid = await make_service()
        try:
            did = await sale_draft(sessions, bid)
            body = invoice(bid) | {'sourceDraftId': did, 'allowZeroDue': True}
            created = json.loads((await service.create(Request(ACTOR, body))).text)
            async with sessions() as session:
                with pytest.raises(ValueError, match='подтверждения оплаты'):
                    await set_sale_counts(session, await session.get(User, 1), await session.get(SaleDraft, did), 10, 2)
            checked = json.loads((await service.status(Request(ACTOR, cid=created['id']))).text)
            assert checked['status'] == ('TEST_PAID' if test_mode else 'PAID')
            async with sessions() as session:
                actor, draft = await session.get(User, 1), await session.get(SaleDraft, did)
                if test_mode:
                    with pytest.raises(ValueError, match='подтверждения оплаты'):
                        await set_sale_counts(session, actor, draft, 10, 2)
                    assert await session.scalar(select(func.count(Receipt.id))) == 0
                else:
                    assert await set_sale_counts(session, actor, draft, 10, 2) == 800
            assert client.create_order.await_count == 1
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_bank_quote_cannot_mutate_another_employees_draft(monkeypatch, qr_environment):
    client, _ = mock_qr_client(monkeypatch)
    async def run():
        engine, sessions, service, bid = await make_service()
        try:
            did = await sale_draft(sessions, bid)
            async with sessions() as session:
                draft = await session.get(SaleDraft, did)
                draft.created_by_id = 2
                await session.commit()
            with pytest.raises(AccessError) as error:
                await service.create(Request(ACTOR, invoice(bid) | {'sourceDraftId': did, 'allowZeroDue': True}))
            assert error.value.status == 400
            async with sessions() as session:
                assert (await session.get(SaleDraft, did)).pricing_snapshot is None
                assert await session.scalar(select(func.count(BankPayment.id))) == 0
            assert client.create_order.await_count == 0
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize('refunded', [20000, 80000])
@pytest.mark.parametrize('manual_receipt', [False, True])
def test_refund_after_accepted_counts_cannot_complete_bank_sale_without_replacement_receipt(
        monkeypatch, qr_environment, refunded, manual_receipt):
    production_environment(monkeypatch)
    client, _ = mock_qr_client(monkeypatch, test_mode=False)
    async def run():
        engine, sessions, service, bid = await make_service()
        try:
            did = await sale_draft(sessions, bid)
            body = invoice(bid) | {'sourceDraftId': did, 'allowZeroDue': True}
            created = json.loads((await service.create(Request(ACTOR, body))).text)
            await service.status(Request(ACTOR, cid=created['id']))
            async with sessions() as session:
                actor, draft = await session.get(User, 1), await session.get(SaleDraft, did)
                await set_sale_counts(session, actor, draft, 10, 2)
                for number in range(2):
                    session.add(SaleDraftPhoto(draft_id=did, telegram_file_id=str(number),
                        telegram_unique_id=f'synthetic-bank-refund-photo-{number}',
                        storage_path=f'app:/synthetic-bank-refund/{number}.jpg',
                        sha256=f'synthetic-bank-refund-hash-{number}', byte_size=100))
                await session.commit()
            client.get_status.side_effect = lambda order_id, order_number, amount: normalized(
                order_id, order_number, amount, refunded=refunded,
                status='REFUNDED' if refunded == amount else 'PARTIAL_REFUND')
            await service.status(Request(ACTOR, cid=created['id']))
            async with sessions() as session:
                actor, draft = await session.get(User, 1), await session.get(SaleDraft, did)
                if manual_receipt:
                    draft.receipt_file_id = 'synthetic-replacement-receipt'
                    draft.receipt_file_unique_id = 'synthetic-replacement-receipt-unique'
                    draft.receipt_analysis = '{}'
                    await session.commit()
                    result = await complete_sale(session, actor, did)
                    assert result.receipt.status == 'PENDING'
                    assert result.receipt.expected_amount == refunded / 100
                    assert await session.scalar(select(func.count(Sale.id))) == 1
                    assert await session.scalar(select(func.count(PayrollEntry.id))) == 1
                else:
                    with pytest.raises(ValueError, match='больше не покрывает покупку'):
                        await complete_sale(session, actor, did)
                    assert draft.status == 'AWAITING_SELECTED'
                    assert await session.scalar(select(func.count(SaleDraftPhoto.id))) == 2
                    assert await session.scalar(select(func.count(Sale.id))) == 0
                    assert await session.scalar(select(func.count(PayrollEntry.id))) == 0
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_qr_creation_tags_provider_returns_real_payload_qr_and_replays_after_disable(monkeypatch, qr_environment):
    client, _ = mock_qr_client(monkeypatch)
    acquiring = AsyncMock(side_effect=AssertionError('QR bills must not call acquiring'))
    monkeypatch.setattr('app.bank_payments.bank_call', acquiring)
    async def run():
        engine, sessions, service, bid = await make_service(deposit='300')
        try:
            body = invoice(bid, purpose='DEPOSIT')
            result = json.loads((await service.create(Request(ACTOR, body))).text)
            assert result['status'] == 'PENDING' and result['provider'] == 'sber_qr'
            assert result['testMode'] is True and result['amount'] == '300.00'
            assert client.create_order.await_args.args[1] == 30000
            async with sessions() as session:
                bill = await session.get(BankPayment, result['id'])
                metadata = json.loads(bill.pricing_snapshot)['_bank']
                assert metadata['provider'] == 'sber_qr' and metadata['environment'] == 'sandbox'
                assert len(metadata['binding']) == 64
                assert await session.scalar(select(func.count(Receipt.id))) == 0
            png = await service.qr(Request(ACTOR, cid=result['id']))
            assert png.content_type == 'image/png' and png.body.startswith(b'\x89PNG')
            monkeypatch.setenv('SBER_QR_ENABLED', '0')
            again = json.loads((await service.create(Request(ACTOR, body))).text)
            assert again == result
            assert client.create_order.await_count == 1 and acquiring.await_count == 0
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_incomplete_explicit_qr_never_falls_back_to_acquiring(monkeypatch, qr_environment):
    monkeypatch.delenv('SBER_QR_TID')
    monkeypatch.setattr('app.bank_payments.configured', lambda: True)
    acquiring = AsyncMock()
    monkeypatch.setattr('app.bank_payments.bank_call', acquiring)
    assert payment_settings()['bankConfigured'] is False
    assert payment_settings()['bankProvider'] == 'sber_qr'
    async def run():
        engine, sessions, service, bid = await make_service()
        try:
            with pytest.raises(AccessError) as error:
                await service.create(Request(ACTOR, invoice(bid)))
            assert error.value.status == 503 and acquiring.await_count == 0
            async with sessions() as session:
                assert await session.scalar(select(func.count(BankPayment.id))) == 0
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_uncertain_registration_recovers_status_without_registering_again(monkeypatch, qr_environment):
    client, _ = mock_qr_client(monkeypatch)
    client.create_order.side_effect = SberQRClientError('qr_request_failed')
    async def run():
        engine, sessions, service, bid = await make_service()
        try:
            body = invoice(bid)
            with pytest.raises(AccessError) as error:
                await service.create(Request(ACTOR, body))
            assert error.value.status == 503
            replay = json.loads((await service.create(Request(ACTOR, body))).text)
            assert replay['status'] == 'CREATING' and replay['url'] is None
            monkeypatch.setenv('SBER_QR_ENABLED', '0')
            checked = json.loads((await service.status(Request(ACTOR, cid=replay['id']))).text)
            assert checked['status'] == 'TEST_PAID'
            assert client.get_status.await_args.args[0] is None
            assert client.create_order.await_count == 1
            async with sessions() as session:
                bill = await session.get(BankPayment, replay['id'])
                assert bill.bank_order_id == 'synthetic-recovered-order'
                assert await session.scalar(select(func.count(BankPayment.id))) == 1
                assert await session.scalar(select(func.count(Receipt.id))) == 0
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize('different_id', [False, True])
def test_registration_response_cannot_regress_a_concurrently_verified_payment(monkeypatch, qr_environment, different_id):
    production_environment(monkeypatch)
    client, _ = mock_qr_client(monkeypatch, test_mode=False)
    async def run():
        engine, sessions, service, bid = await make_service()
        try:
            client.get_status.side_effect = lambda order_id, order_number, amount: normalized(
                'synthetic-confirmed-bank-id', order_number, amount)
            async def register(order_number, amount, description):
                async with sessions() as session:
                    bill = await session.scalar(select(BankPayment).where(BankPayment.order_number == order_number))
                    cid = bill.id
                checked = json.loads((await service.status(Request(ACTOR, cid=cid))).text)
                assert checked['status'] == 'PAID'
                return {'bank_order_id': 'synthetic-other-bank-id' if different_id else 'synthetic-confirmed-bank-id',
                    'payment_url': 'https://qr.nspk.ru/synthetic-registered-order',
                    'status': 'PENDING', 'test_mode': False}
            client.create_order.side_effect = register
            if different_id:
                with pytest.raises(AccessError) as error:
                    await service.create(Request(ACTOR, invoice(bid)))
                assert error.value.status == 409
            else:
                created = json.loads((await service.create(Request(ACTOR, invoice(bid)))).text)
                assert created['status'] == 'PAID'
            async with sessions() as session:
                bill = await session.scalar(select(BankPayment))
                assert bill.status == 'PAID' and bill.bank_order_id == 'synthetic-confirmed-bank-id'
                assert bill.receipt_id is not None
                assert await session.scalar(select(func.count(Receipt.id))) == 1
            assert client.create_order.await_count == client.get_status.await_count == 1
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize('test_mode', [False, True])
def test_verified_deposit_is_counted_once_and_only_real_deposit_reduces_sale(monkeypatch, qr_environment, test_mode):
    if not test_mode:
        production_environment(monkeypatch)
    client, _ = mock_qr_client(monkeypatch, test_mode=test_mode)
    async def run():
        engine, sessions, service, bid = await make_service(deposit='300')
        try:
            deposit = json.loads((await service.create(Request(ACTOR, invoice(bid, purpose='DEPOSIT')))).text)
            for _ in range(2):
                checked = json.loads((await service.status(Request(ACTOR, cid=deposit['id']))).text)
            assert checked['status'] == ('TEST_PAID' if test_mode else 'PAID')
            async with sessions() as session:
                assert await session.scalar(select(func.count(Receipt.id))) == (0 if test_mode else 1)
                if not test_mode:
                    receipt = await session.scalar(select(Receipt))
                    assert receipt.purpose == 'DEPOSIT' and receipt.verified_amount == 300
            purchase = json.loads((await service.create(Request(ACTOR,
                invoice(bid, key='synthetic-second-purchase-0002')))).text)
            assert purchase['amount'] == ('800.00' if test_mode else '500.00')
            assert client.create_order.await_args.args[1] == (80000 if test_mode else 50000)
            listed = json.loads((await Workflow(service.api).listing(Request(ACTOR))).text)
            assert listed['bankConfigured'] and listed['bankProvider'] == 'sber_qr'
            assert listed['bankTestMode'] is test_mode
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_verified_refunds_adjust_one_existing_receipt(monkeypatch, qr_environment):
    production_environment(monkeypatch)
    client, _ = mock_qr_client(monkeypatch, test_mode=False)
    async def run():
        engine, sessions, service, bid = await make_service()
        try:
            created = json.loads((await service.create(Request(ACTOR, invoice(bid)))).text)
            await service.status(Request(ACTOR, cid=created['id']))
            for status, refunded, expected in [('PARTIAL_REFUND', 10000, 700),
                                               ('PARTIAL_REFUND', 10000, 700),
                                               ('REFUNDED', 80000, None)]:
                client.get_status.side_effect = lambda order_id, order_number, amount, status=status, refunded=refunded: normalized(
                    order_id, order_number, amount, status=status, refunded=refunded)
                checked = json.loads((await service.status(Request(ACTOR, cid=created['id']))).text)
                assert checked['status'] == status
                async with sessions() as session:
                    assert await session.scalar(select(func.count(Receipt.id))) == 1
                    receipt = await session.scalar(select(Receipt))
                    assert receipt.verified_amount == expected
                    assert receipt.status == ('REJECTED' if expected is None else 'APPROVED')
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize('change', [
    {'settlement_verified': False}, {'amount_kopecks': 80001}, {'paid_kopecks': 79999},
    {'currency': '840'}, {'order_number': 'another-order'}, {'bank_order_id': 'another-bank-id'},
    {'test_mode': True}, {'status': 'TEST_PAID'}, {'refunded_kopecks': True},
    {'refunded_kopecks': -1}, {'status': 'PARTIAL_REFUND'}, {'refunded_kopecks': 1},
    {'authenticated': True, 'status': 'OAUTH_OK'},
])
def test_unverified_or_mismatched_bank_response_cannot_create_receipt(monkeypatch, qr_environment, change):
    production_environment(monkeypatch)
    client, _ = mock_qr_client(monkeypatch, test_mode=False)
    client.get_status.side_effect = lambda order_id, order_number, amount: (
        normalized(order_id, order_number, amount) | change)
    async def run():
        engine, sessions, service, bid = await make_service()
        try:
            created = json.loads((await service.create(Request(ACTOR, invoice(bid)))).text)
            with pytest.raises(AccessError) as error:
                await service.status(Request(ACTOR, cid=created['id']))
            assert error.value.status == 409
            async with sessions() as session:
                bill = await session.get(BankPayment, created['id'])
                assert bill.status == 'PENDING' and bill.receipt_id is None
                assert await session.scalar(select(func.count(Receipt.id))) == 0
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_legacy_pending_bill_stays_with_acquiring_after_qr_selection(monkeypatch, qr_environment):
    monkeypatch.setenv('SBER_PAYMENTS_TEST', '0')
    client, _ = mock_qr_client(monkeypatch)
    acquiring = AsyncMock(return_value={'errorCode': '0', 'orderNumber': 'legacy-order',
        'orderStatus': 2, 'amount': 12000, 'currency': '643'})
    monkeypatch.setattr('app.bank_payments.bank_call', acquiring)
    async def run():
        engine, sessions, _, bid = await make_service()
        try:
            async with sessions() as session:
                bill = BankPayment(booking_id=bid, created_by_id=1, request_key='legacy-invoice-request-0001',
                    payload_hash='synthetic', order_number='legacy-order', bank_order_id='legacy-bank-id',
                    amount=120, pricing_snapshot='{}', test_mode=False, status='PENDING')
                session.add(bill)
                await session.flush()
                await reconcile(session, bill)
                await reconcile(session, bill)
                assert bill.status == 'PAID' and public_bill(bill)['provider'] == 'sber_ecom'
                assert await session.scalar(select(func.count(Receipt.id))) == 1
                assert acquiring.await_args.args == ('getOrderStatusExtended', {'orderId': 'legacy-bank-id'})
            assert client.get_status.await_count == 0 and acquiring.await_count == 2
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize('setting,value', [('SBER_QR_CLIENT_ID', 'another-synthetic-app'),
    ('SBER_QR_MEMBER_ID', 'MTEST002'), ('SBER_QR_ID_QR', 'another-synthetic-qr'),
    ('SBER_QR_TID', 'TTEST002'), ('SBER_QR_ENV', 'production')])
def test_existing_bill_cannot_follow_changed_application_or_environment(monkeypatch, qr_environment, setting, value):
    client, _ = mock_qr_client(monkeypatch)
    async def run():
        engine, sessions, service, bid = await make_service()
        try:
            created = json.loads((await service.create(Request(ACTOR, invoice(bid)))).text)
            if setting == 'SBER_QR_ENV':
                production_environment(monkeypatch)
            else:
                monkeypatch.setenv(setting, value)
            with pytest.raises(AccessError) as error:
                await service.status(Request(ACTOR, cid=created['id']))
            assert error.value.status == 409 and client.get_status.await_count == 0
            async with sessions() as session:
                assert await session.scalar(select(func.count(Receipt.id))) == 0
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_diagnostics_recheck_actual_owner_role_and_get_has_no_bank_or_certificate_io(monkeypatch, qr_environment):
    client, factory = mock_qr_client(monkeypatch)
    certificate = Mock(side_effect=AssertionError('GET may not parse a certificate'))
    monkeypatch.setattr('app.bank_payments.build_sber_qr_ssl_context', certificate)
    async def run():
        engine, sessions, service, _ = await make_service()
        try:
            for method in (service.connection, service.check_connection):
                with pytest.raises(AccessError) as error:
                    await method(Request(OWNER))
                assert error.value.status == 403
            async with sessions() as session:
                session.add(UserRole(user_id=1, role='OWNER'))
                await session.commit()
            result = json.loads((await service.connection(Request(OWNER))).text)
            assert result['authenticationConfigurationComplete'] is True
            assert result['certificate'] == {'checked': False, 'valid': None}
            assert result['oauth'] == {'checked': False, 'authenticated': False}
            assert result['paymentsVerified'] is False and result['ordersCreated'] is False
            assert certificate.call_count == factory.call_count == client.check_connection.await_count == 0
            assert 'synthetic-client-secret' not in json.dumps(result)
            assert 'synthetic-certificate-password' not in json.dumps(result)
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize('owner_state', ['inactive', 'deleted'])
def test_stale_inactive_or_deleted_owner_cannot_run_diagnostics(monkeypatch, qr_environment, owner_state):
    client, factory = mock_qr_client(monkeypatch)
    certificate = Mock(side_effect=AssertionError('Denied owners cannot parse certificates'))
    monkeypatch.setattr('app.bank_payments.build_sber_qr_ssl_context', certificate)
    stale_owner = {'id': 3, 'roles': ['OWNER']}
    async def run():
        engine, sessions, service, _ = await make_service()
        try:
            async with sessions() as session:
                session.add(User(id=3, tg_id=3, name='Synthetic owner'))
                session.add(UserRole(user_id=3, role='OWNER'))
                await session.commit()
                if owner_state == 'inactive':
                    (await session.get(User, 3)).active = False
                else:
                    await session.execute(delete(UserRole).where(UserRole.user_id == 3))
                    await session.execute(delete(User).where(User.id == 3))
                await session.commit()
            for method in (service.connection, service.check_connection):
                with pytest.raises(AccessError) as error:
                    await method(Request(stale_owner))
                assert error.value.status == 403
            assert certificate.call_count == factory.call_count == client.check_connection.await_count == 0
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_owner_probe_is_auth_only_while_payments_disabled_and_rejects_arbitrary_parameters(monkeypatch, qr_environment):
    monkeypatch.setenv('SBER_QR_ENABLED', '0')
    monkeypatch.delenv('SBER_QR_MEMBER_ID')
    monkeypatch.delenv('SBER_QR_ID_QR')
    monkeypatch.delenv('SBER_QR_TID')
    client, factory = mock_qr_client(monkeypatch)
    context = object()
    certificate = Mock(return_value=context)
    monkeypatch.setattr('app.bank_payments.build_sber_qr_ssl_context', certificate)
    async def run():
        engine, sessions, service, _ = await make_service(owner=True)
        try:
            with pytest.raises(AccessError) as error:
                await service.check_connection(Request(OWNER, {'url': 'https://evil.example'}))
            assert error.value.status == 400 and factory.call_count == certificate.call_count == 0
            response = await service.check_connection(Request(OWNER))
            result = json.loads(response.text)
            assert response.status == 200 and result['ok'] is True
            assert result['enabled'] is False and result['orderConfigurationComplete'] is False
            assert result['certificate'] == {'checked': True, 'valid': True}
            assert result['oauth'] == {'checked': True, 'authenticated': True}
            assert result['ordersCreated'] is False and result['paymentsVerified'] is False
            assert factory.call_args.kwargs == {'ssl_context': context}
            assert client.create_order.await_count == client.get_status.await_count == 0
            assert client.check_connection.await_count == 1
            assert 'synthetic-token' not in response.text and 'synthetic-client-secret' not in response.text
            assert os.environ['SBER_QR_ENABLED'] == '0'
            async with sessions() as session:
                assert await session.scalar(select(func.count(BankPayment.id))) == 0
                assert await session.scalar(select(func.count(Receipt.id))) == 0
            with pytest.raises(AccessError) as error:
                await service.check_connection(Request(OWNER))
            assert error.value.status == 429 and client.check_connection.await_count == 1
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['configuration', 'certificate', 'oauth'])
def test_owner_probe_reports_only_safe_stage_and_never_creates_orders(monkeypatch, qr_environment, failure):
    client, _ = mock_qr_client(monkeypatch)
    certificate = Mock(return_value=object())
    monkeypatch.setattr('app.bank_payments.build_sber_qr_ssl_context', certificate)
    if failure == 'configuration':
        monkeypatch.setenv('SBER_QR_ENDPOINTS_CONFIRMED', '0')
    elif failure == 'certificate':
        certificate.side_effect = SberQRCertificateError('synthetic-private-certificate-error')
    else:
        client.check_connection.side_effect = SberQRClientError('synthetic-private-oauth-error')
    async def run():
        engine, _, service, _ = await make_service(owner=True)
        try:
            response = await service.check_connection(Request(OWNER))
            result = json.loads(response.text)
            assert response.status == 503 and result['ok'] is False and result['stage'] == failure
            assert result['paymentsVerified'] is False and result['ordersCreated'] is False
            assert 'synthetic-private-' not in response.text
            assert 'synthetic-client-secret' not in response.text
            assert 'synthetic-certificate-password' not in response.text
            assert client.create_order.await_count == client.get_status.await_count == 0
            assert client.check_connection.await_count == (1 if failure == 'oauth' else 0)
            assert certificate.call_count == (0 if failure == 'configuration' else 1)
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_one_legacy_error_does_not_block_another_providers_pending_bill(monkeypatch, qr_environment):
    production_environment(monkeypatch)
    client, _ = mock_qr_client(monkeypatch, test_mode=False)
    monkeypatch.setattr('app.bank_payments.configured', lambda: True)
    acquiring = AsyncMock(side_effect=AccessError('synthetic old gateway unavailable', 503))
    monkeypatch.setattr('app.bank_payments.bank_call', acquiring)
    @asynccontextmanager
    async def lease(engine):
        yield None
    monkeypatch.setattr('app.operations_guard.operation_lease', lease)
    async def run():
        engine, sessions, service, bid = await make_service()
        try:
            async with sessions() as session:
                another = await create_booking_record(session, await session.get(User, 1), booking_data())
                session.add(BankPayment(booking_id=another.id, created_by_id=1,
                    request_key='synthetic-old-pending-001', payload_hash='synthetic',
                    order_number='synthetic-old-order', amount=10, pricing_snapshot='{}',
                    test_mode=True, status='PENDING'))
                await session.commit()
            result = json.loads((await service.create(Request(ACTOR, invoice(bid)))).text)
            async def stop_after_one_cycle(interval):
                raise asyncio.CancelledError
            monkeypatch.setattr('app.bank_payments.asyncio.sleep', stop_after_one_cycle)
            with pytest.raises(asyncio.CancelledError):
                await payment_loop(engine, interval=0)
            async with sessions() as session:
                assert (await session.get(BankPayment, result['id'])).status == 'PAID'
                assert await session.scalar(select(func.count(Receipt.id))) == 1
            assert acquiring.await_count == client.get_status.await_count == 1
        finally:
            await engine.dispose()
    asyncio.run(run())
