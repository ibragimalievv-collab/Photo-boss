"""Platform-owner registry for a controlled company pilot.

No company role is mapped to a global staff role. Work-module tenant isolation
must precede external staff onboarding; registry activation cannot bypass it.
"""
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiohttp import web
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .miniapp_security import AccessError, require_owner
from .models import CompanyGallery, DeliveryGallery, GuestAccount, PhotoCompany, User
from .services.core import audit


def describe(company):
    return {'id': company.id, 'name': company.name, 'country': company.country,
            'timezone': company.timezone, 'currency': company.currency, 'active': company.active,
            'monthlyPrice': 0, 'usageRights': 'WORK_ONLY', 'staffOnboardingEnabled': False}


def validate(body):
    if set(body) != {'name', 'country', 'timezone', 'currency'}:
        raise AccessError('Укажите название, страну, часовой пояс и валюту.', 400)
    if not isinstance(body['name'], str) or not 1 <= len(body['name'].strip()) <= 150:
        raise AccessError('Название компании: от 1 до 150 символов.', 400)
    for field, pattern in [('country', r'[A-Z]{2}'), ('currency', r'[A-Z]{3}')]:
        if not isinstance(body[field], str) or not re.fullmatch(pattern, body[field]):
            raise AccessError('Укажите коды страны и валюты латинскими буквами.', 400)
    if not isinstance(body['timezone'], str) or len(body['timezone']) > 80:
        raise AccessError('Некорректный часовой пояс.', 400)
    try:
        ZoneInfo(body['timezone'])
    except (ZoneInfoNotFoundError, ValueError):
        raise AccessError('Неизвестный часовой пояс.', 400) from None
    return {**body, 'name': body['name'].strip()}


class Companies:
    def __init__(self, api):
        self.api = api

    async def listing(self, request):
        actor = request['miniapp_actor']
        require_owner(actor['roles'])
        async with AsyncSession(self.api.engine, expire_on_commit=False) as session:
            if request.method == 'POST':
                body = validate(await self.api.body(request))
                company = PhotoCompany(**body, created_by_id=actor['id'])
                session.add(company)
                await session.flush()
                await audit(session, await session.get(User, actor['id']), 'company_created', 'company', company.id)
                await session.commit()
                return web.json_response(describe(company), status=201)
            rows = (await session.scalars(select(PhotoCompany).order_by(PhotoCompany.id))).all()
            count = await session.scalar(select(func.count(GuestAccount.id)))
            return web.json_response({'companies': [describe(c) for c in rows], 'registeredGuests': count,
                'staffOnboardingEnabled': False, 'pilotNotice': 'Реестр компаний. Подключение внешних сотрудников откроется после изоляции всех рабочих разделов.'})

    async def configure(self, request):
        actor = request['miniapp_actor']
        require_owner(actor['roles'])
        body = await self.api.body(request)
        if set(body) != {'active'} or type(body['active']) is not bool:
            raise AccessError('Некорректный статус компании.', 400)
        async with AsyncSession(self.api.engine, expire_on_commit=False) as session:
            company = await session.get(PhotoCompany, int(request.match_info['company']), with_for_update=True)
            if not company:
                raise AccessError('Компания не найдена.', 404)
            company.active = body['active']
            await audit(session, await session.get(User, actor['id']), 'company_status_changed', 'company', company.id)
            await session.commit()
            return web.json_response(describe(company))

    async def galleries(self, request):
        actor = request['miniapp_actor']
        require_owner(actor['roles'])
        async with AsyncSession(self.api.engine, expire_on_commit=False) as session:
            company = await session.get(PhotoCompany, int(request.match_info['company']), with_for_update=True)
            if not company:
                raise AccessError('Компания не найдена.', 404)
            if request.method == 'POST':
                body = await self.api.body(request)
                if set(body) != {'galleryId'} or type(body['galleryId']) is not int:
                    raise AccessError('Укажите номер альбома.', 400)
                gallery = await session.get(DeliveryGallery, body['galleryId'], with_for_update=True)
                if not gallery:
                    raise AccessError('Альбом не найден.', 404)
                mapping = await session.get(CompanyGallery, gallery.id)
                if mapping and mapping.company_id != company.id:
                    raise AccessError('Альбом уже относится к другой компании.', 409)
                if not mapping:
                    session.add(CompanyGallery(gallery_id=gallery.id, company_id=company.id))
                    await audit(session, await session.get(User, actor['id']), 'company_gallery_linked', 'gallery', gallery.id)
                    await session.commit()
            rows = (await session.scalars(select(DeliveryGallery).join(CompanyGallery,
                    CompanyGallery.gallery_id == DeliveryGallery.id).where(CompanyGallery.company_id == company.id))).all()
            return web.json_response({'galleries': [{'id': g.id, 'title': g.title, 'published': g.published} for g in rows]})


def install_companies(app, api):
    service = Companies(api)
    for method in ('GET', 'POST'):
        app.router.add_route(method, '/api/miniapp/companies', service.listing)
        app.router.add_route(method, r'/api/miniapp/companies/{company:\d+}/galleries', service.galleries)
    app.router.add_post(r'/api/miniapp/companies/{company:\d+}', service.configure)
    return service
