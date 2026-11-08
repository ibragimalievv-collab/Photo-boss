"""Server-owned payment mode; staff login and gallery QR codes are unrelated."""
import os

from .miniapp_security import AccessError


def receipts_only():
    # Invalid explicit modes must never fall back to a chargeable payment path.
    return os.getenv('PHOTO_BOSS_PAYMENT_MODE', 'standard') != 'standard'


def require_payment_qr():
    if receipts_only():
        raise AccessError('Оплата принимается по чеку. Поступление подтверждает владелец.', 409)
