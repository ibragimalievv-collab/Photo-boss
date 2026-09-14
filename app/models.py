from datetime import datetime, date, time
from sqlalchemy import String, Integer, BigInteger, Float, Boolean, DateTime, Date, Time, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .db import Base
class User(Base):
    __tablename__='users'
    id: Mapped[int]=mapped_column(primary_key=True); tg_id: Mapped[int]=mapped_column(BigInteger,unique=True,index=True)
    name: Mapped[str]=mapped_column(String(150)); username: Mapped[str|None]=mapped_column(String(150),nullable=True)
    active: Mapped[bool]=mapped_column(Boolean,default=True); created_at: Mapped[datetime]=mapped_column(DateTime,default=datetime.utcnow)
    roles=relationship('UserRole',back_populates='user',cascade='all, delete-orphan')
class UserRole(Base):
    __tablename__='user_roles'; id: Mapped[int]=mapped_column(primary_key=True); user_id: Mapped[int]=mapped_column(ForeignKey('users.id',ondelete='CASCADE')); role: Mapped[str]=mapped_column(String(30)); user=relationship('User',back_populates='roles'); __table_args__=(UniqueConstraint('user_id','role'),)
class Hotel(Base):
    __tablename__='hotels'; id: Mapped[int]=mapped_column(primary_key=True); name: Mapped[str]=mapped_column(String(200)); address: Mapped[str|None]=mapped_column(String(300),nullable=True); active: Mapped[bool]=mapped_column(Boolean,default=True)
class HotelEmployee(Base):
    __tablename__='hotel_employees'; id: Mapped[int]=mapped_column(primary_key=True); hotel_id: Mapped[int]=mapped_column(ForeignKey('hotels.id',ondelete='CASCADE')); user_id: Mapped[int]=mapped_column(ForeignKey('users.id',ondelete='CASCADE'))
class Client(Base):
    __tablename__='clients'; id: Mapped[int]=mapped_column(primary_key=True); name: Mapped[str]=mapped_column(String(200)); phone: Mapped[str|None]=mapped_column(String(80),nullable=True); notes: Mapped[str|None]=mapped_column(Text,nullable=True); created_at: Mapped[datetime]=mapped_column(DateTime,default=datetime.utcnow)
class Package(Base):
    __tablename__='packages'; id: Mapped[int]=mapped_column(primary_key=True); name: Mapped[str]=mapped_column(String(150)); price_per_photo: Mapped[float]=mapped_column(Float); active: Mapped[bool]=mapped_column(Boolean,default=True)
class Booking(Base):
    __tablename__='bookings'; id: Mapped[int]=mapped_column(primary_key=True); hotel_id: Mapped[int]=mapped_column(ForeignKey('hotels.id')); client_id: Mapped[int]=mapped_column(ForeignKey('clients.id')); room: Mapped[str]=mapped_column(String(100)); shoot_date: Mapped[date]=mapped_column(Date); shoot_time: Mapped[time]=mapped_column(Time); package_id: Mapped[int]=mapped_column(ForeignKey('packages.id')); manager_id: Mapped[int]=mapped_column(ForeignKey('users.id')); photographer_id: Mapped[int|None]=mapped_column(ForeignKey('users.id'),nullable=True); status: Mapped[str]=mapped_column(String(40),default='NEW'); created_at: Mapped[datetime]=mapped_column(DateTime,default=datetime.utcnow)
class Shooting(Base):
    __tablename__='shootings'; id: Mapped[int]=mapped_column(primary_key=True); booking_id: Mapped[int]=mapped_column(ForeignKey('bookings.id',ondelete='CASCADE'),unique=True); accepted_at: Mapped[datetime|None]=mapped_column(DateTime,nullable=True); arrived_at: Mapped[datetime|None]=mapped_column(DateTime,nullable=True); started_at: Mapped[datetime|None]=mapped_column(DateTime,nullable=True); completed_at: Mapped[datetime|None]=mapped_column(DateTime,nullable=True); status: Mapped[str]=mapped_column(String(40),default='ASSIGNED'); lat: Mapped[float|None]=mapped_column(Float,nullable=True); lon: Mapped[float|None]=mapped_column(Float,nullable=True)
class Photo(Base):
    __tablename__='photos'; id: Mapped[int]=mapped_column(primary_key=True); shooting_id: Mapped[int]=mapped_column(ForeignKey('shootings.id',ondelete='CASCADE')); file_id: Mapped[str]=mapped_column(String(300)); created_at: Mapped[datetime]=mapped_column(DateTime,default=datetime.utcnow)
class Sale(Base):
    __tablename__='sales'; id: Mapped[int]=mapped_column(primary_key=True); booking_id: Mapped[int|None]=mapped_column(ForeignKey('bookings.id'),nullable=True); created_by_id: Mapped[int]=mapped_column(ForeignKey('users.id')); credited_user_id: Mapped[int]=mapped_column(ForeignKey('users.id')); commission_role: Mapped[str]=mapped_column(String(30),default='PHOTOGRAPHER'); sold_photos: Mapped[int]=mapped_column(Integer); amount: Mapped[float]=mapped_column(Float); percent: Mapped[float]=mapped_column(Float,default=0); commission: Mapped[float]=mapped_column(Float,default=0); payment_status: Mapped[str]=mapped_column(String(20),default='UNPAID'); created_at: Mapped[datetime]=mapped_column(DateTime,default=datetime.utcnow)
class Compensation(Base):
    __tablename__='compensation'; id: Mapped[int]=mapped_column(primary_key=True); user_id: Mapped[int]=mapped_column(ForeignKey('users.id',ondelete='CASCADE')); role: Mapped[str]=mapped_column(String(30)); base_salary: Mapped[float]=mapped_column(Float,default=0); sales_percent: Mapped[float]=mapped_column(Float,default=0)
class PayrollEntry(Base):
    __tablename__='payroll_entries'; id: Mapped[int]=mapped_column(primary_key=True); user_id: Mapped[int]=mapped_column(ForeignKey('users.id')); kind: Mapped[str]=mapped_column(String(30)); amount: Mapped[float]=mapped_column(Float); period: Mapped[str]=mapped_column(String(30)); note: Mapped[str|None]=mapped_column(Text,nullable=True); created_at: Mapped[datetime]=mapped_column(DateTime,default=datetime.utcnow)
class Shift(Base):
    __tablename__='shifts'; id: Mapped[int]=mapped_column(primary_key=True); user_id: Mapped[int]=mapped_column(ForeignKey('users.id')); hotel_id: Mapped[int]=mapped_column(ForeignKey('hotels.id')); start_at: Mapped[datetime]=mapped_column(DateTime); end_at: Mapped[datetime]=mapped_column(DateTime); status: Mapped[str]=mapped_column(String(20),default='PLANNED')
class SalesPlan(Base):
    __tablename__='sales_plans'; id: Mapped[int]=mapped_column(primary_key=True); user_id: Mapped[int|None]=mapped_column(ForeignKey('users.id'),nullable=True); hotel_id: Mapped[int|None]=mapped_column(ForeignKey('hotels.id'),nullable=True); period: Mapped[str]=mapped_column(String(30)); target_amount: Mapped[float]=mapped_column(Float)
class Notification(Base):
    __tablename__='notifications'; id: Mapped[int]=mapped_column(primary_key=True); user_id: Mapped[int]=mapped_column(ForeignKey('users.id')); text: Mapped[str]=mapped_column(Text); sent: Mapped[bool]=mapped_column(Boolean,default=False); created_at: Mapped[datetime]=mapped_column(DateTime,default=datetime.utcnow)
class Setting(Base):
    __tablename__='settings'; key: Mapped[str]=mapped_column(String(100),primary_key=True); value: Mapped[str]=mapped_column(Text)
class AuditLog(Base):
    __tablename__='audit_logs'; id: Mapped[int]=mapped_column(primary_key=True); user_id: Mapped[int|None]=mapped_column(ForeignKey('users.id'),nullable=True); action: Mapped[str]=mapped_column(String(100)); entity: Mapped[str|None]=mapped_column(String(50),nullable=True); entity_id: Mapped[int|None]=mapped_column(Integer,nullable=True); details: Mapped[str|None]=mapped_column(Text,nullable=True); created_at: Mapped[datetime]=mapped_column(DateTime,default=datetime.utcnow)
