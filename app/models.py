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


class AcademyLesson(Base):
    __tablename__ = "academy_lessons"
    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    category: Mapped[str] = mapped_column(String(50), index=True)
    title: Mapped[str] = mapped_column(String(250))
    description: Mapped[str] = mapped_column(Text)
    bad_example: Mapped[str | None] = mapped_column(Text, nullable=True)
    good_example: Mapped[str | None] = mapped_column(Text, nullable=True)
    advice: Mapped[str | None] = mapped_column(Text, nullable=True)
    level: Mapped[str] = mapped_column(String(50), default="Начальный")
    photo_file_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    video_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class AcademyProgress(Base):
    __tablename__ = "academy_progress"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    lesson_id: Mapped[int] = mapped_column(ForeignKey("academy_lessons.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(30), default="STARTED")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (UniqueConstraint("user_id", "lesson_id", name="uq_academy_progress_user_lesson"),)


class AcademyPractice(Base):
    __tablename__ = "academy_practices"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    lesson_id: Mapped[int] = mapped_column(ForeignKey("academy_lessons.id", ondelete="CASCADE"), index=True)
    task_text: Mapped[str] = mapped_column(Text)
    photo_file_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", index=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class AcademyReview(Base):
    __tablename__ = "academy_reviews"
    id: Mapped[int] = mapped_column(primary_key=True)
    photographer_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    shooting_id: Mapped[int | None] = mapped_column(ForeignKey("shootings.id", ondelete="SET NULL"), unique=True, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="PENDING", index=True)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    strengths: Mapped[str | None] = mapped_column(Text, nullable=True)
    issues: Mapped[str | None] = mapped_column(Text, nullable=True)
    recommended_lesson_id: Mapped[int | None] = mapped_column(ForeignKey("academy_lessons.id", ondelete="SET NULL"), nullable=True)
    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class AcademyAchievement(Base):
    __tablename__ = "academy_achievements"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(100), unique=True)
    title: Mapped[str] = mapped_column(String(250))
    condition: Mapped[str] = mapped_column(Text)
    reward: Mapped[str | None] = mapped_column(Text, nullable=True)


class AcademyUserAchievement(Base):
    __tablename__ = "academy_user_achievements"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    achievement_id: Mapped[int] = mapped_column(ForeignKey("academy_achievements.id", ondelete="CASCADE"), index=True)
    earned_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    __table_args__ = (UniqueConstraint("user_id", "achievement_id", name="uq_academy_user_achievement"),)


class AcademyBestWork(Base):
    __tablename__ = "academy_best_works"
    id: Mapped[int] = mapped_column(primary_key=True)
    category: Mapped[str] = mapped_column(String(50), index=True)
    title: Mapped[str] = mapped_column(String(250))
    photo_file_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    shooting_id: Mapped[int | None] = mapped_column(ForeignKey("shootings.id", ondelete="SET NULL"), nullable=True)
    author_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class GuestSelectedPhoto(Base):
    __tablename__ = "guest_selected_photos"
    id: Mapped[int] = mapped_column(primary_key=True)
    shooting_id: Mapped[int] = mapped_column(
        ForeignKey("shootings.id", ondelete="CASCADE"), index=True
    )
    file_id: Mapped[str] = mapped_column(String(300))
    selected_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    __table_args__ = (
        UniqueConstraint(
            "shooting_id", "file_id", name="uq_guest_selected_photo_per_shooting"
        ),
    )
