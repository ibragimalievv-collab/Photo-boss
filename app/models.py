from datetime import UTC, date, datetime, time

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
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
    amount: Mapped[float] = mapped_column(Float)
    percent: Mapped[float] = mapped_column(Float, default=0)
    commission: Mapped[float] = mapped_column(Float, default=0)
    payment_status: Mapped[str] = mapped_column(String(20), default="UNPAID")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


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
    __table_args__ = (
        UniqueConstraint("user_id", "shift_date", name="uq_shift_check_out_user_day"),
    )


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
    submissions = relationship(
        "TrainingSubmission",
        back_populates="assignment",
        cascade="all, delete-orphan",
        order_by="TrainingSubmission.pose_index",
    )
    __table_args__ = (
        UniqueConstraint("user_id", "assigned_date", name="uq_training_user_day"),
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


# =========================
# Photo Boss Academy
# =========================

class AcademyLesson(Base):
    __tablename__ = "academy_lessons"
    id: Mapped[int] = mapped_column(primary_key=True)
    category: Mapped[str] = mapped_column(String(40), index=True)
    title: Mapped[str] = mapped_column(String(180))
    body: Mapped[str] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class AcademyLessonProgress(Base):
    __tablename__ = "academy_lesson_progress"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    lesson_id: Mapped[int] = mapped_column(ForeignKey("academy_lessons.id", ondelete="CASCADE"), index=True)
    completed_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    __table_args__ = (UniqueConstraint("user_id", "lesson_id", name="uq_academy_lesson_user"),)


class AcademyPracticeTask(Base):
    __tablename__ = "academy_practice_tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    category: Mapped[str] = mapped_column(String(40), index=True)
    title: Mapped[str] = mapped_column(String(180))
    description: Mapped[str] = mapped_column(Text)
    strong_example_text: Mapped[str] = mapped_column(Text)
    common_mistake_text: Mapped[str] = mapped_column(Text)
    repeat_tip: Mapped[str] = mapped_column(Text)
    points: Mapped[int] = mapped_column(Integer, default=100)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class AcademyReview(Base):
    __tablename__ = "academy_reviews"
    id: Mapped[int] = mapped_column(primary_key=True)
    photographer_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    telegram_file_id: Mapped[str] = mapped_column(String(300))
    source_type: Mapped[str] = mapped_column(String(30), default="work", index=True)
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    composition_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    light_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    emotion_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pose_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    overall_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    good_points_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AcademyError(Base):
    __tablename__ = "academy_errors"
    id: Mapped[int] = mapped_column(primary_key=True)
    photographer_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    review_id: Mapped[int] = mapped_column(ForeignKey("academy_reviews.id", ondelete="CASCADE"), index=True)
    category: Mapped[str] = mapped_column(String(40), index=True)
    title: Mapped[str] = mapped_column(String(180))
    recommendation: Mapped[str] = mapped_column(Text)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)


class AcademyPracticeSubmission(Base):
    __tablename__ = "academy_practice_submissions"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("academy_practice_tasks.id", ondelete="CASCADE"), index=True)
    photographer_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    pose_index: Mapped[int] = mapped_column(Integer)
    telegram_file_id: Mapped[str] = mapped_column(String(300))
    review_id: Mapped[int | None] = mapped_column(ForeignKey("academy_reviews.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)
    __table_args__ = (
        UniqueConstraint("task_id", "photographer_id", "pose_index", name="uq_academy_task_user_pose"),
        CheckConstraint("pose_index BETWEEN 1 AND 5", name="ck_academy_pose_index"),
    )


class AcademyBestWork(Base):
    __tablename__ = "academy_best_works"
    id: Mapped[int] = mapped_column(primary_key=True)
    category: Mapped[str] = mapped_column(String(40), index=True)
    telegram_file_id: Mapped[str] = mapped_column(String(300))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    added_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)


class AcademyAchievement(Base):
    __tablename__ = "academy_achievements"
    id: Mapped[int] = mapped_column(primary_key=True)
    photographer_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(String(80), index=True)
    unlocked_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    __table_args__ = (UniqueConstraint("photographer_id", "code", name="uq_academy_achievement_user"),)
