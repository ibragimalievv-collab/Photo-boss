"""Safe titles and internal destinations for owner work notifications."""
import re

WORK_EVENTS = {
    'booking_created': 'Создана запись', 'booking_updated': 'Запись изменена',
    'booking_rescheduled': 'Съёмка перенесена', 'booking_cancelled': 'Съёмка отменена',
    'booking_trashed': 'Запись перемещена в корзину', 'booking_restored': 'Запись восстановлена',
    'receipt_uploaded': 'Загружен чек предоплаты', 'sale_receipt_captured': 'Загружен чек продажи',
    'shift_report_saved': 'Сохранён отчёт смены', 'break_corrected': 'Исправлена отметка обеда',
    'hotel_tariff_saved': 'Изменены цены отеля', 'candidate_created': 'Новый кандидат',
    'candidate_updated': 'Кандидат обновлён', 'candidate_hired': 'Кандидат принят',
    'miniapp_employee_updated': 'Изменены данные сотрудника',
    'miniapp_owner_access_changed': 'Изменён доступ владельца',
    'delivery_handoff': 'Фотографии переданы гостю',
    'full_shoot_upload_completed': 'Завершена загрузка съёмки',
    'shoot_storage_recovery': 'Запущено восстановление фотографий',
}


def work_event_title(action, legacy_text=''):
    if action in WORK_EVENTS:
        return WORK_EVENTS[action]
    for title in WORK_EVENTS.values():
        if legacy_text.startswith(title + '.'):
            return title
    return 'Рабочее событие'


def work_event_url(value):
    if isinstance(value, str) and re.fullmatch(
            r'/app/#(?:home|more|finance|audit|team|workday|attendance|'
            r'shootings(?:\?booking=[1-9][0-9]{0,17})?|workflow(?:\?booking=[1-9][0-9]{0,17})?)', value):
        return value
    return '/app/#home'
