"""Guest delivery in the existing Telegram bot; guests never become staff accounts."""
import logging

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    WebAppInfo,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .delivery import SERVICES
from .models import (
    Booking,
    DeliveryBookingResponse,
    DeliveryCampaign,
    DeliveryCampaignRecipient,
    DeliveryClaim,
    DeliveryContact,
    DeliveryGallery,
    DeliveryReminder,
    User,
    utc_now,
)

logger = logging.getLogger(__name__)
r = Router()
r.message.filter(F.chat.type == 'private', F.from_user)


def service(bot):
    return SERVICES.get(bot.token)


async def contact_record(session, user):
    contact = await session.scalar(select(DeliveryContact).where(DeliveryContact.tg_id == user.id))
    if not contact:
        contact = DeliveryContact(tg_id=user.id, name=user.full_name[:200], username=user.username)
        session.add(contact)
        await session.flush()
    else:
        contact.name, contact.username = user.full_name[:200], user.username
        contact.blocked = False
    return contact


def preferences_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='Получать предложения', callback_data='delivery:optin'),
        InlineKeyboardButton(text='Отписаться', callback_data='delivery:optout')]])


async def guest_button(message, svc):
    await message.answer('Личный кабинет Photo Boss: ваши альбомы и семейные слайд-шоу.',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text='Открыть личный кабинет', web_app=WebAppInfo(url=svc.origin()+'/guest/'))]]),
        protect_content=False)


@r.message(CommandStart(deep_link=True, magic=F.args == 'guest'))
async def guest_start(message):
    svc = service(message.bot)
    if not svc:
        return await message.answer('Личный кабинет временно недоступен.')
    async with AsyncSession(svc.api.engine) as session:
        await contact_record(session, message.from_user)
        await session.commit()
    await guest_button(message, svc)


@r.message(CommandStart(deep_link=True, magic=F.args.regexp(r'^(gallery_|booking_)[A-Za-z0-9_-]{32}$')))
async def start(message, command):
    svc = service(message.bot)
    if not svc:
        return await message.answer('Выдача фото сейчас недоступна.', protect_content=False)
    token = command.args[8:]
    registering = command.args.startswith('booking_')
    async with AsyncSession(svc.api.engine, expire_on_commit=False) as session:
        g = await session.scalar(select(DeliveryGallery).where(DeliveryGallery.access_token == token))
        if not g or (not registering and not g.published) or (g.expires_at and g.expires_at <= utc_now()):
            return await message.answer('Ссылка недействительна. Попросите фотографа показать новый QR-код.', protect_content=False)
        contact = await contact_record(session, message.from_user)
        claim = await session.scalar(select(DeliveryClaim).where(DeliveryClaim.gallery_id == g.id, DeliveryClaim.contact_id == contact.id))
        if not claim:
            claim = DeliveryClaim(gallery_id=g.id, contact_id=contact.id)
            session.add(claim)
        await session.commit()
        if registering and not g.published:
            b = await session.get(Booking, g.booking_id)
            await message.answer(f'Вы записаны на съёмку №{b.id}: {b.shoot_date:%d.%m.%Y} в {b.shoot_time:%H:%M}. Напомним о времени. Готовые фотографии появятся здесь после публикации фотографом.', protect_content=False, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Подтвердить запись',callback_data=f'delivery:confirm:{g.id}'),InlineKeyboardButton(text='Попросить перенос',callback_data=f'delivery:reschedule:{g.id}')]]))
        else:
            url = svc.origin() + '/g/' + token
            await message.answer('Ваши готовые фотографии: ' + g.title,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Открыть и скачать фотографии', url=url)]]),
            protect_content=False)
            claim.link_sent_at = utc_now()
            await session.commit()
    await message.answer('Телефон можно оставить для связи по этой съёмке. Это необязательно; фото доступны по кнопке выше.',
        reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text='Поделиться контактом', request_contact=True)]], resize_keyboard=True, one_time_keyboard=True),
        protect_content=False)
    await message.answer('Сохранён ваш Telegram ID для выдачи фото и напоминаний о съёмке. Получать другие предложения можно по желанию. Отписка: /stop.',
        reply_markup=preferences_keyboard(), protect_content=False)
    await guest_button(message, svc)


@r.message(F.contact)
async def phone(message):
    svc = service(message.bot)
    if not svc:
        raise SkipHandler
    async with AsyncSession(svc.api.engine, expire_on_commit=False) as session:
        contact = await session.scalar(select(DeliveryContact).where(DeliveryContact.tg_id == message.from_user.id))
        if not contact:
            raise SkipHandler
        if message.contact.user_id != message.from_user.id:
            return await message.answer('Поделитесь собственным контактом через кнопку.', protect_content=False)
        contact.phone = message.contact.phone_number[:50]
        await session.commit()
    await message.answer('Контакт сохранён. Фотографии доступны по ссылке выше.', reply_markup=ReplyKeyboardRemove(), protect_content=False)


@r.callback_query(F.data.in_({'delivery:optin','delivery:optout'}))
async def preference(callback):
    svc = service(callback.bot)
    if not svc:
        return await callback.answer('Сервис недоступен.')
    async with AsyncSession(svc.api.engine) as session:
        c = await session.scalar(select(DeliveryContact).where(DeliveryContact.tg_id == callback.from_user.id))
        if not c:
            return await callback.answer('Сначала откройте альбом по QR-коду.')
        c.marketing_opt_in = callback.data == 'delivery:optin'
        await session.commit()
    await callback.answer('Подписка включена.' if callback.data == 'delivery:optin' else 'Подписка отключена.')


@r.message(Command('stop'))
async def stop(message):
    svc = service(message.bot)
    if not svc:
        return
    async with AsyncSession(svc.api.engine) as session:
        c = await session.scalar(select(DeliveryContact).where(DeliveryContact.tg_id == message.from_user.id))
        if c:
            c.marketing_opt_in = False
            await session.commit()
    await message.answer('Предложения отключены. Доступ к вашим фотографиям сохранён.', protect_content=False)


@r.callback_query(F.data.startswith('delivery:confirm:') | F.data.startswith('delivery:reschedule:'))
async def booking_response(callback):
    svc = service(callback.bot)
    if not svc:
        return await callback.answer('Сервис недоступен.')
    try:
        gid = int(callback.data.rsplit(':',1)[1])
    except ValueError:
        return await callback.answer('Некорректная запись.')
    async with AsyncSession(svc.api.engine, expire_on_commit=False) as session:
        c = await session.scalar(select(DeliveryContact).where(DeliveryContact.tg_id == callback.from_user.id))
        claim = await session.scalar(select(DeliveryClaim).where(DeliveryClaim.gallery_id == gid,DeliveryClaim.contact_id == c.id)) if c else None
        if not claim:
            return await callback.answer('Нет доступа к этой записи.')
        g = await session.get(DeliveryGallery, gid)
        b = await session.get(Booking, g.booking_id)
        from .services.operations import booking_moment
        kind = 'CONFIRM' if ':confirm:' in callback.data else 'RESCHEDULE'
        moment = booking_moment(b).isoformat()
        old = await session.scalar(select(DeliveryBookingResponse).where(DeliveryBookingResponse.booking_id == b.id,DeliveryBookingResponse.contact_id == c.id,DeliveryBookingResponse.kind == kind,DeliveryBookingResponse.moment == moment))
        if not old:
            session.add(DeliveryBookingResponse(booking_id=b.id,contact_id=c.id,kind=kind,moment=moment))
            await session.commit()
            manager = await session.get(User,b.manager_id)
            if manager and manager.active:
                try:
                    await callback.bot.send_message(manager.tg_id,f'Гость {c.name}: '+('подтвердил запись' if kind=='CONFIRM' else 'просит перенос съёмки')+f' №{b.id}. Телефон: {c.phone or "не предоставлен"}. Время автоматически не изменялось.')
                except TelegramAPIError:
                    pass
    await callback.answer('Запись подтверждена.' if kind=='CONFIRM' else 'Просьба сохранена. Время согласует менеджер.',show_alert=True)


async def run_guest_messages(api, bot):
    from .services.operations import (
        ACTIVE_BOOKING_STATUSES,
        booking_moment,
        reminder_kind,
    )
    sent = 0
    async with AsyncSession(api.engine, expire_on_commit=False) as session:
        now = utc_now()
        rows = (await session.execute(select(Booking, DeliveryContact).join(DeliveryGallery, DeliveryGallery.booking_id == Booking.id)
            .join(DeliveryClaim, DeliveryClaim.gallery_id == DeliveryGallery.id)
            .join(DeliveryContact, DeliveryContact.id == DeliveryClaim.contact_id)
            .where(Booking.status.in_(ACTIVE_BOOKING_STATUSES), DeliveryContact.blocked.is_(False)))).all()
        for b, c in rows:
            kind = reminder_kind(b, now)
            if not kind:
                continue
            moment = booking_moment(b).isoformat()
            exists = await session.scalar(select(DeliveryReminder.id).where(DeliveryReminder.booking_id == b.id,
                DeliveryReminder.contact_id == c.id, DeliveryReminder.kind == kind, DeliveryReminder.moment == moment))
            if exists:
                continue
            try:
                await bot.send_message(c.tg_id, f'Напоминаем о съёмке №{b.id}: {b.shoot_date:%d.%m.%Y} в {b.shoot_time:%H:%M}. Для переноса свяжитесь с менеджером.', protect_content=False)
            except TelegramForbiddenError:
                c.blocked = True
                continue
            except TelegramAPIError:
                continue
            session.add(DeliveryReminder(booking_id=b.id, contact_id=c.id, moment=moment, kind=kind))
            await session.commit()
            sent += 1
        pending = (await session.execute(select(DeliveryClaim, DeliveryGallery, DeliveryContact).join(DeliveryGallery, DeliveryGallery.id == DeliveryClaim.gallery_id).join(DeliveryContact, DeliveryContact.id == DeliveryClaim.contact_id).where(DeliveryClaim.link_sent_at.is_(None), DeliveryGallery.published.is_(True), DeliveryContact.blocked.is_(False)).limit(20))).all()
        for claim, g, c in pending:
            if g.expires_at and g.expires_at <= utc_now():
                continue
            try:
                await bot.send_message(c.tg_id, 'Ваши готовые фотографии: ' + g.title, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Смотреть и скачать',url=service(bot).origin()+'/g/'+g.access_token)]]), protect_content=False)
                claim.link_sent_at = utc_now()
                await session.commit()
                sent += 1
            except TelegramForbiddenError:
                c.blocked = True
                await session.commit()
            except TelegramAPIError:
                continue
        # Campaigns are queued only by an explicit owner action in the application.
        recipients = (await session.scalars(select(DeliveryCampaignRecipient).where(DeliveryCampaignRecipient.status == 'PENDING').order_by(DeliveryCampaignRecipient.id).limit(20))).all()
        for row in recipients:
            c = await session.get(DeliveryContact, row.contact_id)
            campaign = await session.get(DeliveryCampaign, row.campaign_id)
            if not c or not c.marketing_opt_in or c.blocked:
                row.status = 'SKIPPED'
            else:
                # Claim before sending: a worker restart must not repeat a promotional message.
                row.status = 'SENDING'
                await session.commit()
                try:
                    await bot.send_message(c.tg_id, campaign.text, protect_content=False)
                    row.status = 'SENT'
                    sent += 1
                except TelegramForbiddenError:
                    c.blocked = True
                    row.status = 'BLOCKED'
                except TelegramAPIError:
                    row.status = 'FAILED'
            await session.commit()
    return sent
