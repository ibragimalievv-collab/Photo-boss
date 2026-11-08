"""Optional new-workspace scheduling boundary; never filters historical reads."""
from datetime import date
import re


class OperationalStartError(ValueError):
    """A requested shooting belongs before the configured working period."""


def parse_start_date(value):
    if value in (None, ''):
        return None
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
        raise ValueError('PHOTO_BOSS_OPERATIONAL_START_DATE: укажите дату в формате ГГГГ-ММ-ДД')
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError('PHOTO_BOSS_OPERATIONAL_START_DATE: укажите существующую дату') from None


def configured_start_date():
    # Lazy import keeps Config.validate() able to validate this pure parser.
    from .config import config
    return parse_start_date(getattr(config, 'operational_start_date', ''))


def require_booking_date(day):
    """Validate the already localized shooting day, not its capture/upload day."""
    start = configured_start_date()
    if start is not None and day < start:
        raise OperationalStartError(
            f'Новый рабочий период начинается {start:%d.%m.%Y}. '
            'Выберите эту или более позднюю дату съёмки.')


def require_cash_date(day):
    start = configured_start_date()
    if start is not None and day < start:
        raise OperationalStartError(
            f'Новый рабочий период начинается {start:%d.%m.%Y}. '
            'Расход с более ранней датой нельзя автоматически переносить в новый период. '
            'Проверьте его в истории; не меняйте дату фактической оплаты.')


def stale_operation_message():
    start = configured_start_date()
    if start is None:
        return None
    return (f'Новый рабочий период начинается {start:%d.%m.%Y}. '
            'Это действие относится к прежнему периоду. Проверьте историю перед новой записью; '
            'автоматически повторять старое действие не нужно.')
