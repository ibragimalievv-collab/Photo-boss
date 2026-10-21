"""Twenty selected originals and their invoice use one sale and one deposit."""
import asyncio
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import func, select
from test_team import Request
from test_workflow_replay import ACTOR, packet, setup
from test_workflow_services import booking_data

from app.bank_payments import BankPayments
from app.models import BankPayment, Booking, Receipt, Sale, SaleDraft, SaleDraftPhoto
from app.workflow import Workflow


def test_twenty_originals_receipt_and_post_completion_invoice_use_same_purchase(monkeypatch):
    async def telegram(self, actor, raw, **kwargs):
        digest = hashlib.sha256(raw).hexdigest()
        return digest, digest

    async def selected(self, draft, raw):
        return 'app:/twenty-selected/' + hashlib.sha256(raw).hexdigest()

    monkeypatch.setattr(Workflow, 'save_telegram', telegram)
    monkeypatch.setattr(Workflow, 'save_selected', selected)
    monkeypatch.setattr('app.workflow.extract_receipt', AsyncMock(return_value={'status': 'unavailable'}))
    monkeypatch.setattr('app.bank_payments.configured', lambda: True)
    monkeypatch.setenv('SBER_RETURN_URL', 'https://photo-boss.example/test-return')
    bank = AsyncMock(return_value={'orderId': 'synthetic-order', 'formUrl': 'https://ecomtest.sberbank.ru/payment/synthetic'})
    monkeypatch.setattr('app.bank_payments.bank_call', bank)

    async def run():
        engine, factory, work = await setup()
        try:
            result = json.loads((await work.execute(ACTOR, packet('twenty-booking', 'booking',
                booking_data() | {'photographer_id': None, 'deposit': '1000'}))).text)
            bid = result['bookingId']
            async with factory() as session:
                booking = await session.get(Booking, bid)
                booking.photographer_id, booking.status = 2, 'READY_FOR_SALE'
                session.add(Receipt(booking_id=bid, uploaded_by_id=1, purpose='DEPOSIT',
                    file_id='synthetic-deposit', file_unique_id='synthetic-deposit',
                    expected_amount=1000, verified_amount=1000, status='APPROVED'))
                await session.commit()
            start = packet('twenty-start', 'sale_start', {'booking': {'id': bid}, 'price': '400.00'})
            did = json.loads((await work.execute(ACTOR, start)).text)['draftId']
            draft = {'key': start['key']}
            await work.execute(ACTOR, packet('twenty-receipt', 'sale_receipt', {'draft': draft}),
                b'\xff\xd8\xff' + b'synthetic-receipt' * 20)
            await work.execute(ACTOR, packet('twenty-counts', 'sale_counts',
                {'draft': draft, 'total': 150, 'sold': 20, 'price': '400.00'}))
            for number in range(20):
                response = json.loads((await work.execute(ACTOR,
                    packet(f'twenty-photo-{number}', 'sale_selected', {'draft': draft}),
                    b'\xff\xd8\xff' + bytes([number]) * 200)).text)
            assert response['draftStatus'] == 'COMPLETED' and response['soldPhotos'] == 20
            async with factory() as session:
                assert await session.scalar(select(func.count(Sale.id))) == 1
                assert await session.scalar(select(func.count(SaleDraftPhoto.id))) == 20
                assert (await session.get(SaleDraft, did)).receipt_file_id
                payment = await session.scalar(select(Receipt).where(Receipt.purpose == 'PAYMENT'))
                assert payment.expected_amount == 7000
            api = SimpleNamespace(engine=engine, body=AsyncMock(side_effect=lambda request: request.body))
            # This is the unchanged twenty-photo form after its last file was
            # accepted. Invoicing it cannot add the same purchase a second time.
            body = {'bookingId': bid, 'total': 150, 'sold': 20, 'tariffId': None,
                'sourceDraftId': did, 'amount': None, 'purpose': 'PURCHASE', 'requestKey': 'twenty-photo-invoice-request'}
            bill = json.loads((await BankPayments(api).create(Request(ACTOR, body))).text)
            assert bill['amount'] == '7000.00'
            assert bank.await_args.args[1]['amount'] == 700000
            assert json.loads((await BankPayments(api).create(Request(ACTOR, body))).text)['id'] == bill['id']
            assert bank.await_count == 1
            async with factory() as session:
                assert await session.scalar(select(func.count(BankPayment.id))) == 1
                assert await session.scalar(select(func.count(Sale.id))) == 1
        finally:
            await engine.dispose()
    asyncio.run(run())
