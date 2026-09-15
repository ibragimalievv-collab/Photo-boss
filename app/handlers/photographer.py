from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy import func, select

from ..access import StaffFilter
from ..db import Session
from ..keyboards import inline
from ..models import Booking, Client, Hotel, PayrollEntry, Photo, Sale, Shooting
from ..services.core import ROLES, audit, get_user, has, roles_of

r = Router()
r.message.filter(StaffFilter("PHOTOGRAPHER"), F.text)
r.callback_query.filter(StaffFilter("PHOTOGRAPHER"))


@r.message(F.text == "📸 Мои съёмки")
async def shoots(m):
    async with Session() as s:
        u = await get_user(s, m.from_user.id)
        rs = await roles_of(s, u)
        if not has(rs, "PHOTOGRAPHER"):
            return
        q = await s.execute(
            select(Booking, Shooting, Hotel, Client)
            .join(Shooting, Shooting.booking_id == Booking.id)
            .join(Hotel, Hotel.id == Booking.hotel_id)
            .join(Client, Client.id == Booking.client_id)
            .where(Booking.photographer_id == u.id)
            .order_by(Booking.shoot_date, Booking.shoot_time)
        )
        rows = q.all()
        if not rows:
            return await m.answer("Съёмок нет.")
        for b, sh, h, c in rows:
            await m.answer(
                f"📸 Съёмка #{b.id}\n🏨 {h.name}\n🚪 {b.room}\n👤 {c.name}\n📅 {b.shoot_date} {b.shoot_time}\nСтатус: {sh.status}",
                reply_markup=inline(
                    [
                        [
                            ("▶️ Принять", f"accept:{sh.id}"),
                            ("📍 Прибыл", f"arrive:{sh.id}"),
                        ],
                        [
                            ("▶️ Начать", f"start:{sh.id}"),
                            ("✅ Готово", f"done:{sh.id}"),
                        ],
                    ]
                ),
            )


@r.callback_query(F.data.startswith(("accept:", "arrive:", "start:", "done:")))
async def action(c: CallbackQuery):
    try:
        act, raw_id = c.data.split(":", 1)
        sid = int(raw_id)
        if not 0 < sid <= 2**31 - 1:
            raise ValueError
    except (TypeError, ValueError):
        return await c.answer("Некорректная кнопка.")
    transitions = {
        "accept": ("ASSIGNED", "ACCEPTED", "accepted_at"),
        "arrive": ("ACCEPTED", "ARRIVED", "arrived_at"),
        "start": ("ARRIVED", "SHOOTING", "started_at"),
        "done": ("SHOOTING", "READY_FOR_MANAGER", "completed_at"),
    }
    async with Session() as s:
        u = await get_user(s, c.from_user.id)
        sh = (
            await s.execute(
                select(Shooting).where(Shooting.id == sid).with_for_update()
            )
        ).scalar_one_or_none()
        if sh is None:
            return await c.answer("Съёмка не найдена.")
        b = await s.get(Booking, sh.booking_id)
        if u is None or not u.active or b is None or b.photographer_id != u.id:
            return await c.answer("Это не ваша съёмка.")
        expected, target, timestamp = transitions[act]
        if sh.status != expected:
            return await c.answer(
                "Этот шаг уже выполнен или предыдущий ещё не завершён."
            )
        sh.status = target
        setattr(sh, timestamp, datetime.now(UTC).replace(tzinfo=None))
        if act == "done":
            b.status = target
        await audit(s, u, f"shooting_{act}", "shooting", sid)
        await s.commit()
        await c.message.answer("Готово: " + target)
        await c.answer()


@r.message(F.text == "📊 Моя статистика")
async def stats(m):
    async with Session() as s:
        u = await get_user(s, m.from_user.id)
        n = (
            await s.execute(
                select(func.count(Photo.id))
                .join(Shooting, Shooting.id == Photo.shooting_id)
                .join(Booking, Booking.id == Shooting.booking_id)
                .where(Booking.photographer_id == u.id)
            )
        ).scalar() or 0
        rev = (
            await s.execute(
                select(func.coalesce(func.sum(Sale.amount), 0)).where(
                    Sale.credited_user_id == u.id
                )
            )
        ).scalar() or 0
        await m.answer(f"📊 Статистика\nФото: {n}\nПродажи засчитаны: {rev:.2f} ₽")


@r.message(F.text == "💰 Мои выплаты")
async def payouts(m):
    async with Session() as s:
        u = await get_user(s, m.from_user.id)
        rows = (
            (
                await s.execute(
                    select(PayrollEntry)
                    .where(PayrollEntry.user_id == u.id)
                    .order_by(PayrollEntry.created_at.desc())
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )
        await m.answer(
            "\n".join(f"{x.period}: {x.kind} {x.amount:.2f} ₽" for x in rows)
            or "Выплат пока нет."
        )


@r.message(F.text == "👤 Профиль")
async def profile(m):
    async with Session() as s:
        u = await get_user(s, m.from_user.id)
        rs = await roles_of(s, u)
        await m.answer(
            f"👤 {u.name}\nID: {u.tg_id}\nРоли: {', '.join(ROLES[x] for x in rs)}"
        )
