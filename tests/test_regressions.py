"""Behavior checks run with an isolated SQLite database and a fake Telegram session.
No real token, Telegram request or production database is used.
"""

import asyncio
import os
from datetime import date, datetime, time, timezone

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import AnswerCallbackQuery, SendMessage, SendPhoto
from aiogram.types import CallbackQuery, Chat, Message, Update
from aiogram.types import User as TelegramUser
from sqlalchemy import BigInteger, func, select
from sqlalchemy.dialects.postgresql import dialect
from sqlalchemy.schema import CreateTable

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["BOT_TOKEN"] = "123456:TEST_ONLY_DO_NOT_USE_FOR_TELEGRAM"
os.environ["ADMIN_TELEGRAM_IDS"] = "5000000001"
os.environ["PHOTOGRAPHER_PERCENT"] = "11"
os.environ["MANAGER_PERCENT"] = "16"

from app import db as db_module
from app import main as main_module
from app.config import Config
from app.db import Base, Session, engine, init_db
from app.handlers.admin import E
from app.handlers.sales import S
from app.main import create_dispatcher
from app.models import (
    AuditLog,
    Booking,
    Client,
    Compensation,
    Hotel,
    Package,
    Sale,
    Shooting,
    User,
    UserRole,
)
from app.services.core import bootstrap, get_user, menu, roles_of

OWNER, ADMIN, MANAGER, PHOTO_A, PHOTO_B, STRANGER = range(5000000001, 5000000007)


class FakeTelegramSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, (SendMessage, SendPhoto)):
            return Message(
                message_id=9000 + len(self.calls),
                date=datetime.now(timezone.utc),
                chat=Chat(id=int(method.chat_id), type="private"),
                text=getattr(method, "text", None),
                caption=getattr(method, "caption", None),
            )
        if isinstance(method, AnswerCallbackQuery):
            return True
        raise AssertionError(f"Unexpected Telegram method: {type(method).__name__}")

    async def stream_content(self, *args, **kwargs):
        raise AssertionError("Unexpected download")
        yield b""


telegram = FakeTelegramSession()
bot = Bot(os.environ["BOT_TOKEN"], session=telegram)
dp = create_dispatcher()
update_id = 0


def run(coroutine):
    return asyncio.run(coroutine)


def state_for(tg_id):
    return FSMContext(
        dp.storage, StorageKey(bot_id=bot.id, chat_id=tg_id, user_id=tg_id)
    )


async def message(tg_id, text):
    global update_id
    update_id += 1
    event = Message(
        message_id=update_id,
        date=datetime.now(timezone.utc),
        chat=Chat(id=tg_id, type="private"),
        from_user=TelegramUser(id=tg_id, first_name="Test", is_bot=False),
        text=text,
    )
    return await dp.feed_update(bot, Update(update_id=update_id, message=event))


async def callback(tg_id, data):
    global update_id
    update_id += 1
    event = CallbackQuery(
        id=str(update_id),
        from_user=TelegramUser(id=tg_id, first_name="Test", is_bot=False),
        chat_instance="test",
        data=data,
        message=Message(
            message_id=1,
            date=datetime.now(timezone.utc),
            chat=Chat(id=tg_id, type="private"),
        ),
    )
    return await dp.feed_update(bot, Update(update_id=update_id, callback_query=event))


@pytest.fixture(autouse=True)
def isolated_database():
    async def setup():
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await init_db()
        dp.storage.storage.clear()
        telegram.calls.clear()
        async with Session() as session:
            await bootstrap(session, OWNER, "Owner", None, True)
            for tg_id, role in [
                (ADMIN, "ADMIN"),
                (MANAGER, "MANAGER"),
                (PHOTO_A, "PHOTOGRAPHER"),
                (PHOTO_B, "PHOTOGRAPHER"),
            ]:
                user = await bootstrap(session, tg_id, role, None)
                session.add(UserRole(user_id=user.id, role=role))
                await session.commit()
            client = Client(name="Test client")
            session.add(client)
            await session.flush()
            hotel = (await session.execute(select(Hotel))).scalars().first()
            package = (await session.execute(select(Package))).scalars().first()
            manager = await get_user(session, MANAGER)
            photographer = await get_user(session, PHOTO_B)
            booking = Booking(
                hotel_id=hotel.id,
                client_id=client.id,
                room="101",
                shoot_date=date(2026, 9, 15),
                shoot_time=time(12),
                package_id=package.id,
                manager_id=manager.id,
                photographer_id=photographer.id,
            )
            session.add(booking)
            await session.flush()
            session.add(Shooting(booking_id=booking.id))
            await session.commit()

    run(setup())
    yield
    run(engine.dispose())


async def prepare_sale(creator, *, role="PHOTOGRAPHER"):
    async with Session() as session:
        credited = await get_user(session, PHOTO_B)
        booking = (await session.execute(select(Booking))).scalar_one()
    state = state_for(creator)
    await state.set_data({"booking": booking.id, "credited": credited.id, "role": role})
    await state.set_state(S.photos)


def test_start_is_idempotent_and_does_not_grant_staff_to_strangers():
    async def scenario():
        await message(OWNER, "/start")
        await message(OWNER, "/start")
        await message(STRANGER, "/start")
        async with Session() as session:
            owner = await get_user(session, OWNER)
            assert (
                await session.execute(
                    select(func.count(UserRole.id)).where(
                        UserRole.user_id == owner.id, UserRole.role == "OWNER"
                    )
                )
            ).scalar_one() == 1
            assert await roles_of(session, await get_user(session, STRANGER)) == set()

    run(scenario())


@pytest.mark.parametrize(
    "command",
    ["/add_employee", "/add_hotel", "/add_package", "/set MANAGER_PERCENT 90"],
)
def test_photographer_cannot_use_admin_commands(command):
    async def scenario():
        await message(PHOTO_A, command)
        assert await state_for(PHOTO_A).get_state() is None
        assert not telegram.calls

    run(scenario())


def test_admin_cannot_grant_admin_but_owner_can_repeat_grant():
    async def grant(actor):
        for text in ["/add_employee", str(PHOTO_A), "Photo A", "admin,photographer"]:
            await message(actor, text)

    async def scenario():
        await grant(ADMIN)
        async with Session() as session:
            assert "ADMIN" not in await roles_of(
                session, await get_user(session, PHOTO_A)
            )
        await grant(OWNER)
        await grant(OWNER)
        async with Session() as session:
            user = await get_user(session, PHOTO_A)
            assert await roles_of(session, user) == {"ADMIN", "PHOTOGRAPHER"}
            assert (
                await session.execute(
                    select(func.count(UserRole.id)).where(UserRole.user_id == user.id)
                )
            ).scalar_one() == 2

    run(scenario())


def test_fsm_permissions_are_rechecked_after_role_removal():
    async def scenario():
        await message(ADMIN, "/add_employee")
        await message(ADMIN, str(STRANGER))
        await message(ADMIN, "Stranger")
        assert await state_for(ADMIN).get_state() == E.roles.state
        async with Session() as session:
            user = await get_user(session, ADMIN)
            user.active = False
            await session.commit()
        await message(ADMIN, "MANAGER")
        async with Session() as session:
            assert await get_user(session, STRANGER) is None

    run(scenario())


def test_manager_and_photographer_screens_and_admin_sales_route():
    async def scenario():
        for tg_id, text in [
            (MANAGER, "📋 Мои записи"),
            (MANAGER, "📸 Съёмки"),
            (MANAGER, "📊 Статистика"),
            (PHOTO_B, "📸 Мои съёмки"),
            (PHOTO_B, "📊 Моя статистика"),
            (PHOTO_B, "👤 Профиль"),
            (OWNER, "💰 Продажи"),
        ]:
            before = len(telegram.calls)
            await message(tg_id, text)
            assert len(telegram.calls) > before
        assert telegram.calls[-1].text.startswith("💰 Продажи сети:")

    run(scenario())


@pytest.mark.parametrize("count", ["0", "-5", "text", "1.5", "2147483648"])
def test_invalid_sale_quantity_is_rejected(count):
    async def scenario():
        await prepare_sale(PHOTO_A)
        await message(PHOTO_A, count)
        async with Session() as session:
            assert (
                await session.execute(select(func.count(Sale.id)))
            ).scalar_one() == 0

    run(scenario())


@pytest.mark.parametrize("creator", [PHOTO_A, MANAGER, ADMIN, OWNER])
def test_sale_credits_other_photographer_and_uses_database_percent(creator):
    async def scenario():
        await message(OWNER, "/set PHOTOGRAPHER_PERCENT 12.5")
        # Exercise the complete FSM, not just its final function.
        for text in ["🧾 Продажа", "1", str(PHOTO_B), "PHOTOGRAPHER", "2"]:
            await message(creator, text)
        async with Session() as session:
            sale = (await session.execute(select(Sale))).scalar_one()
            assert sale.created_by_id == (await get_user(session, creator)).id
            assert sale.credited_user_id == (await get_user(session, PHOTO_B)).id
            assert sale.amount == 800
            assert sale.percent == 12.5
            assert sale.commission == 100
            assert (
                await session.execute(
                    select(func.count(AuditLog.id)).where(
                        AuditLog.action == "sale_created"
                    )
                )
            ).scalar_one() == 1
        await message(PHOTO_B, "📊 Моя статистика")
        assert "Продажи засчитаны: 800.00" in telegram.calls[-1].text
        if creator == PHOTO_A:
            await message(PHOTO_A, "📊 Моя статистика")
            assert "Продажи засчитаны: 0.00" in telegram.calls[-1].text
        assert await state_for(creator).get_state() is None

    run(scenario())


def test_employee_percent_takes_precedence_and_forged_role_is_rejected():
    async def scenario():
        async with Session() as session:
            credited = await get_user(session, PHOTO_B)
            session.add(
                Compensation(user_id=credited.id, role="PHOTOGRAPHER", sales_percent=23)
            )
            await session.commit()
        await prepare_sale(PHOTO_A, role="MANAGER")
        await message(PHOTO_A, "2")
        async with Session() as session:
            assert (
                await session.execute(select(func.count(Sale.id)))
            ).scalar_one() == 0
        await prepare_sale(PHOTO_A)
        await message(PHOTO_A, "2")
        async with Session() as session:
            sale = (await session.execute(select(Sale))).scalar_one()
            assert sale.commission == 184

    run(scenario())


def test_disabled_credited_employee_is_rechecked_at_save():
    async def scenario():
        await prepare_sale(PHOTO_A)
        async with Session() as session:
            user = await get_user(session, PHOTO_B)
            user.active = False
            await session.commit()
        await message(PHOTO_A, "2")
        async with Session() as session:
            assert (
                await session.execute(select(func.count(Sale.id)))
            ).scalar_one() == 0

    run(scenario())


def test_shooting_order_ownership_and_repeated_clicks():
    async def scenario():
        await callback(PHOTO_A, "accept:1")
        await callback(PHOTO_B, "done:1")
        async with Session() as session:
            assert (await session.get(Shooting, 1)).status == "ASSIGNED"
        await callback(PHOTO_B, "accept:1")
        async with Session() as session:
            first_time = (await session.get(Shooting, 1)).accepted_at
        await callback(PHOTO_B, "accept:1")
        for action in ["arrive", "start", "done"]:
            await callback(PHOTO_B, f"{action}:1")
        async with Session() as session:
            shoot = await session.get(Shooting, 1)
            assert shoot.status == "READY_FOR_MANAGER"
            assert shoot.accepted_at == first_time
            assert (
                await session.execute(select(func.count(AuditLog.id)))
            ).scalar_one() == 4

    run(scenario())


@pytest.mark.parametrize("value", ["nan", "inf", "-1"])
def test_package_prices_reject_invalid_numbers(value):
    async def scenario():
        for text in ["/add_package", "New package", value]:
            await message(OWNER, text)
        async with Session() as session:
            assert (
                await session.execute(select(func.count(Package.id)))
            ).scalar_one() == 1
        await message(OWNER, "/cancel")
        assert await state_for(OWNER).get_state() is None

    run(scenario())


def test_postgres_telegram_id_uses_bigint():
    assert isinstance(User.__table__.c.tg_id.type, BigInteger)
    assert "tg_id BIGINT" in str(CreateTable(User.__table__).compile(dialect=dialect()))


def test_configuration_rejects_invalid_ids_and_encodes_database_password(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("POSTGRES_PASSWORD", "test:@/#%")
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", f" {OWNER}, {ADMIN} ")
    config = Config.from_env()
    config.validate()
    from sqlalchemy.engine import make_url

    assert make_url(config.database_url).password == "test:@/#%"
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "abc")
    with pytest.raises(ValueError, match="ADMIN_TELEGRAM_IDS"):
        Config.from_env()


def test_configuration_rejects_invalid_port_and_telegram_api(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("POSTGRES_PORT", "70000")
    with pytest.raises(ValueError, match="POSTGRES_PORT"):
        Config.from_env()

    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://postgres:password@postgres/database"
    )
    monkeypatch.setenv("TELEGRAM_API_BASE", "ftp://example.invalid")
    with pytest.raises(ValueError, match="TELEGRAM_API_BASE"):
        Config.from_env().validate()


def test_database_wait_retries_transient_failure(monkeypatch):
    attempts = 0

    async def transient_ping():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise OSError("temporary test failure")
        return True

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(db_module, "ping_database", transient_ping)
    monkeypatch.setattr(db_module.asyncio, "sleep", no_sleep)
    run(db_module.wait_for_database(attempts=3, delay=0))
    assert attempts == 3


def test_ready_file_lifecycle(monkeypatch, tmp_path):
    ready_file = tmp_path / "ready"
    monkeypatch.setattr(main_module, "READY_FILE", ready_file)
    main_module.mark_ready("photo_boss_test_bot")
    assert ready_file.read_text(encoding="utf-8") == "photo_boss_test_bot"
    main_module.clear_ready_file()
    assert not ready_file.exists()


def test_menu_merges_roles_without_duplicates():
    items = menu({"ADMIN", "MANAGER", "PHOTOGRAPHER"})
    assert len(items) == len(set(items))
    assert "🎓 Обучение" in items
    assert "🧾 Продажа" in items
    assert "📋 Мои записи" in items
    assert "📸 Мои съёмки" in items
    assert "➕ Добавить сотрудника" in items


def test_owner_can_start_employee_creation_from_menu_button():
    async def scenario():
        await message(OWNER, "➕ Добавить сотрудника")
        assert await state_for(OWNER).get_state() == E.tg.state
        assert telegram.calls[-1].text.startswith("Telegram ID сотрудника")

    run(scenario())


@pytest.mark.parametrize("tg_id", [OWNER, ADMIN, MANAGER, PHOTO_A])
def test_training_is_available_to_every_staff_role(tg_id):
    async def scenario():
        before = len(telegram.calls)
        await message(tg_id, "🎓 Обучение")
        assert len(telegram.calls) == before + 1
        assert telegram.calls[-1].text.startswith("🎓 Обучение")
        assert "эталонный кадр" in telegram.calls[-1].text

    run(scenario())


def test_training_category_sends_a_real_reference_photo():
    async def scenario():
        await callback(OWNER, "training:family")
        photo_calls = [call for call in telegram.calls if isinstance(call, SendPhoto)]
        assert len(photo_calls) == 1
        assert photo_calls[0].photo.path.name == "family-lifestyle.jpg"
        assert "Повторите этот кадр" in photo_calls[0].caption

    run(scenario())
