"""Explicit opt-in mapping for the separately migrated refund schema.

Flag zero preserves the existing mapping (71 ORM tables, 77 prepared DB tables). This function
registers mappings only; it never opens a connection or performs startup DDL.
An installation must apply the reviewed additive migration before setting one.
"""
import os

TABLES = frozenset({'booking_reservation_versions', 'booking_cancellation_cases',
    'bank_original_pay_facts', 'bank_refund_intents', 'payment_cash_events'})
PRIVILEGES = {name: ('SELECT', 'INSERT', 'UPDATE') if name in
    {'booking_cancellation_cases', 'bank_refund_intents'} else ('SELECT', 'INSERT')
    for name in TABLES}
CONSTRAINTS = frozenset({'fk_booking_active_reservation_version', 'fk_receipt_reservation_version',
    'fk_bank_payment_reservation_version', 'fk_original_bank_bill_version',
    'fk_original_receipt_version', 'fk_original_reservation_key', 'fk_intent_complete_original',
    'fk_intent_case_booking', 'fk_cash_original_binding', 'fk_cash_refund_binding',
    'fk_cash_receipt_booking'})
TRIGGERS = frozenset({'pb_reservation_version_immutable', 'pb_cancellation_notice_immutable',
    'pb_original_immutable', 'pb_cash_immutable', 'pb_refund_identity_immutable',
    'pb_refund_case_origin', 'pb_receipt_version_immutable', 'pb_bill_version_immutable',
    'pb_slot_no_truncate', 'pb_notice_no_truncate', 'pb_original_no_truncate',
    'pb_intent_no_truncate', 'pb_cash_no_truncate'})


def enabled():
    value = os.getenv('PHOTO_BOSS_BANK_REFUNDS_SCHEMA', '0')
    if value not in {'0', '1'}:
        raise ValueError('bank_refund_schema_flag_invalid')
    return value == '1'


def install():
    if not enabled():
        return False
    from sqlalchemy import BigInteger, Uuid
    from sqlalchemy.orm import mapped_column

    from .models import BankPayment, Booking, Receipt
    for model in (Booking, Receipt, BankPayment):
        if not hasattr(model, 'reservation_version_id'):
            # The explicit migration owns the composite booking/version FKs.
            # A single-column ORM FK would lose that cross-booking invariant.
            model.reservation_version_id = mapped_column(Uuid(as_uuid=False), nullable=True)
    if not hasattr(Booking, 'payment_money_version'):
        Booking.payment_money_version = mapped_column(BigInteger, default=0,
                                                       server_default='0', nullable=False)
    from . import bank_refund_models, booking_reservation_models  # noqa: F401
    return True


async def verify_ready(connection):
    """Read-only startup gate; create_all/column presence is insufficient."""
    if not enabled():
        return False
    if connection.dialect.name != 'postgresql':
        raise RuntimeError('reviewed_postgresql_refund_schema_required')
    from sqlalchemy import text
    tables = set((await connection.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'"))).scalars())
    if not TABLES <= tables:
        raise RuntimeError('reviewed_refund_tables_required')
    for table, privileges in PRIVILEGES.items():
        for privilege in privileges:
            allowed = await connection.scalar(text('SELECT has_table_privilege(current_user,:table,:privilege)'),
                {'table': 'public.' + table, 'privilege': privilege})
            if allowed is not True:
                raise RuntimeError('reviewed_refund_application_grants_required')
    constraints = set((await connection.execute(text("""SELECT conname FROM pg_constraint
        WHERE connamespace='public'::regnamespace AND convalidated"""))).scalars())
    triggers = set((await connection.execute(text("""SELECT t.tgname FROM pg_trigger t
        JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND NOT t.tgisinternal AND t.tgenabled='O'"""))).scalars())
    if not CONSTRAINTS <= constraints or not TRIGGERS <= triggers:
        raise RuntimeError('reviewed_refund_constraints_and_triggers_required')
    return True
