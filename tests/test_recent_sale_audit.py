"""Diagnostic revenue totals exclude pending drafts and preserve unpaid balances."""
import asyncio

from test_workflow_services import booking_data, fixture

from app.models import SaleDraftPhoto, User
from app.services.bookings import create_booking_record
from app.services.sale_workflow import (
    capture_draft_receipt,
    complete_sale,
    set_sale_counts,
    start_sale_draft,
)
from app.storage_inventory import recent_sale_audit


def test_recent_audit_separates_revenue_pending_photos_and_payment():
    async def run():
        engine, factory = await fixture()
        try:
            async with factory() as session:
                actor = await session.get(User, 1)
                booking = await create_booking_record(session, actor, booking_data())
                booking.photographer_id = 2
                booking.status = 'READY_FOR_SALE'
                first = await start_sale_draft(session, actor, {'MANAGER'}, booking.id)
                await capture_draft_receipt(session, actor, first, 'receipt-1', 'unique-1', 'hash-1', {'status': 'unavailable'})
                await set_sale_counts(session, actor, first, 150, 2)
                for n in range(2):
                    session.add(SaleDraftPhoto(draft_id=first.id, telegram_file_id=str(n),
                        telegram_unique_id=str(n), storage_path=f'app:/selected/{n}.jpg', sha256=str(n), byte_size=100))
                await session.flush()
                await complete_sale(session, actor, first.id)
                second = await start_sale_draft(session, actor, {'MANAGER'}, booking.id)
                await capture_draft_receipt(session, actor, second, 'receipt-2', 'unique-2', 'hash-2', {'status': 'unavailable'})
                await set_sale_counts(session, actor, second, 150, 3)
                session.add(SaleDraftPhoto(draft_id=second.id, telegram_file_id='partial',
                    telegram_unique_id='partial', storage_path='app:/selected/partial.jpg', sha256='partial', byte_size=100))
                await session.commit()
                report = await recent_sale_audit(engine)
                assert report['salesCount'] == 1
                assert report['soldPhotos'] == 2
                assert report['salesAmount'] == 800
                pending = next(d for d in report['drafts'] if d['id'] == second.id)
                assert pending['status'] == 'AWAITING_SELECTED'
                assert pending['selected'] == 1 and pending['sold'] == 3
                assert pending['calculatedAmount'] == 1200
                assert pending['expectedPayment'] == 2000  # Includes earlier unpaid sale.
                assert report['sales'][0]['paymentStatus'] == 'UNPAID'
        finally:
            await engine.dispose()
    asyncio.run(run())
