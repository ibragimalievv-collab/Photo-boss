"""Current booking scope controls guest SMS settings; no phone is returned."""
import json

from aiohttp import web
from sqlalchemy.ext.asyncio import AsyncSession

from .miniapp_security import AccessError
from .models import Booking, User
from .people import positive_id
from .services.booking_access import can_access_booking
from .services.core import audit, roles_of
from .services.guest_sms import GuestSMS


class GuestSMSAPI:
    def __init__(self, api, *, service=None):
        self.api, self.service = api, service or GuestSMS(api.engine)

    async def handle(self, request):
        a = request['miniapp_actor']
        body = await self.api.body(request) if request.method != 'GET' else None
        async with AsyncSession(self.api.engine, expire_on_commit=False) as session:
            actor = await session.get(User, a['id'])
            roles = await roles_of(session, actor) if actor and actor.active else set()
            booking = await session.get(Booking, positive_id(request.match_info['id']),
                                        with_for_update=request.method != 'GET')
            if not roles or not await can_access_booking(session, a['id'], roles, booking):
                raise AccessError('Запись недоступна.', 403)
            can_edit = bool(roles & {'OWNER', 'ADMIN', 'MANAGER'} and (
                roles & {'OWNER', 'ADMIN'} or booking.manager_id == a['id']))
            if request.method != 'GET':
                if not can_edit:
                    raise AccessError('Нет доступа к настройке SMS этой записи.', 403)
                try:
                    if request.path.endswith('/retry'):
                        if body != {}:
                            raise ValueError('Некорректный запрос повтора SMS.')
                        await self.service.retry(session, booking)
                    else:
                        if not isinstance(body, dict) or set(body) != {'enabled', 'consent', 'leadMinutes'}:
                            raise ValueError('Проверьте настройки SMS.')
                        await self.service.configure(session, booking, enabled=body['enabled'],
                            consent=body['consent'], lead_minutes=body['leadMinutes'])
                except ValueError as exc:
                    raise AccessError(str(exc), 409) from exc
                await audit(session, actor, 'guest_sms_preferences', 'booking', booking.id,
                            json.dumps(body, ensure_ascii=False))
                await session.commit()
            return web.json_response(await self.service.status(session, booking, can_edit=can_edit))


def install_guest_sms(app, api):
    feature = GuestSMSAPI(api)
    app['guest_sms'] = feature.service
    app.router.add_get('/api/miniapp/bookings/{id}/sms-reminder', feature.handle)
    app.router.add_put('/api/miniapp/bookings/{id}/sms-reminder', feature.handle)
    app.router.add_post('/api/miniapp/bookings/{id}/sms-reminder/retry', feature.handle)
    return feature
