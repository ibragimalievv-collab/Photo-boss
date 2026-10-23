"""Gallery archives never expose user filenames as archive paths."""
import asyncio
import io
import zipfile

from sqlalchemy.ext.asyncio import AsyncSession
from test_delivery import image, setup

from app.models import DeliveryGallery, DeliveryPhoto


def test_public_and_staff_archives_flatten_untrusted_legacy_filenames(monkeypatch):
    async def run():
        engine, service, client, _ = await setup(monkeypatch)
        try:
            async with AsyncSession(engine) as session:
                gallery = DeliveryGallery(booking_id=1, access_token='A' * 32,
                    title='Test', created_by_id=1, published=True, delivery_mode='ALL')
                session.add(gallery)
                await session.flush()
                names = ['../../../outside.png', r'..\..\outside.png',
                    '/absolute.png', 'C:\\folder\\frame.png', 'normal.png',
                    '..', 'bad\x00name.png', 'line\r\nname.png']
                session.add_all([DeliveryPhoto(id=i, gallery_id=gallery.id,
                    uploaded_by_id=2, filename=name, disk_path=f'app:/test/{i}',
                    sha256=str(i) * 64, byte_size=len(image()))
                    for i, name in enumerate(names, start=1)])
                await session.commit()
            async def content(_):
                return image()
            monkeypatch.setattr(service, 'bytes', content)
            for url, headers in [('/g/' + 'A' * 32 + '/album.zip', {}),
                    ('/api/miniapp/galleries/1/archive', {'X-Test-Actor': '1'})]:
                response = await client.get(url, headers=headers)
                assert response.status == 200
                with zipfile.ZipFile(io.BytesIO(await response.read())) as archive:
                    members = archive.namelist()
                    assert len(members) == len(names)
                    assert len(set(members)) == len(members)
                    assert '5-normal.png' in members
                    for member in members:
                        assert '/' not in member and '\\' not in member
                        assert ':' not in member and '\x00' not in member
                        assert '\r' not in member and '\n' not in member
                        assert archive.read(member) == image()
        finally:
            await client.close()
            await engine.dispose()
    asyncio.run(run())
