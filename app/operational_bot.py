"""Keep Telegram operational flows inside the current archived-reset generation."""
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery

from .miniapp_security import AccessError
from .operations_guard import current_generation, operation_lease
from .services.core import ROLES, menu

FSM_PREFIXES = ('BookingFlow:', 'ShiftFlow:', 'PhotoUploadFlow:', 'S:',
                'ReceiptFlow:', 'BankFlow:', 'AdminShootFlow:', 'DateLookup:')
GENERATION_DATA_KEY = '_operations_generation'
RESET_MESSAGE = 'Рабочие данные обнулены владельцем. Начните действие заново через меню Photo Boss.'
OPERATION_MENUS = set(menu(set(ROLES))) - {'📚 Академия', '🎓 Обучение'}


def operational_state(value):
    return isinstance(value, str) and value.startswith(FSM_PREFIXES)


def guest_deep_link(text):
    parts = text.split(maxsplit=1)
    return (len(parts) == 2 and parts[0].split('@', 1)[0] == '/start'
            and parts[1].startswith(('gallery_', 'booking_')))


def bypass(event):
    callback = getattr(event, 'data', None) or ''
    text = getattr(event, 'text', None) or ''
    command = text.split(maxsplit=1)[0].split('@', 1)[0] if text else ''
    return (callback.startswith(('academy:', 'training:', 'training_review:', 'training_approve:',
                                 'training_reject:', 'web-login:', 'hr:'))
            or callback in {'nav:back', 'nav:home', 'delivery:optin', 'delivery:optout'}
            or text in {'📚 Академия', '🎓 Обучение', '❌ Отменить'}
            or command in {'/app', '/myid', '/cancel', '/stop'}
            or (command == '/start' and not guest_deep_link(text)))


def operational_event(event, raw_state):
    callback = getattr(event, 'data', None) or ''
    text = getattr(event, 'text', None) or ''
    # Every non-Academy text/callback is leased, including staff/admin flows and
    # guest delivery claims. Academy photo submissions without operational FSM
    # state continue independently of the reset barrier.
    return (operational_state(raw_state) or bool(callback) or bool(text)
            or getattr(event, 'contact', None) is not None)


async def notify(event, message):
    if isinstance(event, CallbackQuery):
        await event.answer(message, show_alert=True)
    else:
        await event.answer(message)


class OperationalBotMiddleware(BaseMiddleware):
    def __init__(self, engine):
        self.engine = engine

    async def __call__(self, handler, event, data):
        if bypass(event):
            return await handler(event, data)
        state = data.get('state')
        raw_state = await state.get_state() if state is not None else data.get('raw_state')
        if not operational_event(event, raw_state):
            return await handler(event, data)
        try:
            async with operation_lease(self.engine) as conn:
                generation = await current_generation(conn)
                if state is not None and operational_state(raw_state):
                    saved = await state.get_data()
                    if generation and saved.get(GENERATION_DATA_KEY) != generation:
                        await state.clear()
                        data['raw_state'] = None
                        # A new menu entry is safe after discarding the old form.
                        fresh = (getattr(event, 'text', None) in OPERATION_MENUS
                                 or getattr(event, 'data', None) == 'shift:begin')
                        if not fresh:
                            await notify(event, RESET_MESSAGE)
                            return None
                try:
                    return await handler(event, data)
                finally:
                    # Entry handlers often clear their FSM data before setting
                    # the first step. Stamp the actual resulting state afterward.
                    if state is not None and operational_state(await state.get_state()):
                        await state.update_data(**{GENERATION_DATA_KEY: generation})
        except AccessError as exc:
            if exc.status != 503:
                raise
            await notify(event, str(exc))
            return None
