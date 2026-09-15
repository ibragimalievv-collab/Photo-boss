from sqlalchemy import select

from ..config import config
from ..models import AuditLog, Hotel, Package, Setting, User, UserRole

ROLES = {
    "OWNER": "👑 Владелец",
    "ADMIN": "🛠 Администратор",
    "MANAGER": "📋 Менеджер",
    "PHOTOGRAPHER": "📸 Фотограф",
}


def has(roles, role):
    return role in roles or bool({"OWNER", "ADMIN"} & roles)


def menu(roles):
    items = (
        [
            "📸 Мои съёмки",
            "🔄 Моя смена",
            "📊 Моя статистика",
            "💰 Мои выплаты",
            "👤 Профиль",
        ]
        if "PHOTOGRAPHER" in roles
        else []
    )
    if "MANAGER" in roles:
        items += [
            "➕ Новая запись",
            "📋 Мои записи",
            "📸 Съёмки",
            "💰 Продажи",
            "📊 Статистика",
            "🏆 Премия",
        ]
    if {"ADMIN", "OWNER"} & roles:
        items += [
            "👥 Сотрудники",
            "🏨 Отели",
            "📋 Все записи",
            "📸 Все съёмки",
            "💰 Продажи",
            "💵 Зарплаты/выплаты",
            "📊 Отчёты",
            "📦 Пакеты",
            "⚙️ Настройки",
            "📜 Аудит",
        ]
    if set(ROLES) & roles:
        items += ["🧾 Продажа"]
    return list(dict.fromkeys(items))


async def get_user(session, tg):
    return (
        await session.execute(select(User).where(User.tg_id == tg))
    ).scalar_one_or_none()


async def roles_of(session, user):
    if user is None or not user.active:
        return set()
    return set(
        (
            await session.execute(
                select(UserRole.role).where(UserRole.user_id == user.id)
            )
        ).scalars()
    )


async def audit(session, user, action, entity=None, eid=None, details=None):
    # The caller commits the business change and audit record together.
    session.add(
        AuditLog(
            user_id=user.id if user else None,
            action=action,
            entity=entity,
            entity_id=eid,
            details=details,
        )
    )


async def setting(session, key, default):
    value = await session.get(Setting, key)
    return value.value if value else default


async def bootstrap(session, tg, name, username, owner=False):
    user = await get_user(session, tg)
    if user is None:
        user = User(tg_id=tg, name=name[:150], username=username)
        session.add(user)
        await session.flush()
    else:
        user.name = name[:150]
        user.username = username
    if user.active and owner and "OWNER" not in await roles_of(session, user):
        session.add(UserRole(user_id=user.id, role="OWNER"))
    # A stranger receives no staff role; an administrator assigns it explicitly.
    if owner and user.active:
        if (
            await session.execute(select(Package.id).limit(1))
        ).scalar_one_or_none() is None:
            price = float(await setting(session, "PHOTO_PRICE", config.photo_price))
            session.add(Package(name="Базовая", price_per_photo=price))
        if (
            await session.execute(select(Hotel.id).limit(1))
        ).scalar_one_or_none() is None:
            session.add(Hotel(name="Основной отель"))
    await session.commit()
    return user
