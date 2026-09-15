from aiogram.filters import Filter
from aiogram.types import CallbackQuery, Message

from .db import Session
from .services.core import get_user, roles_of


class StaffFilter(Filter):
    """Check authorization for every message, FSM step and callback."""

    def __init__(self, *roles):
        self.roles = set(roles) | {"ADMIN", "OWNER"}

    async def __call__(self, event: Message | CallbackQuery):
        message = event.message if isinstance(event, CallbackQuery) else event
        if message is None or message.chat.type != "private" or event.from_user is None:
            return False
        async with Session() as session:
            user = await get_user(session, event.from_user.id)
            roles = await roles_of(session, user)
        if not roles & self.roles:
            return False
        return {"current_user": user, "current_roles": roles}
