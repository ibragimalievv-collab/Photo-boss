"""Manual test-period salary ledger; does not transfer money."""
from decimal import Decimal, InvalidOperation
from aiogram import Router, F
from aiogram.filters import Command
from sqlalchemy import select
from .db import Session
from .models import PayrollEntry, User
from .services.core import audit, get_user, roles_of

r = Router()
PERIOD = "14.09-27.09.2026"
ACCRUAL, PAYMENT = "manual_salary", "manual_salary_payment"

def amount_of(value):
    try:
        v = Decimal(value.replace(",", ".")).quantize(Decimal(".01"))
        if not v.is_finite() or not Decimal(0) < v <= Decimal(10000000):
            raise ValueError()
        return v
    except (InvalidOperation, ValueError):
        raise ValueError("Неверная сумма") from None

async def totals(db, uid):
    entries = (await db.scalars(select(PayrollEntry).where(
        PayrollEntry.user_id == uid, PayrollEntry.period == PERIOD,
        PayrollEntry.kind.in_([ACCRUAL, PAYMENT])))).all()
    due = sum((Decimal(str(e.amount)) for e in entries if e.kind == ACCRUAL), Decimal(0))
    paid = -sum((Decimal(str(e.amount)) for e in entries if e.kind == PAYMENT), Decimal(0))
    return due, paid

async def admin(db, message):
    actor = await get_user(db, message.from_user.id)
    return actor if await roles_of(db, actor) & {"OWNER", "ADMIN"} else None

@r.message(Command("salary_report"), F.chat.type == "private")
async def report(m):
    async with Session() as db:
        if not await admin(db, m):
            return await m.answer("Нет доступа")
        lines = []
        for u in (await db.scalars(select(User).where(User.active.is_(True)).order_by(User.name))).all():
            due, paid = await totals(db, u.id)
            if due or paid:
                lines.append(f"{u.name} (ID {u.id}): {due:.2f} / {paid:.2f} / {due-paid:.2f} ₽")
        await m.answer("Начислено / Выдано / Остаток\n" + ("\n".join(lines) or "Пока пусто"))

@r.message(Command("salary_set", "salary_paid"), F.chat.type == "private")
async def change(m):
    parts = (m.text or "").split()
    if len(parts) != 3:
        return await m.answer("Используйте: /salary_set ID СУММА или /salary_paid ID СУММА")
    try:
        uid, requested = int(parts[1]), amount_of(parts[2])
    except (ValueError, TypeError):
        return await m.answer("Неверный ID или сумма")
    async with Session() as db:
        actor = await admin(db, m)
        if actor is None:
            return await m.answer("Нет доступа")
        user = (await db.scalars(select(User).where(User.id == uid).with_for_update())).one_or_none()
        if user is None or not user.active:
            return await m.answer("Такого действующего сотрудника нет")
        due, paid = await totals(db, uid)
        setting = parts[0].split("@")[0] == "/salary_set"
        if setting and requested < paid:
            return await m.answer("Начисление меньше уже выданного")
        if not setting and requested > due-paid:
            return await m.answer("Выплата превышает остаток")
        delta = requested-due if setting else -requested
        if delta:
            entry = PayrollEntry(user_id=uid, period=PERIOD,
                kind=ACCRUAL if setting else PAYMENT, amount=float(delta),
                note="Ручная корректировка" if setting else "Ручная отметка выплаты")
            db.add(entry)
            await db.flush()
            await audit(db, actor, "manual_salary_change", "payroll_entry", entry.id,
                        f"employee_id={uid}; operation={parts[0]}; amount={requested}")
            await db.commit()
        final_due, final_paid = await totals(db, uid)
        await m.answer(f"{user.name}: начислено {final_due:.2f} ₽, выдано {final_paid:.2f} ₽, остаток {final_due-final_paid:.2f} ₽")

@r.message(Command("salary_me"), F.chat.type == "private")
async def my_salary(m):
    async with Session() as db:
        actor = await get_user(db, m.from_user.id)
        if not await roles_of(db, actor):
            return await m.answer("Нет доступа")
        due, paid = await totals(db, actor.id)
        await m.answer(f"Моя зарплата: {due:.2f} ₽; выдано {paid:.2f} ₽; остаток {due-paid:.2f} ₽")
