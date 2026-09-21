"""Behavior checks run with an isolated SQLite database and a fake Telegram session.
No real token, Telegram request or production database is used.
"""

import asyncio
import hashlib
import json
import os
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import AnswerCallbackQuery, SendMessage, SendPhoto
from aiogram.types import CallbackQuery, Chat, Location, Message, PhotoSize, Update
from aiogram.types import User as TelegramUser
from sqlalchemy import BigInteger, func, select
from sqlalchemy.dialects.postgresql import dialect
from sqlalchemy.schema import CreateTable

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["BOT_TOKEN"] = "123456:TEST_ONLY_DO_NOT_USE_FOR_TELEGRAM"
os.environ["ADMIN_TELEGRAM_IDS"] = "5000000001"
os.environ["PHOTOGRAPHER_PERCENT"] = "11"
os.environ["MANAGER_PERCENT"] = "16"
os.environ["OPENAI_API_KEY"] = ""

from app import db as db_module
from app import main as main_module
from app.config import Config
from app.db import Base, Session, engine, init_db
from app.handlers import photographer as photographer_module
from app.handlers import receipts as receipts_module
from app.handlers.admin import DateLookup, E
from app.handlers.manager import BookingFlow
from app.handlers.photographer import PhotoUploadFlow, ShiftFlow
from app.handlers.receipts import ReceiptFlow
from app.handlers.sales import S
from app.main import create_dispatcher
from app.models import (
    AuditLog,
    Booking,
    Client,
    Hotel,
    Package,
    PayrollEntry,
    Photo,
    Receipt,
    Sale,
    SaleDraft,
    SaleDraftPhoto,
    ShiftCheckIn,
    ShiftCheckOut,
    Shooting,
    TrainingAssignment,
    TrainingSubmission,
    User,
    UserRole,
)
from app.services.core import bootstrap, get_user, menu, roles_of
from app.services.receipts import payment_totals, refresh_payment_statuses, warnings_for
from app.services.sale_workflow import finalize_photographer_commissions
from app.services.training import TRAINING_CATEGORIES, training_day

OWNER, ADMIN, MANAGER, PHOTO_A, PHOTO_B, STRANGER = range(5000000001, 5000000007)


@pytest.fixture
def receipt_api(monkeypatch):
    async def download(bot, photo):
        return b"test jpeg " + photo.file_id.encode()

    async def extract(content):
        return {"status": "not_configured"}

    monkeypatch.setattr(receipts_module, "download_receipt", download)
    monkeypatch.setattr(receipts_module, "extract_receipt", extract)


async def approve_receipt(receipt_id, amount):
    await callback(OWNER, f"receipt:amount:{receipt_id}")
    await message(OWNER, str(amount))
    await callback(OWNER, f"receipt:approve:{receipt_id}")


async def attach_receipt(purpose, file_id, user=MANAGER):
    await callback(user, f"receipt:upload:{purpose}:1")
    await photo(user, file_id)
    async with Session() as session:
        return await session.scalar(select(Receipt).order_by(Receipt.id.desc()).limit(1))


def test_receipt_deposit_and_payment_are_counted_once_and_owner_only(receipt_api):
    async def scenario():
        await prepare_sale(PHOTO_A)
        await message(PHOTO_A, "2")  # 800 rubles
        async with Session() as session:
            booking = await session.get(Booking, 1)
            booking.deposit = 200
            await session.commit()
        deposit = await attach_receipt("DEPOSIT", "deposit")
        assert deposit.expected_amount == 200
        assert deposit.status == "PENDING"
        for user in [MANAGER, ADMIN, PHOTO_B, STRANGER]:
            await callback(user, f"receipt:review:{deposit.id}")
            await callback(user, f"receipt:reject:{deposit.id}")
            await callback(user, f"receipt:amount:{deposit.id}")
            await callback(user, f"receipt:approve:{deposit.id}")
            assert not any(isinstance(call, SendPhoto) and call.chat_id == user
                           for call in telegram.calls)
        async with Session() as session:
            assert (await session.get(Receipt, deposit.id)).status == "PENDING"
            assert (await session.get(Sale, 1)).payment_status == "UNPAID"
        await message(OWNER, "🔎 Проверить чеки")
        await callback(OWNER, f"receipt:review:{deposit.id}")
        assert any(isinstance(call, SendPhoto) and call.photo == "deposit"
                   for call in telegram.calls)
        await approve_receipt(deposit.id, 200)
        async with Session() as session:
            assert (await session.get(Sale, 1)).payment_status == "PARTIAL"
            assert await payment_totals(session, 1) == (800, 200, 600)
        payment = await attach_receipt("PAYMENT", "payment")
        assert payment.expected_amount == 600
        await approve_receipt(payment.id, 600)
        await callback(OWNER, f"receipt:approve:{payment.id}")
        await callback(OWNER, f"receipt:reject:{payment.id}")
        async with Session() as session:
            assert await payment_totals(session, 1) == (800, 800, 0)
            assert (await session.get(Sale, 1)).payment_status == "PAID"
            assert await session.scalar(select(func.count(AuditLog.id)).where(
                AuditLog.action == "receipt_approved"
            )) == 2
        await callback(MANAGER, "receipt:upload:PAYMENT:1")
        assert "не требуется" in telegram.calls[-1].text

    run(scenario())


def test_receipt_deposit_is_shared_across_multiple_sales(receipt_api):
    async def scenario():
        async with Session() as session:
            booking = await session.get(Booking, 1)
            booking.deposit = 1000
            await session.commit()
        deposit = await attach_receipt("DEPOSIT", "large-deposit")
        await approve_receipt(deposit.id, 1000)
        await prepare_sale(PHOTO_A)
        await message(PHOTO_A, "2")
        await prepare_sale(PHOTO_A, sold_photos=1)
        await message(PHOTO_A, "1")
        async with Session() as session:
            sales = (await session.scalars(select(Sale).order_by(Sale.id))).all()
            assert [sale.payment_status for sale in sales] == ["PAID", "PARTIAL"]
            assert await payment_totals(session, 1) == (1200, 1000, 200)

    run(scenario())


@pytest.mark.parametrize("duplicate_kind", ["file", "hash", "operation"])
def test_receipt_copy_cannot_confirm_same_payment_twice(receipt_api, monkeypatch, duplicate_kind):
    fields = {"is_receipt": True, "amount": "200", "currency": "RUB",
              "date": "2026-09-18", "bank": "Bank", "recipient": "Recipient",
              "operation_id": "bank-operation-1", "payment_status": "Выполнен", "concerns": []}

    async def same_download(*args):
        return b"same receipt bytes"

    async def same_operation(*args):
        return {"status": "extracted", "fields": fields}

    if duplicate_kind == "hash":
        monkeypatch.setattr(receipts_module, "download_receipt", same_download)
    if duplicate_kind == "operation":
        monkeypatch.setattr(receipts_module, "extract_receipt", same_operation)

    async def scenario():
        await prepare_sale(PHOTO_A)
        await message(PHOTO_A, "2")
        first = await attach_receipt("PAYMENT", "receipt-original")
        await approve_receipt(first.id, 200)
        second_file = "receipt-original" if duplicate_kind == "file" else "receipt-copy"
        second = await attach_receipt("PAYMENT", second_file)
        if duplicate_kind == "file":
            assert "уже загружали" in telegram.calls[-1].text
            assert second.id == first.id
        else:
            assert "похожий" in telegram.calls[-1].text
            await approve_receipt(second.id, 200)
            assert "уже учтён" in telegram.calls[-1].text
        async with Session() as session:
            assert await payment_totals(session, 1) == (800, 200, 600)

    run(scenario())


def test_receipt_failed_analysis_and_rejection_preserve_attachment(receipt_api, monkeypatch):
    async def unavailable(*args):
        raise TimeoutError

    monkeypatch.setattr(receipts_module, "download_receipt", unavailable)

    async def scenario():
        await prepare_sale(PHOTO_A)
        await message(PHOTO_A, "2")
        receipt = await attach_receipt("PAYMENT", "offline-receipt")
        assert receipt.file_id == "offline-receipt"
        assert receipt.status == "PENDING"
        assert json.loads(receipt.analysis)["status"] == "download_failed"
        await callback(OWNER, f"receipt:reject:{receipt.id}")
        async with Session() as session:
            assert (await session.get(Receipt, receipt.id)).status == "REJECTED"
            assert (await session.get(Sale, 1)).payment_status == "UNPAID"
        replacement = await attach_receipt("PAYMENT", "replacement")
        assert replacement.id != receipt.id
        assert replacement.status == "PENDING"

    run(scenario())


def test_receipt_incomplete_analysis_can_be_retried_after_restart(receipt_api):
    async def scenario():
        await prepare_sale(PHOTO_A)
        await message(PHOTO_A, "2")
        receipt = await attach_receipt("PAYMENT", "interrupted")
        async with Session() as session:
            saved = await session.get(Receipt, receipt.id)
            saved.analysis = None
            await session.commit()
        await approve_receipt(receipt.id, 800)
        assert "Дождитесь анализа" in telegram.calls[-1].text
        await callback(OWNER, f"receipt:analyze:{receipt.id}")
        assert "ручная проверка" in telegram.calls[-1].text
        await approve_receipt(receipt.id, 800)
        async with Session() as session:
            assert (await session.get(Sale, 1)).payment_status == "PAID"

    run(scenario())


def test_receipt_upload_enforces_booking_access_and_pending_limit(receipt_api):
    async def scenario():
        await prepare_sale(MANAGER)
        await message(MANAGER, "2")
        await callback(PHOTO_A, "receipt:upload:PAYMENT:1")
        assert "Нет доступа" in telegram.calls[-1].text
        # Forging FSM data still cannot bypass authorization at upload time.
        await state_for(PHOTO_A).set_state(ReceiptFlow.photo)
        await state_for(PHOTO_A).set_data({"receipt_booking": 1, "receipt_purpose": "PAYMENT"})
        await photo(PHOTO_A, "unauthorized")
        receipt = await attach_receipt("PAYMENT", "first-pending")
        await attach_receipt("PAYMENT", "second-pending", PHOTO_B)
        assert "уже ожидает" in telegram.calls[-1].text
        async with Session() as session:
            assert (await session.scalars(select(Receipt.id))).all() == [receipt.id]

    run(scenario())


def test_receipt_analysis_flags_mismatched_missing_and_old_fields():
    flags = warnings_for({"is_receipt": False, "amount": "100", "currency": "USD",
                          "date": "2020-01-01"}, 800, date(2026, 9, 18))
    assert any("отличается" in flag for flag in flags)
    assert any("старше" in flag for flag in flags)
    assert any("Получатель" in flag for flag in flags)
    assert any("RUB" in flag for flag in flags)
    flags = warnings_for({"amount": "NaN", "date": "2030-01-01"}, 800, date(2026, 9, 18))
    assert "Сумма не распознана." in flags
    assert "В чеке будущая дата." in flags


@pytest.mark.parametrize("result_kind", ["success", "bad_schema", "refusal", "incomplete", "http_error", "no_key"])
def test_receipt_vision_response_contract_and_failures(monkeypatch, result_kind):
    from types import SimpleNamespace

    from app.services import receipts as service

    captured = []
    fields = {"is_receipt": True, "amount": "800.00", "currency": "RUB",
              "date": "2026-09-18", "bank": "Bank", "recipient": "Recipient",
              "operation_id": "operation-1", "payment_status": "Выполнен", "concerns": []}

    class Response:
        status = 429 if result_kind == "http_error" else 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def json(self):
            content = [{"type": "output_text", "text": json.dumps(
                {"fake": "wrong schema"} if result_kind == "bad_schema" else fields
            )}]
            if result_kind == "refusal":
                content = [{"type": "refusal", "refusal": "refused"}]
            return {"status": "incomplete" if result_kind == "incomplete" else "completed",
                    "output": [{"type": "message", "content": content}]}

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def post(self, url, *, headers, json):
            captured.append((url, headers, json))
            return Response()

    monkeypatch.setattr(service, "config", SimpleNamespace(
        openai_api_key="" if result_kind == "no_key" else "test-key",
        receipt_analysis_model="gpt-4.1-mini",
    ))
    monkeypatch.setattr(service.aiohttp, "ClientSession", Client)
    result = run(service.extract_receipt(b"test-image"))
    if result_kind == "no_key":
        assert result == {"status": "not_configured"}
        assert captured == []
        return
    assert result["status"] == ("extracted" if result_kind == "success" else "unavailable")
    url, headers, payload = captured[0]
    assert url == "https://api.openai.com/v1/responses"
    assert headers == {"Authorization": "Bearer test-key"}
    assert payload["store"] is False
    assert payload["text"]["format"]["strict"] is True
    assert payload["input"][0]["content"][1]["image_url"].startswith("data:image/jpeg;base64,")
    assert os.environ["BOT_TOKEN"] not in json.dumps(payload)


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


async def photo(tg_id, file_id):
    global update_id
    update_id += 1
    event = Message(
        message_id=update_id,
        date=datetime.now(timezone.utc),
        chat=Chat(id=tg_id, type="private"),
        from_user=TelegramUser(id=tg_id, first_name="Test", is_bot=False),
        photo=[
            PhotoSize(
                file_id=file_id,
                file_unique_id=f"unique-{file_id}",
                width=1200,
                height=1600,
            )
        ],
    )
    return await dp.feed_update(bot, Update(update_id=update_id, message=event))


async def location(tg_id, latitude=55.7558, longitude=37.6173):
    global update_id
    update_id += 1
    event = Message(
        message_id=update_id,
        date=datetime.now(timezone.utc),
        chat=Chat(id=tg_id, type="private"),
        from_user=TelegramUser(id=tg_id, first_name="Test", is_bot=False),
        location=Location(latitude=latitude, longitude=longitude),
    )
    return await dp.feed_update(bot, Update(update_id=update_id, message=event))


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


async def prepare_sale(creator, *, role="PHOTOGRAPHER", sold_photos=2):
    """Seed a completed sale for finance/receipt regressions.

    Interactive sale-flow behavior is covered separately below.
    """
    async with Session() as session:
        booking = (await session.scalars(select(Booking))).one()
        photographer = await get_user(session, PHOTO_B)
        manager = await get_user(session, MANAGER)
        creator_user = await get_user(session, creator)
        credited = manager if role == "MANAGER" else photographer
        percent = 16 if role == "MANAGER" else 10
        amount = sold_photos * 400
        sale = Sale(
            booking_id=booking.id,
            created_by_id=creator_user.id,
            credited_user_id=credited.id,
            commission_role=role,
            sold_photos=sold_photos,
            amount=amount,
            percent=percent,
            commission=amount * percent / 100,
        )
        session.add(sale)
        await session.flush()
        await refresh_payment_statuses(session, booking.id)
        await session.commit()
        return sale.id


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
            (MANAGER, "📊 Статистика"),
            (PHOTO_B, "📸 Мои съёмки"),
            (PHOTO_B, "📊 Моя статистика"),
            (PHOTO_B, "👤 Профиль"),
            (OWNER, "💰 Продажи"),
        ]:
            before = len(telegram.calls)
            await message(tg_id, text)
            assert len(telegram.calls) > before
        assert telegram.calls[-1].text == "💰 Продажи\nВыберите отель:"

    run(scenario())


@pytest.mark.parametrize("count", ["0", "-5", "text", "1.5", "10001"])
def test_invalid_new_sale_quantity_is_rejected(count):
    async def scenario():
        async with Session() as session:
            booking = (await session.scalars(select(Booking))).one()
            booking.status = "READY_FOR_SALE"
            shooting = (await session.scalars(select(Shooting))).one()
            shooting.status = "READY_FOR_SALE"
            manager = await get_user(session, MANAGER)
            draft = SaleDraft(
                booking_id=booking.id,
                created_by_id=manager.id,
                status="AWAITING_COUNTS",
                declared_photo_count=10,
            )
            session.add(draft)
            await session.commit()
            draft_id = draft.id
        state = state_for(MANAGER)
        await state.set_state(S.sold)
        await state.set_data({"sale_draft_id": draft_id})
        await message(MANAGER, count)
        async with Session() as session:
            assert await session.scalar(select(func.count(Sale.id))) == 0

    run(scenario())


def test_sale_requires_selected_photos_and_defers_photographer_percent():
    async def scenario():
        async with Session() as session:
            booking = (await session.scalars(select(Booking))).one()
            booking.status = "READY_FOR_SALE"
            shooting = (await session.scalars(select(Shooting))).one()
            shooting.status = "READY_FOR_SALE"
            manager = await get_user(session, MANAGER)
            draft = SaleDraft(
                booking_id=booking.id,
                created_by_id=manager.id,
                status="AWAITING_SELECTED",
                receipt_file_id="sale-check",
                receipt_file_unique_id="sale-check-unique",
                receipt_image_sha256="a" * 64,
                receipt_analysis=json.dumps({"status": "extracted"}),
                declared_photo_count=150,
                sold_photos=2,
                expected_amount=800,
            )
            session.add(draft)
            await session.flush()
            session.add(
                SaleDraftPhoto(
                    draft_id=draft.id,
                    telegram_file_id="selected-1",
                    telegram_unique_id="selected-1-unique",
                    storage_path="app:/PhotoBoss/sales/test/1.jpg",
                    sha256="b" * 64,
                    byte_size=100,
                )
            )
            await session.commit()
            draft_id = draft.id

        await callback(MANAGER, f"sale:finalize:{draft_id}")
        async with Session() as session:
            assert await session.scalar(select(func.count(Sale.id))) == 0

        async with Session() as session:
            session.add(
                SaleDraftPhoto(
                    draft_id=draft_id,
                    telegram_file_id="selected-2",
                    telegram_unique_id="selected-2-unique",
                    storage_path="app:/PhotoBoss/sales/test/2.jpg",
                    sha256="c" * 64,
                    byte_size=100,
                )
            )
            await session.commit()

        await callback(MANAGER, f"sale:finalize:{draft_id}")
        async with Session() as session:
            sale = (await session.scalars(select(Sale))).one()
            assert sale.sold_photos == 2
            assert sale.declared_photo_count == 150
            assert sale.amount == 800
            assert sale.percent == 0
            assert sale.commission == 0
            assert sale.commission_finalized_at is None
            receipt = (await session.scalars(select(Receipt))).one()
            assert receipt.file_id == "sale-check"
            assert receipt.status == "PENDING"
            shooting = (await session.scalars(select(Shooting))).one()
            session.add_all(
                Photo(shooting_id=shooting.id, file_id=f"full-{index}")
                for index in range(1, 151)
            )
            shooting.full_upload_completed_at = datetime.now(timezone.utc).replace(
                tzinfo=None
            )
            booking = (await session.scalars(select(Booking))).one()
            result = await finalize_photographer_commissions(session, booking)
            await session.commit()
            assert result["percent"] == 15
            sale = (await session.scalars(select(Sale))).one()
            assert sale.percent == 15
            assert sale.commission == 120
            assert sale.commission_finalized_at is not None

    run(scenario())


def test_shooting_order_ownership_start_finish_ready_and_full_upload():
    async def scenario():
        await callback(PHOTO_A, "photo:pickup:1")
        async with Session() as session:
            assert (await session.get(Shooting, 1)).status == "ASSIGNED"

        await callback(PHOTO_B, "photo:pickup:1")
        async with Session() as session:
            shoot = await session.get(Shooting, 1)
            first_time = shoot.accepted_at
            assert shoot.status == "PICKED_UP"

        await callback(PHOTO_B, "photo:pickup:1")
        await callback(PHOTO_B, "photo:start:1")
        async with Session() as session:
            assert (await session.get(Shooting, 1)).status == "SHOOTING"

        await callback(PHOTO_B, "photo:finish:1")
        async with Session() as session:
            assert (await session.get(Shooting, 1)).status == "SHOT"

        await callback(PHOTO_B, "photo:ready:1")
        async with Session() as session:
            shoot = await session.get(Shooting, 1)
            assert shoot.status == "READY_FOR_SALE"
            assert shoot.accepted_at == first_time
            assert shoot.ready_for_sale_at is not None

        await callback(PHOTO_B, "photo:full_upload:1")
        assert await state_for(PHOTO_B).get_state() == PhotoUploadFlow.uploading.state
        async with Session() as session:
            session.add(Photo(shooting_id=1, file_id="full-upload-test"))
            await session.commit()
        await callback(PHOTO_B, "photo:upload_done")
        async with Session() as session:
            shoot = await session.get(Shooting, 1)
            assert shoot.full_upload_completed_at is not None

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
    assert "📚 Академия" in items
    assert "🎓 Обучение" in items
    assert "🧾 Продажа" in items
    assert "📋 Мои записи" in items
    assert "📸 Мои съёмки" in items
    assert "➕ Добавить сотрудника" in items


def test_owner_can_start_employee_creation_from_menu_button():
    async def scenario():
        await message(OWNER, "➕ Добавить сотрудника")
        assert await state_for(OWNER).get_state() == E.tg.state
        assert telegram.calls[-1].text.startswith("Введите номер сотрудника в Telegram")

    run(scenario())


def test_owner_can_press_employee_add_inline_button():
    async def scenario():
        await message(OWNER, "👥 Сотрудники")
        await callback(OWNER, "employee:add")
        assert await state_for(OWNER).get_state() == E.tg.state
        assert telegram.calls[-2].text.startswith("Введите номер сотрудника в Telegram")

    run(scenario())


def test_employee_list_has_management_buttons():
    async def scenario():
        await message(OWNER, "👥 Сотрудники")
        markup = telegram.calls[-1].reply_markup
        callbacks = {
            button.callback_data for row in markup.inline_keyboard for button in row
        }
        assert "employee:add" in callbacks
        assert any(value.startswith("employee:view:") for value in callbacks)

    run(scenario())


def test_owner_can_remove_one_role_without_firing_employee():
    async def scenario():
        async with Session() as session:
            target = await get_user(session, PHOTO_A)
            session.add(UserRole(user_id=target.id, role="MANAGER"))
            await session.commit()
            target_id = target.id
        await callback(OWNER, f"employee:remove_role:{target_id}:MANAGER")
        async with Session() as session:
            target = await get_user(session, PHOTO_A)
            assert target.active is True
            assert await roles_of(session, target) == {"PHOTOGRAPHER"}

    run(scenario())


def test_owner_can_fire_and_restore_employee_but_owner_is_protected():
    async def scenario():
        async with Session() as session:
            target_id = (await get_user(session, PHOTO_B)).id
            owner_id = (await get_user(session, OWNER)).id
        await callback(OWNER, f"employee:fire_confirm:{target_id}")
        async with Session() as session:
            assert (await get_user(session, PHOTO_B)).active is False
            assert (
                await session.scalar(
                    select(func.count(AuditLog.id)).where(
                        AuditLog.action == "employee_fired"
                    )
                )
            ) == 1
        await callback(OWNER, f"employee:restore:{target_id}")
        async with Session() as session:
            assert (await get_user(session, PHOTO_B)).active is True

        await callback(ADMIN, f"employee:fire_confirm:{owner_id}")
        async with Session() as session:
            assert (await get_user(session, OWNER)).active is True

    run(scenario())


def test_removing_last_role_disables_access_and_readding_restores_it():
    async def scenario():
        async with Session() as session:
            target_id = (await get_user(session, PHOTO_A)).id
        await callback(OWNER, f"employee:remove_role:{target_id}:PHOTOGRAPHER")
        async with Session() as session:
            target = await session.get(User, target_id)
            assert target.active is False
            assert await stored_role_names(session, target_id) == set()

        for value in [
            "➕ Добавить сотрудника",
            str(PHOTO_A),
            "Photo A Restored",
            "PHOTOGRAPHER",
        ]:
            await message(OWNER, value)
        async with Session() as session:
            target = await get_user(session, PHOTO_A)
            assert target.active is True
            assert await roles_of(session, target) == {"PHOTOGRAPHER"}

    async def stored_role_names(session, user_id):
        return set(
            (
                await session.scalars(
                    select(UserRole.role).where(UserRole.user_id == user_id)
                )
            ).all()
        )

    run(scenario())


@pytest.mark.parametrize("tg_id", [OWNER, ADMIN, MANAGER, PHOTO_A])
def test_training_is_available_to_every_staff_role(tg_id):
    async def scenario():
        before = len(telegram.calls)
        await message(tg_id, "🎓 Обучение")
        assert len(telegram.calls) == before + 1
        assert telegram.calls[-1].text.startswith("🎓 Обучение")
        assert "5 разных поз" in telegram.calls[-1].text

    run(scenario())


def test_training_category_sends_a_real_reference_photo():
    async def scenario():
        await callback(OWNER, "training:family")
        photo_calls = [call for call in telegram.calls if isinstance(call, SendPhoto)]
        assert len(photo_calls) == 5
        assert [call.photo.path.name for call in photo_calls] == [
            "01.jpg",
            "02.jpg",
            "03.jpg",
            "04.jpg",
            "05.jpg",
        ]
        assert [call.caption.rsplit(" ", 1)[-1] for call in photo_calls] == [
            "1/5",
            "2/5",
            "3/5",
            "4/5",
            "5/5",
        ]
        async with Session() as session:
            assignment = (await session.scalars(select(TrainingAssignment))).one()
            assert assignment.category_slug == "family"
            assert assignment.status == "ACTIVE"

    run(scenario())


def test_shift_requires_location_and_photo_and_charges_late_fine(monkeypatch):
    fixed = datetime(2026, 9, 15, 9, 1, tzinfo=ZoneInfo("Europe/Moscow"))

    def fixed_shift_now(value=None):
        if value is None:
            return fixed
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(ZoneInfo("Europe/Moscow"))

    monkeypatch.setattr(photographer_module, "shift_now", fixed_shift_now)

    async def scenario():
        await message(PHOTO_A, "🔄 Моя смена")
        assert telegram.calls[-1].text.startswith("🔄 Моя смена")
        await callback(PHOTO_A, "shift:begin")
        assert await state_for(PHOTO_A).get_state() == ShiftFlow.location.state

        await location(PHOTO_A)
        assert await state_for(PHOTO_A).get_state() == ShiftFlow.photo.state
        assert "полный рост" in telegram.calls[-1].text

        await photo(PHOTO_A, "full-body-check-in")
        assert await state_for(PHOTO_A).get_state() is None
        assert "штраф 500" in telegram.calls[-1].text
        async with Session() as session:
            check_in = (await session.scalars(select(ShiftCheckIn))).one()
            assert check_in.status == "STARTED"
            assert check_in.full_body_file_id == "full-body-check-in"
            assert check_in.latitude == pytest.approx(55.7558)
            assert check_in.longitude == pytest.approx(37.6173)
            assert check_in.late is True
            assert check_in.fine_amount == 500
            fine = (await session.scalars(select(PayrollEntry))).one()
            assert fine.kind == "Штраф за опоздание"
            assert fine.amount == -500

        await message(PHOTO_A, "🔄 Моя смена")
        assert "Смена начата в 09:01" in telegram.calls[-1].text

    run(scenario())


def test_shift_before_nine_has_no_fine(monkeypatch):
    fixed = datetime(2026, 9, 15, 8, 59, tzinfo=ZoneInfo("Europe/Moscow"))
    monkeypatch.setattr(photographer_module, "shift_now", lambda value=None: fixed)

    async def scenario():
        await callback(PHOTO_B, "shift:begin")
        await location(PHOTO_B)
        await photo(PHOTO_B, "on-time-full-body")
        async with Session() as session:
            check_in = (await session.scalars(select(ShiftCheckIn))).one()
            assert check_in.late is False
            assert check_in.fine_amount == 0
            assert await session.scalar(select(func.count(PayrollEntry.id))) == 0
        assert "вовремя" in telegram.calls[-1].text

    run(scenario())


def test_shift_end_requires_location_and_workplace_photo():
    async def scenario():
        await callback(PHOTO_A, "shift:begin")
        await location(PHOTO_A)
        await photo(PHOTO_A, "start-full-body")

        await message(PHOTO_A, "🔄 Моя смена")
        markup = telegram.calls[-1].reply_markup
        assert markup.inline_keyboard[0][0].callback_data == "shift:end"
        await callback(PHOTO_A, "shift:end")
        assert await state_for(PHOTO_A).get_state() == ShiftFlow.end_location.state
        await location(PHOTO_A, latitude=55.7, longitude=37.5)
        assert await state_for(PHOTO_A).get_state() == ShiftFlow.workplace_photo.state
        await photo(PHOTO_A, "workplace-photo")
        assert await state_for(PHOTO_A).get_state() is None

        async with Session() as session:
            check_out = (await session.scalars(select(ShiftCheckOut))).one()
            assert check_out.status == "FINISHED"
            assert check_out.latitude == pytest.approx(55.7)
            assert check_out.longitude == pytest.approx(37.5)
            assert check_out.workplace_file_id == "workplace-photo"
            assert check_out.ended_at is not None

    run(scenario())


def test_new_booking_button_creates_complete_booking(monkeypatch):
    async def fake_download(*args):
        return b"fake receipt bytes"

    async def fake_extract(*args):
        return {"status": "not_configured"}

    monkeypatch.setattr(receipts_module, "download_receipt", fake_download)
    monkeypatch.setattr(receipts_module, "extract_receipt", fake_extract)
    async def scenario():
        async with Session() as session:
            hotel = (await session.scalars(select(Hotel))).first()
            package = (await session.scalars(select(Package))).first()
            photographer = await get_user(session, PHOTO_A)
            before = await session.scalar(select(func.count(Booking.id)))

        await message(MANAGER, "➕ Новая запись")
        assert await state_for(MANAGER).get_state() == BookingFlow.hotel.state
        await callback(MANAGER, f"booking:hotel:{hotel.id}")
        for text in [
            "Новый клиент",
            "+79990000000",
            "3",
            "404",
            "1000",
            "20.09.2026",
            "14:30",
        ]:
            await message(MANAGER, text)
        assert await state_for(MANAGER).get_state() == BookingFlow.package.state
        await callback(MANAGER, f"booking:package:{package.id}")
        await callback(MANAGER, f"booking:photographer:{photographer.id}")
        assert await state_for(MANAGER).get_state() == ReceiptFlow.photo.state

        async with Session() as session:
            assert await session.scalar(select(func.count(Booking.id))) == before + 1
            booking = (
                await session.scalars(select(Booking).order_by(Booking.id.desc()))
            ).first()
            assert booking.room == "404"
            assert booking.guest_count == 3
            assert booking.deposit == 1000
            assert booking.shoot_date == date(2026, 9, 20)
            assert booking.shoot_time == time(14, 30)
            assert booking.photographer_id is None
            assert booking.status == "PENDING_CONFIRMATION"
            assert await session.scalar(
                select(func.count(Shooting.id)).where(Shooting.booking_id == booking.id)
            ) == 1

        await callback(MANAGER, f"booking:confirm:{booking.id}")
        assert "фото чека" in telegram.calls[-1].text
        await photo(MANAGER, "deposit-new-booking")
        assert await state_for(MANAGER).get_state() is None
        async with Session() as session:
            receipt = (await session.scalars(select(Receipt))).one()
            assert receipt.booking_id == booking.id
            assert receipt.status == "PENDING"
            assert receipt.expected_amount == 1000
            assert not await session.scalar(select(func.count(Photo.id)))
        await callback(MANAGER, f"booking:confirm:{booking.id}")
        async with Session() as session:
            booking = await session.get(Booking, booking.id)
            assert booking.status == "CONFIRMED"

    run(scenario())


def test_manager_cards_have_colored_decisions_and_reminder():
    async def scenario():
        await message(MANAGER, "📋 Мои записи")
        call = telegram.calls[-1]
        assert "📞 Телефон:" in call.text
        assert "📸 Фотограф:" in call.text
        buttons = [button for row in call.reply_markup.inline_keyboard for button in row]
        by_callback = {button.callback_data: button for button in buttons}
        assert by_callback["booking:confirm:1"].style == "success"
        assert by_callback["booking:reject:1"].style == "danger"
        assert by_callback["booking:reschedule:1"].style == "primary"
        assert "booking:remind:1" in by_callback

        await callback(MANAGER, "booking:remind:1")
        assert telegram.calls[-1].text.startswith("🔔 Напоминание гостю")
        await callback(MANAGER, "booking:confirm:1")
        async with Session() as session:
            assert (await session.get(Booking, 1)).status == "CONFIRMED"
            assert (await session.get(Shooting, 1)).status == "ASSIGNED"

    run(scenario())


def test_admin_sees_complete_booking_cards():
    async def scenario():
        await message(OWNER, "📋 Все записи")
        assert telegram.calls[-1].text == "📋 Выберите день записей"
        assert len(telegram.calls[-1].reply_markup.inline_keyboard) == 9
        await callback(OWNER, "admin:bookings:date:2026-09-15")
        sent = [call for call in telegram.calls if isinstance(call, SendMessage)]
        assert sent[-3].text.startswith("📋 ЕЖЕДНЕВНИК\nДата: 15.09.2026")
        card = sent[-2].text
        for field in [
            "🏨 Отель:",
            "🚪 Комната:",
            "👤 Клиент:",
            "📞 Телефон:",
            "👥 Количество гостей:",
            "📅 Дата:",
            "🕐 Время:",
            "📦 Пакет:",
            "💳 Бронь:",
            "💰 Продажа:",
            "🖼 Кадры:",
            "📋 Менеджер:",
            "📸 Фотограф:",
        ]:
            assert field in card

    run(scenario())


def test_admin_shootings_are_selected_by_date_and_time():
    async def scenario():
        await message(OWNER, "📸 Все съёмки")
        assert telegram.calls[-1].text == "📸 Выберите день съёмок"
        await callback(OWNER, "admin:shoots:date:2026-09-15")
        call = telegram.calls[-1]
        assert call.text == "📸 Съёмки на 15.09.2026: 1"
        assert call.reply_markup.inline_keyboard[0][0].callback_data == (
            "admin:shoot:view:1"
        )
        await callback(OWNER, "admin:shoot:view:1")
        assert telegram.calls[-1].text.startswith("📋 Запись #1")

    run(scenario())


def test_daily_weekly_and_monthly_reports_show_cash_and_employee_percent():
    async def scenario():
        await prepare_sale(PHOTO_A)
        await message(PHOTO_A, "2")
        await message(OWNER, "📊 Отчёты")
        assert telegram.calls[-2].text == "📊 Дневной отчёт — выберите день"
        assert telegram.calls[-1].text == "Итоговый период:"

        today = datetime.now(ZoneInfo("Europe/Moscow")).date()
        await callback(OWNER, f"admin:report:date:{today.isoformat()}")
        report = telegram.calls[-1].text
        assert "Касса: 800.00 ₽" in report
        assert "Процент сотрудников: 80.00 ₽" in report
        assert "Итого компании: 720.00 ₽" in report
        assert "PHOTOGRAPHER" in report
        assert "Фотограф: 10% = 80.00 ₽" in report

        await callback(OWNER, "admin:report:period:week")
        assert telegram.calls[-1].text.startswith("📊 Отчёт")
        await callback(OWNER, "admin:report:period:month")
        assert telegram.calls[-1].text.startswith("📊 Отчёт")

    run(scenario())


def test_owner_sales_are_filtered_by_hotel_and_period():
    async def scenario():
        await prepare_sale(PHOTO_A)
        await message(PHOTO_A, "2")
        async with Session() as session:
            hotel = (await session.scalars(select(Hotel))).first()

        await message(OWNER, "💰 Продажи")
        await callback(OWNER, f"admin:sales:hotel:{hotel.id}")
        assert telegram.calls[-2].text.startswith(f"🏨 {hotel.name}")
        today = datetime.now(ZoneInfo("Europe/Moscow")).date()
        await callback(
            OWNER, f"admin:sales:day:{hotel.id}:{today.isoformat()}"
        )
        report = telegram.calls[-2]
        assert f"💰 Продажи · {hotel.name}" in report.text
        assert "Касса: 800.00 ₽" in report.text
        assert "Куплено кадров: 2" in report.text
        assert "PHOTOGRAPHER" in report.text
        assert report.reply_markup is not None

        await callback(OWNER, f"admin:sales:period:{hotel.id}:week")
        assert telegram.calls[-2].text.startswith(f"💰 Продажи · {hotel.name}")
        await callback(OWNER, f"admin:sales:period:{hotel.id}:month")
        assert telegram.calls[-2].text.startswith(f"💰 Продажи · {hotel.name}")

    run(scenario())


@pytest.mark.parametrize(
    ("section", "search_callback", "expected"),
    [
        ("📋 Все записи", "admin:bookings:search", "📋 ЕЖЕДНЕВНИК"),
        ("📸 Все съёмки", "admin:shoots:search", "📸 Съёмки на 15.09.2026"),
        ("📊 Отчёты", "admin:report:search", "📊 Отчёт 15.09.2026"),
    ],
)
def test_owner_can_search_any_date(section, search_callback, expected):
    async def scenario():
        await message(OWNER, section)
        await callback(OWNER, search_callback)
        assert await state_for(OWNER).get_state() == DateLookup.value.state
        await message(OWNER, "15.09.2026")
        assert await state_for(OWNER).get_state() is None
        sent = [call.text for call in telegram.calls if isinstance(call, SendMessage)]
        assert any(text.startswith(expected) for text in sent)

    run(scenario())


@pytest.mark.parametrize(
    ("shoot_photos", "expected_percent"),
    [(149, 10), (150, 15), (151, 15)],
)
def test_photographer_percent_finalizes_only_from_full_shoot_count(
    shoot_photos, expected_percent
):
    async def scenario():
        async with Session() as session:
            booking = (await session.scalars(select(Booking))).one()
            shooting = (await session.scalars(select(Shooting))).one()
            photographer = await get_user(session, PHOTO_B)
            creator = await get_user(session, MANAGER)
            session.add(
                Sale(
                    booking_id=booking.id,
                    created_by_id=creator.id,
                    credited_user_id=photographer.id,
                    commission_role="PHOTOGRAPHER",
                    sold_photos=2,
                    declared_photo_count=shoot_photos,
                    amount=800,
                    percent=0,
                    commission=0,
                )
            )
            session.add_all(
                Photo(shooting_id=shooting.id, file_id=f"tier-{index}")
                for index in range(1, shoot_photos + 1)
            )
            await session.flush()
            before = (await session.scalars(select(Sale))).one()
            assert before.percent == 0 and before.commission == 0
            shooting.full_upload_completed_at = datetime.now(timezone.utc).replace(
                tzinfo=None
            )
            result = await finalize_photographer_commissions(session, booking)
            await session.commit()
            sale = (await session.scalars(select(Sale))).one()
            assert result["count"] == shoot_photos
            assert sale.percent == expected_percent
            assert sale.commission == 800 * expected_percent / 100

    run(scenario())



def test_owner_can_add_named_premium_and_open_employee_profile():
    async def scenario():
        await message(OWNER, "💵 Зарплаты/выплаты")
        await callback(OWNER, "premium:add")
        async with Session() as session:
            employee = await get_user(session, PHOTO_A)
        await callback(OWNER, f"premium:user:{employee.id}")
        await message(OWNER, "2500")
        await message(OWNER, "Отличная работа")
        async with Session() as session:
            premium = (
                await session.scalars(
                    select(PayrollEntry).where(PayrollEntry.kind == "Премия")
                )
            ).one()
            assert premium.user_id == employee.id
            assert premium.amount == 2500

        await message(OWNER, "💵 Зарплаты/выплаты")
        call = telegram.calls[-1]
        assert "PHOTOGRAPHER" in call.text
        callbacks = {
            button.callback_data
            for row in call.reply_markup.inline_keyboard
            for button in row
        }
        assert f"employee:view:{employee.id}" in callbacks

    run(scenario())


def test_each_training_category_has_five_distinct_photos():
    assert len(TRAINING_CATEGORIES) == 9
    for category in TRAINING_CATEGORIES:
        paths = category.image_paths
        assert len(paths) == 5
        assert all(path.is_file() for path in paths)
        digests = {hashlib.sha256(path.read_bytes()).digest() for path in paths}
        assert len(digests) == 5


def test_training_is_locked_until_owner_reviews_and_rejected_pose_is_repeated():
    async def scenario():
        await callback(PHOTO_A, "training:woman")
        await callback(PHOTO_A, "training:man")
        async with Session() as session:
            assert (
                await session.scalar(select(func.count(TrainingAssignment.id)))
            ) == 1

        for index in range(1, 6):
            await photo(PHOTO_A, f"training-upload-{index}")
        async with Session() as session:
            assignment = (await session.scalars(select(TrainingAssignment))).one()
            assert assignment.status == "PENDING_REVIEW"
            assert (
                await session.scalar(select(func.count(TrainingSubmission.id)))
            ) == 5

        await callback(ADMIN, f"training_approve:{assignment.id}")
        async with Session() as session:
            assert (await session.get(TrainingAssignment, assignment.id)).status == (
                "PENDING_REVIEW"
            )

        await callback(OWNER, f"training_review:{assignment.id}")
        review_photos = [call for call in telegram.calls if isinstance(call, SendPhoto)]
        assert len(review_photos) >= 16

        await callback(OWNER, f"training_reject:{assignment.id}:3")
        async with Session() as session:
            current = await session.get(TrainingAssignment, assignment.id)
            assert current.status == "ACTIVE"
            indexes = set(
                (
                    await session.scalars(
                        select(TrainingSubmission.pose_index).where(
                            TrainingSubmission.assignment_id == assignment.id
                        )
                    )
                ).all()
            )
            assert indexes == {1, 2, 4, 5}

        await photo(PHOTO_A, "training-upload-3-redone")
        async with Session() as session:
            assert (await session.get(TrainingAssignment, assignment.id)).status == (
                "PENDING_REVIEW"
            )

        await callback(OWNER, f"training_approve:{assignment.id}")
        async with Session() as session:
            approved = await session.get(TrainingAssignment, assignment.id)
            assert approved.status == "COMPLETED"
            assert approved.completed_at is not None

        await callback(PHOTO_A, "training:man")
        async with Session() as session:
            assert (
                await session.scalar(select(func.count(TrainingAssignment.id)))
            ) == 1

        async with Session() as session:
            approved = await session.get(TrainingAssignment, assignment.id)
            approved.assigned_date = training_day() - timedelta(days=1)
            approved.completed_at = datetime.now(timezone.utc).replace(
                tzinfo=None
            ) - timedelta(days=1)
            await session.commit()
        await callback(PHOTO_A, "training:woman")
        async with Session() as session:
            assert (
                await session.scalar(select(func.count(TrainingAssignment.id)))
            ) == 1
        await callback(PHOTO_A, "training:man")
        async with Session() as session:
            assignments = (
                await session.scalars(
                    select(TrainingAssignment).order_by(TrainingAssignment.id)
                )
            ).all()
            assert [item.category_slug for item in assignments] == ["woman", "man"]

        await callback(PHOTO_B, "training:woman")
        async with Session() as session:
            assert (
                await session.scalar(
                    select(func.count(TrainingAssignment.id)).where(
                        TrainingAssignment.user_id
                        == (await get_user(session, PHOTO_B)).id
                    )
                )
            ) == 1

    run(scenario())


def test_photographer_assignment_is_owner_or_admin_only():
    async def scenario():
        async with Session() as session:
            hotel = (await session.scalars(select(Hotel))).first()
            package = (await session.scalars(select(Package))).first()
            photographer = await get_user(session, PHOTO_A)
            before = await session.scalar(select(func.count(Booking.id)))
            data = {"hotel_id": hotel.id, "package_id": package.id, "client_name": "Fixture client",
                "client_phone": None, "room": "200", "guest_count": 2, "deposit": 0,
                "shoot_date": "2026-09-22", "shoot_time": "12:00:00"}
        # Even a crafted callback in a stale photographer-selection state is rejected.
        state = state_for(MANAGER)
        await state.set_state(BookingFlow.photographer)
        await state.set_data(data)
        await callback(MANAGER, f"booking:photographer:{photographer.id}")
        await callback(MANAGER, f"smart:assign:1:{photographer.id}")
        async with Session() as session:
            assert await session.scalar(select(func.count(Booking.id))) == before
            assert (await session.get(Booking, 1)).photographer_id != photographer.id
        # New manager bookings skip the selection UI and stay unassigned.
        await state.set_state(BookingFlow.package)
        telegram.calls.clear()
        await callback(MANAGER, f"booking:package:{package.id}")
        assert not any(isinstance(call, SendMessage) and "Выберите фотографа" in call.text for call in telegram.calls)
        async with Session() as session:
            created = await session.scalar(select(Booking).order_by(Booking.id.desc()))
            assert created.photographer_id is None
        # Both authorized roles can still create an assigned booking.
        for actor in (OWNER, ADMIN):
            await state_for(actor).set_state(BookingFlow.package)
            await state_for(actor).set_data(data)
            await callback(actor, f"booking:package:{package.id}")
            assert await state_for(actor).get_state() == BookingFlow.photographer.state
            await callback(actor, f"booking:photographer:{photographer.id}")
            async with Session() as session:
                created = await session.scalar(select(Booking).order_by(Booking.id.desc()))
                assert created.photographer_id == photographer.id
            await callback(actor, f"smart:assign:1:{photographer.id}")
            async with Session() as session:
                assert (await session.get(Booking, 1)).photographer_id == photographer.id
    run(scenario())
