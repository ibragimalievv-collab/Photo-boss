"""Prepared Disk media never waits on the provider under a sale transaction."""
import asyncio
import json

import pytest
from sqlalchemy import func, select
from test_disk_sale_media import prepared

from app.disk_sale_media import (
    BINDING,
    FILE_PREFIX,
    REFERENCE,
    binding,
    prepare_selection,
    reference,
    select_photo,
    selection_reference,
    verify_saved_reference,
)
from app.models import Booking, Sale, SaleDraft, SaleDraftPhoto, Setting, User
from app.services.sale_workflow import complete_sale


def test_prepared_selection_and_completion_release_provider_before_sale_transaction(monkeypatch):
    async def run():
        engine, factory, _, _, _, disk, did = await prepared(monkeypatch)
        try:
            async with factory() as session:
                draft = await session.get(SaleDraft, did)
                bid = draft.booking_id
                source = await binding(session, bid)
            data = [{'revision': source['revision'], 'path': path} for path in
                    ('disk:/Hotel/a.jpg', 'disk:/Hotel/Family/b.jpg')]
            originals = [await prepare_selection(source, item) for item in data]
            refs = dict(selection_reference(did, bid, original) for original in originals)
            # A regression into either metadata/download from the locked phase
            # fails immediately, even though all financial records are valid.
            disk.disk_metadata.side_effect = AssertionError('Provider read retained a sale transaction')
            disk.disk_download_bytes.side_effect = AssertionError('Provider download retained a sale transaction')
            async with factory() as session:
                actor = await session.get(User, 1)
                draft = await session.get(SaleDraft, did)
                booking = await session.get(Booking, bid)
                for item, original in zip(data, originals, strict=True):
                    await select_photo(session, actor, booking, draft, item, prepared=original)
                result = await complete_sale(session, actor, did, verified_references=refs)
                assert result.sale.sold_photos == 2 and result.sale.amount == 800
                await session.commit()
                assert await session.scalar(select(func.count(Sale.id))) == 1
                assert await session.scalar(select(func.count(SaleDraftPhoto.id))) == 2
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_prepared_selection_rechecks_folder_binding_before_linking(monkeypatch):
    async def run():
        engine, factory, _, _, _, disk, did = await prepared(monkeypatch)
        try:
            async with factory() as session:
                draft = await session.get(SaleDraft, did)
                bid = draft.booking_id
                source = await binding(session, bid)
            data = {'revision': source['revision'], 'path': 'disk:/Hotel/a.jpg'}
            original = await prepare_selection(source, data)
            calls = disk.disk_metadata.await_count
            async with factory() as session:
                row = await session.get(Setting, BINDING + str(bid))
                row.value = json.dumps(source | {'account': 'changed-after-download'})
                await session.commit()
            async with factory() as session:
                actor = await session.get(User, 1)
                draft = await session.get(SaleDraft, did)
                booking = await session.get(Booking, bid)
                with pytest.raises(ValueError, match='Папка съёмки изменилась'):
                    await select_photo(session, actor, booking, draft, data, prepared=original)
                assert not await session.scalar(select(SaleDraftPhoto.id))
            assert disk.disk_metadata.await_count == calls
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize('change', ['missing', 'changed'])
def test_prepared_completion_rejects_new_or_changed_reference_without_provider_io(monkeypatch, change):
    async def run():
        engine, factory, _, _, source, disk, did = await prepared(monkeypatch)
        try:
            async with factory() as session:
                actor = await session.get(User, 1)
                draft = await session.get(SaleDraft, did)
                booking = await session.get(Booking, draft.booking_id)
                for path in ('disk:/Hotel/a.jpg', 'disk:/Hotel/Family/b.jpg'):
                    await select_photo(session, actor, booking, draft,
                                       {'revision': source['revision'], 'path': path})
                await session.commit()
            async with factory() as session:
                file_ids = list(await session.scalars(select(SaleDraftPhoto.storage_path)))
                refs = {file_id: await reference(session, file_id) for file_id in file_ids}
            for saved in refs.values():
                await verify_saved_reference(saved)
            if change == 'missing':
                refs.pop(file_ids[-1])
            else:
                async with factory() as session:
                    row = await session.get(Setting, REFERENCE + file_ids[-1][len(FILE_PREFIX):])
                    row.value = json.dumps(refs[file_ids[-1]] | {'name': 'relinked-after-verification.jpg'})
                    await session.commit()
            disk.disk_metadata.side_effect = AssertionError('Provider read retained a sale transaction')
            async with factory() as session:
                with pytest.raises(ValueError, match='Фотография на Диске изменилась'):
                    await complete_sale(session, await session.get(User, 1), did,
                                        verified_references=refs)
                assert not await session.scalar(select(Sale.id))
                assert (await session.get(SaleDraft, did)).status == 'AWAITING_SELECTED'
        finally:
            await engine.dispose()
    asyncio.run(run())
