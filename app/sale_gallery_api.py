"""Sale-scoped QR and explicit recovery of already accepted originals."""
import io

from aiohttp import web
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .launch_policy import app_url
from .miniapp_security import AccessError
from .models import Booking, Sale, SaleDraft, SaleDraftPhoto, Setting, User
from .services.core import roles_of
from .services.sale_gallery import (
    gallery_results,
    link_draft,
    storage_identity,
    verify_original,
)
from .services.sale_workflow import can_sell
from .yandex_disk import YandexDiskError


class SaleGalleryAPI:
    def __init__(self, api):
        self.api = api

    async def authorize(self, session, request, *, lock=False):
        a = request['miniapp_actor']
        actor = await session.get(User, a['id'])
        roles = (await roles_of(session, actor)) & set(a['roles']) if actor else set()
        try:
            if 'sale' in request.match_info:
                sale = await session.get(Sale, int(request.match_info['sale']))
                draft = await session.get(SaleDraft, sale.source_draft_id) if sale and sale.source_draft_id else None
            else:
                draft = await session.get(SaleDraft, int(request.match_info['draft']))
                sale = await session.scalar(select(Sale).where(Sale.source_draft_id == draft.id)) if draft else None
        except (TypeError, ValueError) as exc:
            raise AccessError('Продажа не найдена.', 404) from exc
        booking = await session.get(Booking, draft.booking_id, with_for_update=lock) if draft else None
        if (not actor or not actor.active or not draft or not booking
                or not await can_sell(actor, roles, booking, session=session)):
            raise AccessError('Нет доступа к фотографиям этой продажи.', 403)
        if draft.status == 'CANCELLED':
            raise AccessError('Продажа отменена. Фотографии не открыты гостю.', 409)
        return actor, booking, draft, sale

    async def qr(self, request):
        async with AsyncSession(self.api.engine) as session:
            _actor, booking, _draft, sale = await self.authorize(session, request)
            summaries = await gallery_results(session, [booking.id], app_url().split('/app/', 1)[0],
                                             sale_ids=[sale.id])
            result = summaries[booking.id]
            if not result['ready']:
                raise AccessError('Ссылка пока не готова. Проверьте оплату и состояние галереи.', 409)
        import qrcode
        output = io.BytesIO()
        qrcode.make(result['clientUrl']).save(output, format='PNG')
        return web.Response(body=output.getvalue(), content_type='image/png',
                            headers={'Cache-Control': 'no-store'})

    @staticmethod
    def snapshot(photos):
        return [(p.id, p.draft_id, p.storage_path, p.sha256, p.byte_size) for p in photos]

    @staticmethod
    async def generations(session):
        return [(s.key, s.value) for s in await session.scalars(select(Setting).where(
            Setting.key.in_(['operations:generation', 'sales:generation'])).order_by(Setting.key))]

    async def sync(self, request):
        if await self.api.body(request) != {}:
            raise AccessError('Некорректный запрос восстановления галереи.', 400)
        async with AsyncSession(self.api.engine, expire_on_commit=False) as session:
            _actor, booking, draft, _sale = await self.authorize(session, request)
            before = draft.id, draft.status, draft.created_by_id, booking.id
            photos = list(await session.scalars(select(SaleDraftPhoto).where(
                SaleDraftPhoto.draft_id == draft.id).order_by(SaleDraftPhoto.id)))
            snapshot = self.snapshot(photos)
            generations = await self.generations(session)
        identity = storage_identity()
        try:
            for photo in photos:
                async with request.app['delivery'].download_slots:
                    await verify_original(self.api.engine, self.api.bot, photo)
        except ValueError as exc:
            raise AccessError(str(exc), 409) from exc
        except (YandexDiskError, OSError, TimeoutError) as exc:
            raise AccessError('Не удалось подтвердить оригиналы на подключённом Диске. Фотографии и продажа сохранены.', 503) from exc
        async with AsyncSession(self.api.engine) as session:
            _actor, booking, draft, sale = await self.authorize(session, request, lock=True)
            current = list(await session.scalars(select(SaleDraftPhoto).where(
                SaleDraftPhoto.draft_id == draft.id).order_by(SaleDraftPhoto.id)))
            if (before != (draft.id, draft.status, draft.created_by_id, booking.id)
                    or snapshot != self.snapshot(current) or identity != storage_identity()
                    or generations != await self.generations(session)):
                raise AccessError('Состояние продажи или подключённый Диск изменились. Повторите проверку.', 409)
            try:
                await link_draft(session, draft, verified=True)
            except ValueError as exc:
                raise AccessError(str(exc), 409) from exc
            summaries = await gallery_results(session, [booking.id], app_url().split('/app/', 1)[0],
                                             sale_ids=[sale.id] if sale else [])
            result = summaries[booking.id]
            draft_id = draft.id
            await session.commit()
        return web.json_response({'draftId': draft_id, 'linkedPhotos': len(current), 'gallery': result})


def install_sale_gallery(app, api):
    feature = SaleGalleryAPI(api)
    app.router.add_get(r'/api/miniapp/sales/{sale:\d+}/gallery-qr', feature.qr)
    app.router.add_post(r'/api/miniapp/sales/{sale:\d+}/gallery/sync', feature.sync)
    app.router.add_post(r'/api/miniapp/sale-drafts/{draft:\d+}/gallery/sync', feature.sync)
    return feature
