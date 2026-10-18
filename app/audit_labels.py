"""Human-readable audit presentation; stored evidence remains unchanged."""
import json
import re

ENTITIES = {
    'user': 'Сотрудник', 'users': 'Сотрудники', 'user_roles': 'Роли сотрудников',
    'booking': 'Запись на съёмку', 'bookings': 'Записи на съёмку',
    'shooting': 'Съёмка', 'shootings': 'Съёмки', 'sale': 'Продажа', 'sales': 'Продажи',
    'sale_draft': 'Черновик продажи', 'sale_drafts': 'Черновики продаж',
    'receipt': 'Чек', 'receipts': 'Чеки', 'shift': 'Смена', 'shifts': 'График смен',
    'shift_check_ins': 'Начало смены', 'shift_check_outs': 'Окончание смены',
    'payroll_entries': 'Начисления сотрудникам', 'compensation': 'Выплаты сотрудникам',
    'packages': 'Пакеты съёмки', 'hotel': 'Отель', 'hotel_employees': 'Доступ к отелям',
    'bank_reconciliations': 'Подтверждение оплаты', 'cash_movements': 'Движение денег',
    'setting': 'Настройки', 'settings': 'Настройки', 'notification': 'Уведомление',
    'academy_plan': 'План обучения', 'academy_lesson': 'Урок Академии',
    'academy_location': 'Учебная локация', 'training': 'Учебная тренировка',
    'photo': 'Фотография', 'photo_edit': 'Обработка фотографии',
}
FIELDS = {
    'client': 'Имя гостя', 'guestCount': 'Количество гостей', 'photographerId': 'Фотограф', 'shootingStatus': 'Состояние съёмки', 'time': 'Время съёмки', 'booking': 'Запись на съёмку', 'sold': 'Куплено фотографий', 'declared': 'Заявлено кадров', 'selected': 'Загружено купленных кадров', 'full_uploaded': 'Сохранено оригиналов', 'photographer_percent': 'Процент фотографа', 'id': 'Номер', 'name': 'Имя / название', 'username': 'Имя в Telegram',
    'tg_id': 'Номер аккаунта Telegram', 'user_id': 'Сотрудник', 'role': 'Роль',
    'roles': 'Роли', 'active': 'Доступ активен', 'hotel_id': 'Отель',
    'hotelIds': 'Доступные отели', 'client_id': 'Гость', 'client_name': 'Имя гостя',
    'client_phone': 'Телефон гостя', 'phone': 'Телефон', 'room': 'Номер комнаты',
    'guest_count': 'Количество гостей', 'shoot_date': 'Дата съёмки',
    'shoot_time': 'Время съёмки', 'status': 'Состояние', 'manager_id': 'Менеджер записи',
    'photographer_id': 'Фотограф', 'package_id': 'Пакет съёмки', 'deposit': 'Заявленная предоплата, ₽',
    'amount': 'Сумма, ₽', 'sold_photos': 'Куплено фотографий',
    'declared_photo_count': 'Заявлено кадров', 'payment_status': 'Состояние оплаты',
    'booking_id': 'Запись на съёмку', 'shooting_id': 'Съёмка', 'receipt_id': 'Чек',
    'credited_user_id': 'Получатель начисления', 'kind': 'Вид операции',
    'type': 'Тип', 'reason': 'Причина', 'comment': 'Комментарий', 'note': 'Примечание',
    'cancellation_reason': 'Причина отмены', 'cancelled_at': 'Время отмены',
    'created_at': 'Время создания', 'updated_at': 'Время изменения',
    'start_time': 'Начало смены', 'end_time': 'Конец смены', 'shift_date': 'Дата смены',
    'checked_at': 'Время отметки', 'started_at': 'Начало', 'finished_at': 'Окончание',
    'completed_at': 'Время завершения', 'accepted_at': 'Съёмка принята',
    'arrived_at': 'Прибытие фотографа', 'ready_for_sale_at': 'Готовность к продаже',
    'full_upload_completed_at': 'Полная загрузка завершена', 'terminated_at': 'Доступ отключён',
    'expected_amount': 'Ожидаемая сумма, ₽', 'price_per_photo': 'Цена фотографии, ₽',
    'percent': 'Процент', 'value': 'Значение', 'key': 'Настройка',
    'source': 'Источник', 'capture': 'Как записано событие', 'date': 'Дата',
    'revision': 'Версия записи', 'track': 'Направление обучения',
    'mode': 'Режим разговора', 'maxSeconds': 'Лимит разговора, секунд',
    'idleMinutes': 'Интервал без записей, минут', 'enabled': 'Включено',
    'deleted': 'В корзине', 'daily': 'Ежедневная сводка', 'critical': 'Критические события',
    'lat': 'Широта', 'lon': 'Долгота', 'accuracy': 'Точность геопозиции, м',
    'paid_at': 'Время оплаты', 'matched_at': 'Время сверки', 'matched_by_id': 'Кто сверил',
    'bank_reference': 'Номер банковской операции', 'provider': 'Сервис',
}
VALUES = {
    'OWNER': 'Владелец', 'ADMIN': 'Администратор', 'MANAGER': 'Менеджер записи',
    'PHOTOGRAPHER': 'Фотограф', 'ACTIVE': 'В работе', 'COMPLETED': 'Завершено',
    'NEW': 'Новая запись', 'PENDING_CONFIRMATION': 'Ожидает подтверждения',
    'CONFIRMED': 'Подтверждено', 'ASSIGNED': 'Назначено', 'RESCHEDULED': 'Перенесено',
    'CANCELLED': 'Отменено', 'REJECTED': 'Отклонено', 'PICKED_UP': 'Съёмка принята',
    'ACCEPTED': 'Съёмка принята', 'ARRIVED': 'Фотограф на месте', 'SHOOTING': 'Идёт съёмка',
    'SHOT': 'Съёмка отснята', 'READY_FOR_MANAGER': 'На обработке',
    'READY_FOR_SALE': 'Готово к продаже', 'SOLD': 'Продано', 'PLANNED': 'Запланировано',
    'STARTED': 'На смене', 'FINISHED': 'Смена завершена', 'PAID': 'Оплачено',
    'PARTIAL': 'Частично оплачено', 'UNPAID': 'Ожидает оплаты', 'PENDING': 'Ожидает проверки',
    'PENDING_REVIEW': 'На проверке', 'APPROVED': 'Подтверждено', 'FAILED': 'Ошибка',
    'STORED': 'Сохранено на Диске', 'UPLOADING': 'Загружается', 'RUNNING': 'Выполняется',
    'database': 'Автоматическая запись базы данных', 'photographer': 'Обучение фотографа',
    'booking': 'Обучение менеджера записи', 'MANUAL': 'Ручная сверка',
    'MATCHED': 'Совпадает', 'MISMATCH': 'Не совпадает',
    'coach': 'ИИ-наставник', 'guest': 'Учебный гость',
}
ACTIONS = {
    'bank_reconciled': 'Поступление оплаты сверено с банком',
    'candidate_created': 'Добавлен кандидат', 'candidate_hired': 'Кандидат принят в команду',
    'candidate_updated': 'Обновлена карточка кандидата', 'checklist_configured': 'Изменён контрольный список смены',
    'delivery_campaign_created': 'Подготовлена рассылка гостям', 'delivery_gallery_configured': 'Изменена клиентская галерея',
    'delivery_handoff': 'Изменена отметка выдачи фотографий', 'delivery_photo_selected': 'Изменён выбор фотографий гостя',
    'delivery_photo_uploaded': 'Загружена фотография в галерею', 'employee_roles_added': 'Добавлены роли сотруднику',
    'full_shoot_upload_completed': 'Подтверждена полная загрузка съёмки', 'guest_reminder_prepared': 'Подготовлено напоминание гостю',
    'hotel_created': 'Добавлен отель', 'miniapp_employee_updated': 'Обновлены данные сотрудника',
    'offline_attendance_received': 'Принята отметка смены, сделанная без сети',
    'offline_attendance_reviewed': 'Проверена отметка смены, сделанная без сети',
    'package_created': 'Добавлен пакет съёмки', 'paid_expense_recorded': 'Записан оплаченный расход',
    'paid_expense_voided': 'Отменена запись о расходе', 'sale_draft_started': 'Начато оформление продажи',
    'sale_counts_corrected': 'Исправлено количество кадров в продаже',
    'sale_receipt_captured': 'Принят чек продажи', 'setting_changed': 'Изменена настройка',
    'shift_check_in_started': 'Начато подтверждение прихода на смену',
    'shift_check_out_started': 'Начато подтверждение окончания смены',
    'shift_end_location_received': 'Получена геопозиция при окончании смены',
    'shift_location_received': 'Получена геопозиция при начале смены',
    'shift_report_saved': 'Сохранён отчёт о смене', 'smart_assignment': 'Подготовлено распределение съёмок',
    'training_started': 'Начата учебная практика', 'training_submitted': 'Практика отправлена на проверку',
    'training_uncertain_retake': 'Назначена повторная проверка практики',
    'work_chat_message_deleted': 'Удалено сообщение рабочего чата',
    'work_chat_owner_thread_opened': 'Открыт рабочий диалог с владельцем',
    'work_rules_accepted': 'Подтверждены рабочие правила', 'academy_training_turn': 'Сохранён ответ в учебной тренировке',
    'booking_updated': 'Изменены данные записи', 'booking_rescheduled': 'Съёмка перенесена',
    'booking_cancelled': 'Запись отменена', 'booking_trashed': 'Запись перемещена в корзину',
    'booking_restored': 'Запись восстановлена из корзины',
    'notification_preferences_updated': 'Изменены настройки уведомлений',
    'booking_control_updated': 'Изменён интервал напоминаний менеджерам',
    'event_acknowledged': 'Уведомление просмотрено', 'shoot_storage_recovery': 'Повторно запущено сохранение фотографий',
    'shoot_photo_uploaded': 'Принята фотография съёмки', 'receipt_uploaded': 'Загружен чек',
    'gallery_project_preferences': 'Изменены настройки клиентской галереи',
    'gallery_project_purged': 'Удалены фотографии проекта', 'gallery_defaults_changed': 'Изменены настройки галерей',
    'miniapp_photographer_assigned': 'Назначен фотограф', 'shooting_pickup': 'Фотограф принял съёмку',
    'shooting_start': 'Начата съёмка', 'shooting_finish': 'Завершена съёмка',
    'shooting_ready': 'Съёмка подготовлена к продаже', 'academy_voice_turn': 'Сохранён голосовой ответ в тренировке',
    'academy_realtime_start': 'Начат живой голосовой разговор с ИИ',
    'academy_realtime_finish': 'Сохранён разбор живого разговора с ИИ',
}
SECRET = re.compile(r'password|secret|token|credential|hash|file_id|disk_path|storage_path', re.IGNORECASE)


def safe_payload(value):
    if isinstance(value, dict):
        return {k: safe_payload(v) for k, v in value.items() if not SECRET.search(k)}
    if isinstance(value, list):
        return [safe_payload(v) for v in value]
    return value


def readable(value, key=''):
    if value is None or value == '':
        return 'Не указано'
    if type(value) is bool:
        return 'Да' if value else 'Нет'
    if isinstance(value, (list, dict)):
        parts = ([f'{FIELDS.get(k, "Дополнительное поле")}: {readable(v, k)}' for k, v in value.items()]
                 if isinstance(value, dict) else [readable(v) for v in value])
        return '; '.join(parts) or 'Нет данных'
    return VALUES.get(str(value), str(value))


def presentation(action, entity, details, titles):
    label = ENTITIES.get(entity, 'Рабочие данные')
    title = ACTIONS.get(action) or titles.get(action)
    if action.startswith('row.'):
        title = {'row.insert': 'Создана запись', 'row.update': 'Изменена запись',
                 'row.delete': 'Удалена запись'}.get(action, 'Изменены данные') + ' · ' + label.lower()
    title = title or 'Зарегистрировано служебное событие'
    try:
        payload = safe_payload(json.loads(details or '{}'))
    except (ValueError, TypeError):
        payload = None
    changes, facts = [], []
    if isinstance(payload, dict):
        before, after = payload.get('before'), payload.get('after')
        if 'before' in payload or 'after' in payload:
            before = before if isinstance(before, dict) else {'value': before}
            after = after if isinstance(after, dict) else {'value': after}
            for key in sorted(before.keys() | after.keys()):
                if before.get(key) != after.get(key):
                    changes.append({'label': FIELDS.get(key, 'Дополнительное поле'),
                                    'field': key, 'before': readable(before.get(key), key),
                                    'after': readable(after.get(key), key)})
        for key, value in payload.items():
            if key not in ('before', 'after'):
                facts.append({'label': FIELDS.get(key, 'Дополнительные сведения'), 'value': readable(value, key)})
    text = (details or 'Дополнительные сведения не были записаны.') if payload is None else ''
    return {'title': title, 'entityTitle': label, 'changes': changes, 'facts': facts,
            'description': 'Событие сохранено в истории Photo Boss. Ниже приведены только записанные сведения.',
            'detailsText': text, 'details': json.dumps(payload, ensure_ascii=False) if payload is not None else text}
