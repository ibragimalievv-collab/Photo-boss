"""Reservation facts; import only after the explicit migration is approved.

This module is deliberately not installed in app.models or a startup factory.
It owns no bank operation, refund intent or cash event. PostgreSQL migration
triggers must additionally enforce immutability against direct SQL writers.
"""
from datetime import date, datetime, time

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
    Uuid,
    event,
    inspect,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .models import Base, Booking

CASE_STATES = frozenset({
    'RECEIVED', 'NO_VERIFIED_DEPOSIT', 'RETAINED', 'REFUND_READY',
    'REFUND_PENDING', 'REFUND_UNKNOWN', 'REFUNDED',
    'BLOCKED_ORIGIN_UNAVAILABLE', 'BLOCKED_PAYMENT_RECONCILIATION',
    'LEGACY_FACTS_UNAVAILABLE', 'STALE_SLOT_REVIEW', 'MIXED_MONEY_REVIEW',
})


class ImmutableReservationFact(ValueError):
    pass


class BookingReservationVersion(Base):
    __tablename__ = 'booking_reservation_versions'
    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey('bookings.id', ondelete='RESTRICT'))
    reservation_key: Mapped[str] = mapped_column(Uuid(as_uuid=False))
    version: Mapped[int] = mapped_column(Integer)
    previous_version_id: Mapped[str | None] = mapped_column(Uuid(as_uuid=False), nullable=True)
    shoot_date: Mapped[date] = mapped_column(Date)
    shoot_time: Mapped[time] = mapped_column(Time)
    business_timezone: Mapped[str] = mapped_column(String(40), default='Europe/Moscow')
    expected_deposit_kopecks: Mapped[int] = mapped_column(BigInteger, default=100_000)
    policy_version: Mapped[str] = mapped_column(String(80))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    captured_by_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='RESTRICT'))
    provenance: Mapped[str] = mapped_column(String(30), default='NEW_SERVER')
    reason: Mapped[str] = mapped_column(Text)
    booking: Mapped[Booking] = relationship(Booking, foreign_keys=[booking_id])
    __table_args__ = (
        UniqueConstraint('booking_id', 'version'),
        UniqueConstraint('booking_id', 'id'),
        UniqueConstraint('booking_id', 'id', 'reservation_key'),
        ForeignKeyConstraint(['booking_id', 'previous_version_id'],
            ['booking_reservation_versions.booking_id', 'booking_reservation_versions.id'],
            ondelete='RESTRICT'),
        CheckConstraint('version > 0'),
        CheckConstraint("business_timezone = 'Europe/Moscow'"),
        CheckConstraint('expected_deposit_kopecks BETWEEN 0 AND 1000000000'),
        CheckConstraint("provenance IN ('NEW_SERVER','RECOVERED_AUDIT')"),
        CheckConstraint('length(trim(reason)) BETWEEN 3 AND 500'),
    )


class BookingCancellationCase(Base):
    __tablename__ = 'booking_cancellation_cases'
    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey('bookings.id', ondelete='RESTRICT'))
    reservation_version_id: Mapped[str | None] = mapped_column(Uuid(as_uuid=False), nullable=True)
    reservation_key: Mapped[str | None] = mapped_column(Uuid(as_uuid=False), nullable=True)
    first_actor_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='RESTRICT'))
    request_key: Mapped[str] = mapped_column(String(80))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str] = mapped_column(Text)
    request_kind: Mapped[str] = mapped_column(String(20))
    policy_version: Mapped[str] = mapped_column(String(80))
    state: Mapped[str] = mapped_column(String(50), default='RECEIVED')
    decision_revision: Mapped[int] = mapped_column(Integer, default=0)
    verified_net_kopecks: Mapped[int] = mapped_column(BigInteger, default=0)
    retained_kopecks: Mapped[int] = mapped_column(BigInteger, default=0)
    eligible_refund_kopecks: Mapped[int] = mapped_column(BigInteger, default=0)
    decision: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, 'postgresql'), default=dict)
    assessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint('first_actor_id', 'request_key'),
        UniqueConstraint('booking_id', 'reservation_key'),
        ForeignKeyConstraint(['booking_id', 'reservation_version_id', 'reservation_key'],
            ['booking_reservation_versions.booking_id', 'booking_reservation_versions.id',
             'booking_reservation_versions.reservation_key'], ondelete='RESTRICT'),
        CheckConstraint('(reservation_version_id IS NULL) = (reservation_key IS NULL)'),
        CheckConstraint('recorded_at >= received_at'),
        CheckConstraint('length(request_fingerprint) = 64'),
        CheckConstraint('length(trim(reason)) BETWEEN 3 AND 500'),
        CheckConstraint("request_kind IN ('ORDINARY','FORCE_MAJEURE')"),
        CheckConstraint('decision_revision >= 0'),
        CheckConstraint('verified_net_kopecks BETWEEN 0 AND 99999999999999'),
        CheckConstraint('retained_kopecks >= 0 AND eligible_refund_kopecks >= 0'),
        CheckConstraint('retained_kopecks + eligible_refund_kopecks <= verified_net_kopecks'),
        CheckConstraint('state IN (' + ','.join(repr(s) for s in sorted(CASE_STATES)) + ')'),
        Index('uq_booking_unresolved_legacy_cancellation', 'booking_id', unique=True,
              sqlite_where=text('reservation_key IS NULL'),
              postgresql_where=text('reservation_key IS NULL')),
        Index('ix_booking_cancellation_received', 'booking_id', 'received_at'),
    )


NOTICE_FIELDS = frozenset({'id', 'booking_id', 'reservation_version_id', 'reservation_key',
    'first_actor_id', 'request_key', 'request_fingerprint', 'received_at', 'recorded_at',
    'reason', 'request_kind', 'policy_version'})


@event.listens_for(BookingReservationVersion, 'before_update')
def _version_immutable(mapper, connection, target):
    if any(inspect(target).attrs[c.key].history.has_changes() for c in mapper.columns):
        raise ImmutableReservationFact('reservation_version_is_immutable')


@event.listens_for(BookingCancellationCase, 'before_update')
def _notice_immutable(mapper, connection, target):
    if any(inspect(target).attrs[name].history.has_changes() for name in NOTICE_FIELDS):
        raise ImmutableReservationFact('cancellation_notice_is_immutable')


@event.listens_for(BookingReservationVersion, 'before_delete')
@event.listens_for(BookingCancellationCase, 'before_delete')
def _fact_not_deleted(mapper, connection, target):
    raise ImmutableReservationFact('reservation_fact_cannot_be_deleted')
