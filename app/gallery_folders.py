"""Shared staff gallery grouping and safe, unique destinations for new originals."""
import re

from .yandex_disk import ROOT, safe_path

MONTHS = ('Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
          'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь')


def folder_path(booking, hotel_name, photographer_name):
    """Use stored shoot assignments, never the staff member viewing/uploading."""
    day = booking.shoot_date
    hotel = (hotel_name or '').strip()
    photographer = (photographer_name or '').strip()
    return [
        {'key': f'hotel:{booking.hotel_id}' if booking.hotel_id else 'hotel:unknown',
         'label': hotel or 'Отель не указан', 'sort': hotel.casefold() or '~'},
        {'key': day.strftime('%Y-%m') if day else 'month:unknown',
         'label': f'{MONTHS[day.month-1]} {day.year}' if day else 'Месяц не указан',
         'sort': day.strftime('%Y-%m') if day else '~'},
        {'key': day.isoformat() if day else 'day:unknown',
         'label': str(day.day) if day else 'Число не указано',
         'sort': day.isoformat() if day else '~'},
        {'key': f'photographer:{booking.photographer_id}' if booking.photographer_id else 'photographer:unknown',
         'label': photographer or 'Фотограф не указан', 'sort': photographer.casefold() or '~'},
    ]


def segment(value, fallback, limit=55):
    value = re.sub(r'[^A-Za-zА-Яа-яЁё0-9._()#-]', '_', str(value or ''))
    return value[:limit].strip('._') or fallback


def storage_folder(booking, client_name, hotel_name, photographer_name):
    path = folder_path(booking, hotel_name, photographer_name)
    hotel = segment(hotel_name, 'Отель_не_указан') + f'#{booking.hotel_id or "unknown"}'
    photographer = segment(photographer_name, 'Фотограф_не_указан') + f'#{booking.photographer_id or "unknown"}'
    month = segment(path[1]['label'], 'Месяц_не_указан')
    day = f'{booking.shoot_date.day:02d}' if booking.shoot_date else 'Число_не_указано'
    stamp = str(booking.shoot_time)[:5].replace(':', '-') if booking.shoot_time else 'Время_не_указано'
    guest = segment(client_name, 'Гость_не_указан')
    room = '_номер-' + segment(booking.room, 'не_указан', 25) if booking.room else ''
    shoot = f'{stamp}_{guest}{room}_booking-{booking.id}'
    return safe_path(f'{ROOT}/delivery/{hotel}/{month}/{day}/{photographer}/{shoot}')


def directory_chain(folder):
    safe_path(folder)
    if not folder.startswith(ROOT + '/delivery/'):
        raise ValueError('Invalid delivery folder')
    parts = folder[len(ROOT) + 1:].split('/')
    return [ROOT, *(ROOT + '/' + '/'.join(parts[:i]) for i in range(1, len(parts)+1))]


def booking_storage_root(path, booking_id):
    """Only roots owned by this booking are eligible for explicit trash purge."""
    safe_path(path)
    legacy = ROOT + f'/delivery/booking-{booking_id}'
    if path.startswith(legacy + '/'):
        return legacy
    prefix = ROOT + '/delivery/'
    if path.startswith(prefix):
        parts = path[len(prefix):].split('/')
        if (len(parts) == 7 and parts[4].endswith(f'_booking-{booking_id}')
                and parts[5] in {'Все фото', 'Выбранные'}):
            return prefix + '/'.join(parts[:5])
    raise ValueError('Photo is outside this booking delivery folder')


def selected_folder(path, booking_id):
    """Retain the original's location when selecting an older uploaded file."""
    booking_storage_root(path, booking_id)
    folder, _filename = path.rsplit('/', 1)
    if not folder.endswith('/Все фото'):
        raise ValueError('Original photo folder is unknown')
    return folder.rsplit('/', 1)[0] + '/Выбранные'
