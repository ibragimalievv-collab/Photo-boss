from datetime import UTC, date, datetime, time
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utc_now():
    """Return naive UTC for the existing TIMESTAMP WITHOUT TIME ZONE columns."""
    return datetime.now(UTC).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(150))
    username: Mapped[str | None] = mapped_column(String(150), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    terminated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    roles = relationship(
        "UserRole", back_populates="user", cascade="all, delete-orphan"
    )


class UserRole(Base):
    __tablename__ = "user_roles"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(30))
    user = relationship("User", back_populates="roles")
    __table_args__ = (UniqueConstraint("user_id", "role"),)


class WorkRuleAcceptance(Base):
    __tablename__ = "work_rule_acceptances"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[str] = mapped_column(String(80))
    text_sha256: Mapped[str] = mapped_column(String(64))
    accepted_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    __table_args__ = (
        UniqueConstraint(
            "user_id", "version", name="uq_work_rule_acceptance_user_version"
        ),
    )


class WorkChatReadState(Base):
    __tablename__ = "work_chat_read_states"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    scope_key: Mapped[str] = mapped_column(String(64))
    last_read_message_id: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    __table_args__ = (
        UniqueConstraint(
            "user_id", "scope_key", name="uq_work_chat_read_state_user_scope"
        ),
        CheckConstraint("last_read_message_id >= 0"),
    )


class WorkChatAttachment(Base):
    __tablename__ = "work_chat_attachments"
    id: Mapped[int] = mapped_column(primary_key=True)
    uploader_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    storage_path: Mapped[str] = mapped_column(String(500), unique=True)
    original_name: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(100))
    byte_size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)
    __table_args__ = (
        CheckConstraint("byte_size > 0"),
    )


class WorkChatMessage(Base):
    __tablename__ = "work_chat_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    sender_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    recipient_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    attachment_id: Mapped[int | None] = mapped_column(
        ForeignKey("work_chat_attachments.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
        index=True,
    )
    body: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)
    __table_args__ = (
        CheckConstraint("length(body) BETWEEN 0 AND 2000"),
        CheckConstraint("length(body) > 0 OR attachment_id IS NOT NULL"),
        CheckConstraint("recipient_id IS NULL OR recipient_id <> sender_id"),
        {"sqlite_autoincrement": True},
    )


class WorkChatDeletion(Base):
    """Content-free change feed for clients with an already open conversation."""
    __tablename__ = "work_chat_deletions"
    id: Mapped[int] = mapped_column(primary_key=True)
    __table_args__ = ({"sqlite_autoincrement": True},)
    message_id: Mapped[int] = mapped_column(Integer, unique=True)
    sender_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    recipient_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    deleted_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)


class Hotel(Base):
    __tablename__ = "hotels"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    address: Mapped[str | None] = mapped_column(String(300), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class HotelEmployee(Base):
    __tablename__ = "hotel_employees"
    id: Mapped[int] = mapped_column(primary_key=True)
    hotel_id: Mapped[int] = mapped_column(ForeignKey("hotels.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))


class Client(Base):
    __tablename__ = "clients"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(80), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class Package(Base):
    __tablename__ = "packages"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(150))
    price_per_photo: Mapped[float] = mapped_column(Float)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Booking(Base):
    __tablename__ = "bookings"
    id: Mapped[int] = mapped_column(primary_key=True)
    hotel_id: Mapped[int] = mapped_column(ForeignKey("hotels.id"))
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    room: Mapped[str] = mapped_column(String(100))
    guest_count: Mapped[int] = mapped_column(Integer, default=1)
    deposit: Mapped[float] = mapped_column(Float, default=0)
    shoot_date: Mapped[date] = mapped_column(Date)
    shoot_time: Mapped[time] = mapped_column(Time)
    package_id: Mapped[int] = mapped_column(ForeignKey("packages.id"))
    manager_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    photographer_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(40), default="NEW")
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class Shooting(Base):
    __tablename__ = "shootings"
    id: Mapped[int] = mapped_column(primary_key=True)
    booking_id: Mapped[int] = mapped_column(
        ForeignKey("bookings.id", ondelete="CASCADE"), unique=True
    )
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    arrived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    viewing_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ready_for_sale_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    full_upload_completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(40), default="ASSIGNED")
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)


class Photo(Base):
    __tablename__ = "photos"
    id: Mapped[int] = mapped_column(primary_key=True)
    shooting_id: Mapped[int] = mapped_column(
        ForeignKey("shootings.id", ondelete="CASCADE")
    )
    file_id: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class PhotoStorage(Base):
    __tablename__ = "photo_storage"
    id: Mapped[int] = mapped_column(primary_key=True)
    photo_id: Mapped[int] = mapped_column(
        ForeignKey("photos.id", ondelete="CASCADE"), unique=True, index=True
    )
    shooting_id: Mapped[int] = mapped_column(
        ForeignKey("shootings.id", ondelete="CASCADE"), index=True
    )
    telegram_file_id: Mapped[str] = mapped_column(String(300))
    telegram_unique_id: Mapped[str] = mapped_column(String(300), index=True)
    source_kind: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    disk_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    byte_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    stored_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    __table_args__ = (
        UniqueConstraint(
            "shooting_id", "telegram_unique_id", name="uq_photo_storage_shooting_unique_file"
        ),
        CheckConstraint("source_kind IN ('PHOTO', 'DOCUMENT')"),
        CheckConstraint("status IN ('PENDING', 'UPLOADING', 'STORED', 'FAILED')"),
        CheckConstraint("attempts >= 0"),
    )


class SaleDraft(Base):
    __tablename__ = "sale_drafts"
    id: Mapped[int] = mapped_column(primary_key=True)
    booking_id: Mapped[int] = mapped_column(
        ForeignKey("bookings.id", ondelete="CASCADE"), index=True
    )
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(30), default="AWAITING_RECEIPT", index=True)
    receipt_file_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    receipt_file_unique_id: Mapped[str | None] = mapped_column(String(300), nullable=True, index=True)
    receipt_image_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    receipt_operation_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    receipt_analysis: Mapped[str | None] = mapped_column(Text, nullable=True)
    declared_photo_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sold_photos: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expected_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (
        CheckConstraint(
            "status IN ('AWAITING_RECEIPT','AWAITING_COUNTS','AWAITING_SELECTED','COMPLETED','CANCELLED')"
        ),
        CheckConstraint("declared_photo_count IS NULL OR declared_photo_count > 0"),
        CheckConstraint("sold_photos IS NULL OR sold_photos > 0"),
        CheckConstraint("expected_amount IS NULL OR expected_amount > 0"),
    )


class SaleDraftPhoto(Base):
    __tablename__ = "sale_draft_photos"
    id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[int] = mapped_column(
        ForeignKey("sale_drafts.id", ondelete="CASCADE"), index=True
    )
    telegram_file_id: Mapped[str] = mapped_column(String(300))
    telegram_unique_id: Mapped[str] = mapped_column(String(300))
    storage_path: Mapped[str] = mapped_column(String(500), unique=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    byte_size: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)
    __table_args__ = (
        UniqueConstraint(
            "draft_id", "telegram_unique_id", name="uq_sale_draft_photo_telegram"
        ),
        CheckConstraint("byte_size > 0"),
    )


class Sale(Base):
    __tablename__ = "sales"
    id: Mapped[int] = mapped_column(primary_key=True)
    booking_id: Mapped[int | None] = mapped_column(
        ForeignKey("bookings.id"), nullable=True
    )
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    credited_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    commission_role: Mapped[str] = mapped_column(String(30), default="PHOTOGRAPHER")
    sold_photos: Mapped[int] = mapped_column(Integer)
    declared_photo_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_draft_id: Mapped[int | None] = mapped_column(
        ForeignKey("sale_drafts.id", ondelete="SET NULL"), nullable=True, unique=True
    )
    amount: Mapped[float] = mapped_column(Float)
    percent: Mapped[float] = mapped_column(Float, default=0)
    commission: Mapped[float] = mapped_column(Float, default=0)
    commission_finalized_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    manager_percent_applied: Mapped[str | None] = mapped_column(String(60), nullable=True)
    manager_payroll_entry_id: Mapped[int | None] = mapped_column(ForeignKey('payroll_entries.id', ondelete='SET NULL'), nullable=True)
    payment_status: Mapped[str] = mapped_column(String(20), default="UNPAID")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class Receipt(Base):
    __tablename__ = "receipts"
    id: Mapped[int] = mapped_column(primary_key=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id"), index=True)
    uploaded_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    purpose: Mapped[str] = mapped_column(String(20))
    file_id: Mapped[str] = mapped_column(String(300))
    file_unique_id: Mapped[str] = mapped_column(String(300), unique=True)
    image_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    operation_key: Mapped[str | None] = mapped_column(String(64), index=True)
    # Unique claims also protect against two owners approving copies concurrently.
    approved_image_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    approved_operation_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    expected_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    verified_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    analysis: Mapped[str | None] = mapped_column(Text)
    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    __table_args__ = (
        CheckConstraint("purpose IN ('DEPOSIT', 'PAYMENT')"),
        CheckConstraint("status IN ('PENDING', 'APPROVED', 'REJECTED')"),
        CheckConstraint("expected_amount > 0"),
        CheckConstraint("verified_amount IS NULL OR verified_amount > 0"),
    )


class Compensation(Base):
    __tablename__ = "compensation"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(30))
    base_salary: Mapped[float] = mapped_column(Float, default=0)
    sales_percent: Mapped[float] = mapped_column(Float, default=0)


class PayrollEntry(Base):
    __tablename__ = "payroll_entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(30))
    amount: Mapped[float] = mapped_column(Float)
    period: Mapped[str] = mapped_column(String(30))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class CashMovement(Base):
    """Owner-recorded paid outflows, distinct from salary accruals."""
    __tablename__ = 'cash_movements'
    id: Mapped[int] = mapped_column(primary_key=True)
    category: Mapped[str] = mapped_column(String(20))
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    paid_on: Mapped[date] = mapped_column(Date, index=True)
    hotel_id: Mapped[int | None] = mapped_column(ForeignKey('hotels.id'), nullable=True)
    employee_id: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    note: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default='POSTED')
    created_by_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    voided_by_id: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    void_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__ = (
        CheckConstraint("category IN ('HOTEL','TAX','PAYROLL','OTHER')"),
        CheckConstraint('amount>0'),
        CheckConstraint("status IN ('POSTED','VOIDED')"),
        CheckConstraint("(category='PAYROLL' AND employee_id IS NOT NULL) OR (category<>'PAYROLL' AND employee_id IS NULL)"),
    )


class Shift(Base):
    __tablename__ = "shifts"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    hotel_id: Mapped[int] = mapped_column(ForeignKey("hotels.id"))
    start_at: Mapped[datetime] = mapped_column(DateTime)
    end_at: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(20), default="PLANNED")


class ShiftCheckIn(Base):
    __tablename__ = "shift_check_ins"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    shift_date: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(
        String(30), default="AWAITING_LOCATION", index=True
    )
    initiated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    location_received_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    full_body_file_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    late: Mapped[bool] = mapped_column(Boolean, default=False)
    fine_amount: Mapped[float] = mapped_column(Float, default=0)
    offline_claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (
        UniqueConstraint("user_id", "shift_date", name="uq_shift_check_in_user_day"),
    )


class ShiftCheckOut(Base):
    __tablename__ = "shift_check_outs"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    shift_date: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(
        String(30), default="AWAITING_LOCATION", index=True
    )
    initiated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    location_received_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    workplace_file_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    offline_claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (
        UniqueConstraint("user_id", "shift_date", name="uq_shift_check_out_user_day"),
    )

    report_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    report_saved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SalesPlan(Base):
    __tablename__ = "sales_plans"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    hotel_id: Mapped[int | None] = mapped_column(ForeignKey("hotels.id"), nullable=True)
    period: Mapped[str] = mapped_column(String(30))
    target_amount: Mapped[float] = mapped_column(Float)


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    text: Mapped[str] = mapped_column(Text)
    sent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    event_key: Mapped[str | None] = mapped_column(String(160), nullable=True)
    priority: Mapped[str] = mapped_column(String(20), default="info", server_default="info")
    kind: Mapped[str] = mapped_column(String(50), default="legacy", server_default="legacy")
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (UniqueConstraint("user_id", "event_key", name="uq_notification_event"),)


class BookingReminder(Base):
    __tablename__ = "booking_reminders"
    id: Mapped[int] = mapped_column(primary_key=True)
    booking_id: Mapped[int] = mapped_column(
        ForeignKey("bookings.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    reminder_kind: Mapped[str] = mapped_column(String(10))
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    __table_args__ = (
        UniqueConstraint(
            "booking_id", "user_id", "reminder_kind", name="uq_booking_reminder"
        ),
        CheckConstraint("reminder_kind IN ('24H', '2H')"),
    )


class RepeatSaleLead(Base):
    __tablename__ = "repeat_sale_leads"
    id: Mapped[int] = mapped_column(primary_key=True)
    booking_id: Mapped[int] = mapped_column(
        ForeignKey("bookings.id", ondelete="CASCADE"), unique=True, index=True
    )
    manager_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="NEW", index=True)
    offer_text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    contacted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (
        CheckConstraint("status IN ('NEW', 'CONTACTED', 'DECLINED')"),
    )


class BankReconciliation(Base):
    __tablename__ = "bank_reconciliations"
    id: Mapped[int] = mapped_column(primary_key=True)
    receipt_id: Mapped[int] = mapped_column(
        ForeignKey("receipts.id", ondelete="CASCADE"), unique=True, index=True
    )
    provider: Mapped[str] = mapped_column(String(50), default="MANUAL")
    bank_reference: Mapped[str] = mapped_column(String(150), unique=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    status: Mapped[str] = mapped_column(String(20), default="MATCHED", index=True)
    matched_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    matched_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    __table_args__ = (
        CheckConstraint("amount > 0"),
        CheckConstraint("status IN ('MATCHED', 'MISMATCH')"),
    )


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(100))
    entity: Mapped[str | None] = mapped_column(String(50), nullable=True)
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class TrainingAssignment(Base):
    __tablename__ = "training_assignments"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    assigned_date: Mapped[date] = mapped_column(Date, index=True)
    category_slug: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", index=True)
    assigned_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ai_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ai_analysis: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    submissions = relationship(
        "TrainingSubmission",
        back_populates="assignment",
        cascade="all, delete-orphan",
        order_by="TrainingSubmission.pose_index",
    )
    __table_args__ = (
        UniqueConstraint("user_id", "assigned_date", name="uq_training_user_day"),
        CheckConstraint(
            "ai_score IS NULL OR ai_score BETWEEN 0 AND 100",
            name="ck_training_ai_score",
        ),
    )


class TrainingSubmission(Base):
    __tablename__ = "training_submissions"
    id: Mapped[int] = mapped_column(primary_key=True)
    assignment_id: Mapped[int] = mapped_column(
        ForeignKey("training_assignments.id", ondelete="CASCADE"), index=True
    )
    pose_index: Mapped[int] = mapped_column(Integer)
    reference_filename: Mapped[str] = mapped_column(String(200))
    submitted_file_id: Mapped[str] = mapped_column(String(300))
    submitted_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    assignment = relationship("TrainingAssignment", back_populates="submissions")
    __table_args__ = (
        UniqueConstraint(
            "assignment_id", "pose_index", name="uq_training_assignment_pose"
        ),
        CheckConstraint("pose_index BETWEEN 1 AND 5", name="ck_training_pose_index"),
    )


class AcademyLessonProgress(Base):
    __tablename__ = "academy_lesson_progress"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    topic_slug: Mapped[str] = mapped_column(String(50))
    completed_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    __table_args__ = (
        UniqueConstraint(
            "user_id", "topic_slug", name="uq_academy_lesson_progress_user_topic"
        ),
    )


class AcademyReminder(Base):
    __tablename__ = "academy_reminders"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    reminder_date: Mapped[date] = mapped_column(Date, index=True)
    kind: Mapped[str] = mapped_column(String(30), default="DAILY_LESSON")
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    __table_args__ = (
        UniqueConstraint(
            "user_id", "reminder_date", "kind", name="uq_academy_reminder"
        ),
    )


class AcademyCertificate(Base):
    __tablename__ = "academy_certificates"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True
    )
    certificate_no: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    verification_code: Mapped[str] = mapped_column(
        String(64), unique=True, index=True
    )
    final_score: Mapped[int] = mapped_column(Integer)
    issued_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (
        CheckConstraint(
            "final_score BETWEEN 85 AND 100", name="ck_academy_certificate_score"
        ),
    )


class AcademyLocation(Base):
    __tablename__ = "academy_locations"
    id: Mapped[int] = mapped_column(primary_key=True)
    hotel_id: Mapped[int | None] = mapped_column(
        ForeignKey("hotels.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(150))
    description: Mapped[str] = mapped_column(Text, default="")
    shot_plan: Mapped[str] = mapped_column(Text, default="[]")
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class AcademyReview(Base):
    __tablename__ = "academy_reviews"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    photo_id: Mapped[int | None] = mapped_column(
        ForeignKey("photos.id", ondelete="SET NULL"), nullable=True, unique=True, index=True
    )
    reviewer_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    file_id: Mapped[str] = mapped_column(String(300))
    composition_score: Mapped[int] = mapped_column(Integer)
    light_score: Mapped[int] = mapped_column(Integer)
    pose_score: Mapped[int] = mapped_column(Integer)
    emotion_score: Mapped[int] = mapped_column(Integer)
    color_score: Mapped[int] = mapped_column(Integer)
    quality_score: Mapped[int] = mapped_column(Integer, index=True)
    strengths: Mapped[str] = mapped_column(Text, default="")
    issues: Mapped[str] = mapped_column(Text, default="")
    recommendation: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)
    __table_args__ = (
        CheckConstraint(
            "composition_score BETWEEN 1 AND 10",
            name="ck_academy_review_composition_score",
        ),
        CheckConstraint(
            "light_score BETWEEN 1 AND 10",
            name="ck_academy_review_light_score",
        ),
        CheckConstraint(
            "pose_score BETWEEN 1 AND 10",
            name="ck_academy_review_pose_score",
        ),
        CheckConstraint(
            "emotion_score BETWEEN 1 AND 10",
            name="ck_academy_review_emotion_score",
        ),
        CheckConstraint(
            "color_score BETWEEN 1 AND 10",
            name="ck_academy_review_color_score",
        ),
        CheckConstraint(
            "quality_score BETWEEN 1 AND 10",
            name="ck_academy_review_quality_score",
        ),
    )


class HRCandidate(Base):
    __tablename__ = 'hr_candidates'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(150))
    contact: Mapped[str] = mapped_column(String(300), default='')
    source: Mapped[str] = mapped_column(String(150), default='')
    role: Mapped[str] = mapped_column(String(30))
    stage: Mapped[str] = mapped_column(String(30), default='NEW')
    interview_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decision: Mapped[str] = mapped_column(Text, default='')
    notes: Mapped[str] = mapped_column(Text, default='[]')
    employee_id: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True, unique=True)
    created_by_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    __table_args__ = (CheckConstraint("stage IN ('NEW','CONTACTED','INTERVIEW','OFFER','DOCUMENTS','HIRED','REJECTED')"),
                     CheckConstraint("role IN ('PHOTOGRAPHER','MANAGER')"))


class AcademyAssessment(Base):
    __tablename__ = 'academy_assessments'
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    kind: Mapped[str] = mapped_column(String(40), default='entry-v1')
    score: Mapped[int] = mapped_column(Integer)
    result: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    __table_args__ = (CheckConstraint('score BETWEEN 0 AND 100'),)


class WorkChecklistCompletion(Base):
    __tablename__ = 'work_checklist_completions'
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    shift_date: Mapped[date] = mapped_column(Date)
    item_key: Mapped[str] = mapped_column(String(80))
    done: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    __table_args__ = (UniqueConstraint('user_id','shift_date','item_key',name='uq_checklist_user_day_item'),)


class OperationRequest(Base):
    """Idempotency receipt, not another financial or attendance ledger."""
    __tablename__ = 'operation_requests'
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    request_key: Mapped[str] = mapped_column(String(80))
    payload_hash: Mapped[str] = mapped_column(String(64))
    result: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    __table_args__ = (UniqueConstraint('user_id','request_key',name='uq_operation_request_user_key'),)


class GuestFeedback(Base):
    __tablename__ = 'guest_feedback'
    id: Mapped[int] = mapped_column(primary_key=True)
    sale_id: Mapped[int] = mapped_column(ForeignKey('sales.id'),unique=True)
    token_hash: Mapped[str] = mapped_column(String(64),unique=True)
    rating: Mapped[int | None] = mapped_column(Integer,nullable=True)
    comment: Mapped[str] = mapped_column(Text,default='')
    created_at: Mapped[datetime] = mapped_column(DateTime,default=utc_now)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime,nullable=True)
    __table_args__ = (CheckConstraint('rating IS NULL OR rating BETWEEN 1 AND 5'),)


class ShootDevelopmentReview(Base):
    __tablename__ = 'shoot_development_reviews'
    id: Mapped[int] = mapped_column(primary_key=True)
    shooting_id: Mapped[int] = mapped_column(ForeignKey('shootings.id'), index=True)
    photographer_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    photo_ids: Mapped[str] = mapped_column(Text)
    analyzed: Mapped[str] = mapped_column(Text, default='[]')
    summary_parts: Mapped[str] = mapped_column(Text, default='[]', server_default='[]')
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default='PENDING', index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (UniqueConstraint('shooting_id','fingerprint',name='uq_shoot_review_fingerprint'),)


class SalesTrainingSession(Base):
    __tablename__ = 'sales_training_sessions'
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    client_type: Mapped[str] = mapped_column(String(60))
    transcript: Mapped[str] = mapped_column(Text, default='[]')
    status: Mapped[str] = mapped_column(String(20), default='ACTIVE')
    revision: Mapped[int] = mapped_column(Integer, default=1)
    evaluation: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
