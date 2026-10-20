"""Deterministic checks of private Disk originals, independent of AI success."""
import asyncio
import hashlib
import io
import json
import math

from PIL import Image, ImageOps
from sqlalchemy import select

from ..models import (
    Booking,
    Photo,
    PhotoStorage,
    Setting,
    ShootDevelopmentReview,
    Shooting,
)


def inspect_original(raw, photo_id, expected_hash=None, expected_size=None):
    digest = hashlib.sha256(raw).hexdigest()
    if ((expected_hash and expected_hash != digest)
            or (expected_size is not None and expected_size != len(raw))):
        raise ValueError('Оригинал на Диске отличается от сохранённой контрольной суммы или размера.')
    with Image.open(io.BytesIO(raw)) as image:
        if image.width * image.height > 60_000_000:
            raise ValueError('Превышен предел размера изображения для проверки.')
        image.load()
        width, height = image.size
        exif = image.getexif()
        location = {'status': 'missing', 'note': 'GPS в исходнике отсутствует; место съёмки не подтверждено.'}
        try:
            gps = exif.get_ifd(0x8825)
            if gps:
                def coordinate(values, ref, maximum):
                    degrees, minutes, seconds = map(float, values)
                    if not (0 <= degrees <= maximum and 0 <= minutes < 60 and 0 <= seconds < 60):
                        raise ValueError
                    result = degrees + minutes / 60 + seconds / 3600
                    result *= -1 if ref in ('S', 'W', b'S', b'W') else 1
                    if not math.isfinite(result) or abs(result) > maximum:
                        raise ValueError
                    return result
                lat = coordinate(gps[2], gps[1], 90)
                lon = coordinate(gps[4], gps[3], 180)
                if gps[1] not in ('N', 'S', b'N', b'S') or gps[3] not in ('E', 'W', b'E', b'W'):
                    raise ValueError
                location = {'status': 'embedded', 'latitude': lat, 'longitude': lon,
                    'note': 'Координаты из EXIF можно изменить; они не доказывают фактическое место съёмки.'}
        except (ValueError, TypeError, KeyError, ZeroDivisionError, OSError, SyntaxError):
            location = {'status': 'invalid', 'note': 'GPS в исходнике не удалось прочитать.'}
        thumb = ImageOps.exif_transpose(image).convert('L').resize((9, 8))
        pixels = list(thumb.get_flattened_data())
        value = sum((pixels[y * 9 + x] > pixels[y * 9 + x + 1]) << (y * 8 + x)
                    for y in range(8) for x in range(8))
        # Featureless pictures must not be reported as perceptually similar.
        useful = max(pixels) - min(pixels) >= 20
    return {'photoId': photo_id, 'status': 'checked', 'source': 'yandex_disk',
        'sha256': digest, 'bytes': len(raw), 'width': width, 'height': height,
        'visualHash': f'{value:016x}' if useful else None, 'location': location}


def audit_summary(files, total):
    checked = [f for f in files if f.get('status') == 'checked']
    exact, similar = [], []
    # Bound quadratic candidate comparisons; exact unique counts still cover all files.
    compared = checked[:1000]
    for i, left in enumerate(compared):
        for right in compared[i + 1:]:
            pair = [left['photoId'], right['photoId']]
            if left['sha256'] == right['sha256']:
                if len(exact) < 100:
                    exact.append(pair)
            elif (len(similar) < 100 and left.get('visualHash') and right.get('visualHash')
                  and (int(left['visualHash'], 16) ^ int(right['visualHash'], 16)).bit_count() <= 5):
                similar.append(pair)
    return {'total': total, 'checked': len(checked), 'failed': len(files) - len(checked),
        'uniqueOriginals': len({f['sha256'] for f in checked}),
        'exactCopies': exact[:100], 'similarCandidates': similar[:100],
        'gpsPresent': sum(f['location']['status'] == 'embedded' for f in checked),
        'complete': len(files) == total,
        'similarityLimited': len(checked) > len(compared),
        'note': 'Похожие кадры — кандидаты на ручную проверку, не автоматический брак. Данные не меняют кассу, штрафы или оплату.'}


def compare_hotel(location, point):
    if location.get('status') != 'embedded':
        return {'status': 'no_gps'}
    if not point:
        return {'status': 'not_configured'}
    lat1, lat2 = math.radians(point['latitude']), math.radians(location['latitude'])
    delta = math.radians(location['longitude'] - point['longitude'])
    a = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta / 2) ** 2
    distance = 6371000 * 2 * math.asin(min(1, math.sqrt(a)))
    return {'status': 'compared_unverified', 'distanceM': round(distance, 1),
        'reportedInside': distance <= point['radiusM'],
        'referencePoint': point,
        'note': 'Сравнение заявленных EXIF-координат с меткой отеля; подлинность GPS не установлена.'}


async def audit_next_files(factory, *, limit=6):
    from .media_files import configured_storage

    async with factory() as session:
        reviews = list(await session.scalars(select(ShootDevelopmentReview).order_by(ShootDevelopmentReview.id)))
        for review in reviews:
            ids = json.loads(review.photo_ids)
            key = f'shoot-audit:{review.id}'
            state = await session.get(Setting, key, with_for_update=True)
            if state is None:
                state = Setting(key=key, value='[]')
                session.add(state)
                await session.flush()
            files = json.loads(state.value)
            remaining = [pid for pid in ids if pid not in {f['photoId'] for f in files}][:limit]
            if not remaining:
                continue
            shooting = await session.get(Shooting, review.shooting_id)
            booking = await session.get(Booking, shooting.booking_id) if shooting else None
            from ..hotel_points import point_key
            point = await session.get(Setting, point_key(booking.hotel_id)) if booking else None
            point = json.loads(point.value) if point else None
            for pid in remaining:
                storage = await session.scalar(select(PhotoStorage).where(PhotoStorage.photo_id == pid))
                photo = await session.get(Photo, pid)
                try:
                    if not photo or not storage or storage.status != 'STORED' or not storage.disk_path:
                        raise ValueError('Оригинал ещё не подтверждён на Яндекс.Диске.')
                    raw = await configured_storage().download_bytes(storage.disk_path)
                    result = await asyncio.to_thread(inspect_original, raw, pid, storage.sha256, storage.byte_size)
                    result['hotelZone'] = compare_hotel(result['location'], point)
                except Exception as exc:  # noqa: BLE001 - isolate unreadable files; no private paths
                    result = {'photoId': pid, 'status': 'unavailable',
                        'error': str(exc) if isinstance(exc, ValueError) else 'Не удалось проверить оригинал на Диске. Запустите повтор.'}
                files.append(result)
            state.value = json.dumps(files, ensure_ascii=False)
            await session.commit()
            return True
        await session.commit()
    return False
