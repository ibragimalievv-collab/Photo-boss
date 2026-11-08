"""Deterministic admission of extracted receipt evidence, not bank settlement.

The model never receives the expected amount or recipient profile. It extracts
visible fields; this module compares them with server-owned order/config data.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import re
import unicodedata
from zoneinfo import ZoneInfo

VERSION = '2026-11-04-receipt-evidence-v1'
TZ = ZoneInfo('Europe/Moscow')
MESSAGES = {
    'recipient_not_configured': 'Проверка получателя настраивается. Чек сохранён и будет проверен автоматически.',
    'analysis_unavailable': 'Сервис проверки временно недоступен. Повторим проверку автоматически.',
    'not_receipt': 'Не удалось распознать чек. Прикрепите банковский чек целиком.',
    'amount_mismatch': 'Сумма в чеке не совпадает с доплатой по этой записи. Прикрепите нужный чек.',
    'currency_mismatch': 'Нужен чек об оплате в рублях.',
    'recipient_mismatch': 'Получатель в чеке не совпадает с получателем оплаты. Проверьте перевод и прикрепите нужный чек.',
    'recipient_details_mismatch': 'Реквизиты получателя в чеке не совпадают с настройками оплаты.',
    'date_missing_or_invalid': 'Дата или время платежа не прочитаны. Прикрепите более чёткий чек целиком.',
    'date_outside_window': 'Дата платежа не подходит к этой записи. Прикрепите актуальный чек.',
    'operation_not_completed': 'В чеке нет однозначного успешного статуса. Нужен чек выполненного перевода.',
    'operation_id_missing': 'Не прочитан номер операции. Прикрепите чек, на котором он виден.',
    'ambiguous_document': 'Есть неясные данные в чеке. Прикрепите оригинальный чек или более чёткое изображение.',
}


def normalized(value):
    if not isinstance(value, str):
        return ''
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().replace('ё', 'е').split())


def recipient_name(value):
    return ' '.join(re.sub(r'[^\w\s]', '', normalized(value)).split())


@dataclass(frozen=True)
class RecipientProfile:
    names: tuple[str, ...]
    tax_id: str | None = None
    account_last4: str | None = None

    @classmethod
    def parse(cls, value):
        if not isinstance(value, dict) or set(value) - {'names', 'taxId', 'accountLast4'}:
            raise ValueError('invalid_receipt_recipient_profile')
        names = value.get('names')
        if (not isinstance(names, list) or not 1 <= len(names) <= 8
                or any(not isinstance(n, str) or not 5 <= len(n.strip()) <= 300
                       or len(recipient_name(n)) < 5 or any(ord(c) < 32 for c in n) for n in names)):
            raise ValueError('invalid_receipt_recipient_names')
        tax_id = value.get('taxId')
        last4 = value.get('accountLast4')
        if tax_id is not None and (not isinstance(tax_id, str) or not re.fullmatch(r'\d{10}|\d{12}', tax_id)):
            raise ValueError('invalid_receipt_recipient_tax_id')
        if last4 is not None and (not isinstance(last4, str) or not re.fullmatch(r'\d{4}', last4)):
            raise ValueError('invalid_receipt_recipient_reference')
        return cls(tuple(names), tax_id, last4)


@dataclass(frozen=True)
class Decision:
    accepted: bool
    codes: tuple[str, ...]
    operation_key: str | None = None
    amount: Decimal | None = None

    def public(self):
        return {'accepted': self.accepted, 'codes': list(self.codes),
                'messages': [MESSAGES[c] for c in self.codes], 'policyVersion': VERSION,
                'evidenceType': 'AI_RECEIPT', 'bankConfirmed': False}


def operation_identity(fields):
    bank, operation = normalized(fields.get('bank')), normalized(fields.get('operation_id'))
    if (not 2 <= len(bank) <= 100 or not 6 <= len(operation) <= 160
            or not re.fullmatch(r'[a-z0-9\s/._-]+', operation) or not any(c.isdigit() for c in operation)):
        return None
    compact = re.sub(r'[\s.-]', '', bank)
    if compact in {'сбер', 'сбербанк', 'паосбербанк', 'sber', 'sberbank', 'sberbankofrussia'}:
        bank = 'sber'
    elif compact in {'тинькофф', 'тинькоффбанк', 'тбанк', 'tbank', 'tinkoff'}:
        bank = 'tbank'
    else:
        bank = compact
    # Formatting whitespace and case are not distinct transaction identities.
    return hashlib.sha256((bank + ':' + re.sub(r'[\s/._-]+', '', operation)).encode()).hexdigest()


def evaluate(analysis, *, expected, profile, created_at, now=None):
    if profile is None:
        return Decision(False, ('recipient_not_configured',))
    if isinstance(analysis, dict) and analysis.get('status') == 'unreadable':
        return Decision(False, ('not_receipt',))
    if not isinstance(analysis, dict) or analysis.get('status') != 'extracted':
        return Decision(False, ('analysis_unavailable',))
    fields = analysis.get('fields')
    if not isinstance(fields, dict):
        return Decision(False, ('analysis_unavailable',))
    codes = []
    if fields.get('is_receipt') is not True:
        codes.append('not_receipt')
    amount = None
    try:
        raw = fields.get('amount')
        if not isinstance(raw, str) or not re.fullmatch(r'\d{1,8}(?:\.\d{1,2})?', raw):
            raise ValueError
        amount = Decimal(raw)
        expected = Decimal(str(expected))
        if not amount.is_finite() or amount <= 0 or amount != expected:
            raise ValueError
    except (ValueError, TypeError, InvalidOperation):
        codes.append('amount_mismatch')
    if fields.get('currency') != 'RUB':
        codes.append('currency_mismatch')
    if recipient_name(fields.get('recipient')) not in {recipient_name(n) for n in profile.names}:
        codes.append('recipient_mismatch')
    if profile.tax_id and fields.get('tax_id') != profile.tax_id:
        codes.append('recipient_details_mismatch')
    if profile.account_last4 and fields.get('recipient_account_last4') != profile.account_last4:
        codes.append('recipient_details_mismatch')
    now = now or datetime.now(timezone.utc)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    try:
        day, clock = fields.get('date'), fields.get('time')
        if (not isinstance(day, str) or not isinstance(clock, str)
                or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day)
                or not re.fullmatch(r'\d{2}:\d{2}:\d{2}', clock)):
            raise ValueError
        paid_at = datetime.fromisoformat(day + 'T' + clock).replace(tzinfo=TZ)
        if paid_at > now.astimezone(TZ) + timedelta(minutes=5) or paid_at < created_at.astimezone(TZ) - timedelta(days=7):
            codes.append('date_outside_window')
    except (ValueError, TypeError):
        codes.append('date_missing_or_invalid')
    status = normalized(fields.get('payment_status')).rstrip('.!')
    if status not in {'успешно', 'выполнен', 'выполнено', 'исполнен', 'исполнено',
                      'перевод выполнен', 'операция выполнена', 'платеж выполнен',
                      'completed', 'success', 'successful', 'succeeded'}:
        codes.append('operation_not_completed')
    identity = operation_identity(fields)
    if identity is None:
        codes.append('operation_id_missing')
    concerns = fields.get('concerns')
    if not isinstance(concerns, list) or concerns:
        codes.append('ambiguous_document')
    codes = tuple(dict.fromkeys(codes))
    return Decision(not codes, codes, identity if not codes else None, amount if not codes else None)
