"""Malformed client input and bank URLs must fail closed without server errors."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_team import Request

from app.bank_payments import BankPayments, bank_url
from app.miniapp_security import AccessError


@pytest.mark.parametrize('purpose', [[], {}, 1, True, None, 'REFUND'])
def test_malformed_payment_purpose_is_a_client_error(monkeypatch, purpose):
    monkeypatch.setattr('app.bank_payments.configured', lambda: True)
    monkeypatch.setenv('SBER_RETURN_URL', 'https://photo-boss.example/return')
    body = {'bookingId': 1, 'total': 1, 'sold': 1, 'tariffId': None,
            'amount': None, 'requestKey': 'test-invalid-purpose-123', 'purpose': purpose}
    api = SimpleNamespace(body=AsyncMock(return_value=body))
    async def run():
        with pytest.raises(AccessError) as error:
            await BankPayments(api).create(Request({'id': 1}, body))
        assert error.value.status == 400
    asyncio.run(run())


@pytest.mark.parametrize('url', [None, [], {}, 1,
    'https://ecomtest.sberbank.ru:invalid/payment',
    'https://ecomtest.sberbank.ru:65536/payment',
    'https://[invalid/payment',
    'https://ecomtest.sberbank.ru.evil.example/payment',
    'https://user:secret@ecomtest.sberbank.ru/payment',
    'http://ecomtest.sberbank.ru/payment'])
def test_malformed_or_untrusted_bank_url_is_rejected(url):
    assert bank_url(url) is False


def test_trusted_bank_url_remains_accepted():
    assert bank_url('https://ecomtest.sberbank.ru:443/payment/test') is True


def test_malformed_bank_response_keeps_invoice_for_reconciliation_without_reregistering(monkeypatch):
    from sqlalchemy import func, select
    from test_workflow_services import booking_data, fixture

    from app.models import BankPayment, User
    from app.services.bookings import create_booking_record

    monkeypatch.setattr('app.bank_payments.configured', lambda: True)
    monkeypatch.setenv('SBER_RETURN_URL', 'https://photo-boss.example/return')
    gateway = AsyncMock(return_value={'orderId': 'bank-order',
        'formUrl': 'https://ecomtest.sberbank.ru:invalid/payment'})
    monkeypatch.setattr('app.bank_payments.bank_call', gateway)
    async def run():
        engine, factory = await fixture()
        try:
            async with factory() as session:
                actor = await session.get(User, 1)
                booking = await create_booking_record(session, actor, booking_data())
                booking.photographer_id, booking.status = 2, 'READY_FOR_SALE'
                await session.commit()
            body = {'bookingId': booking.id, 'total': 1, 'sold': 1, 'tariffId': None,
                'amount': None, 'requestKey': 'test-malformed-bank-url-123', 'purpose': 'PURCHASE'}
            api = SimpleNamespace(engine=engine, body=AsyncMock(return_value=body))
            with pytest.raises(AccessError) as error:
                await BankPayments(api).create(Request({'id': 1}, body))
            assert error.value.status == 503
            async with factory() as session:
                assert await session.scalar(select(func.count(BankPayment.id))) == 1
                bill = await session.scalar(select(BankPayment))
                assert bill.status == 'CREATING' and bill.amount == 400
            replay = await BankPayments(api).create(Request({'id': 1}, body))
            assert replay.status == 200
            assert gateway.await_count == 1
        finally:
            await engine.dispose()
    asyncio.run(run())
