"""Validate newly entered guest phones; never rewrite historical contacts."""
import re


def guest_phone(value):
    if value is None or value == '':
        return None
    if not isinstance(value, str):
        raise ValueError('Укажите телефон гостя.')  # noqa: TRY004 -- callers treat ValueError as invalid input
    raw = value.strip()
    if not raw:
        return None
    if len(raw) > 80 or not re.fullmatch(r'\+?[0-9\s()\-]+', raw):
        raise ValueError('Укажите телефон цифрами с кодом страны.')
    digits = re.sub(r'\D', '', raw)
    if raw.startswith('+'):
        if not 7 <= len(digits) <= 15 or digits.startswith('0') or (digits.startswith('7') and len(digits) != 11):
            raise ValueError('Проверьте длину номера и код страны.')
        return '+' + digits
    if len(digits) == 10:
        return '+7' + digits
    if len(digits) == 11 and digits[0] in '78':
        return '+7' + digits[1:]
    raise ValueError('Укажите 10 цифр российского номера, +7 или международный код с +.')
