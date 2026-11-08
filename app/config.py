import math
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram.utils.token import TokenValidationError, validate_token
from dotenv import load_dotenv
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

load_dotenv()


def number(value, name, *, minimum=0, maximum=None):
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name}: требуется число") from None
    if (
        not math.isfinite(result)
        or result < minimum
        or (maximum is not None and result > maximum)
    ):
        raise ValueError(f"{name}: число вне допустимого диапазона")
    return result


def integer(value, name, *, minimum=1, maximum=None):
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name}: требуется целое число") from None
    if result < minimum or (maximum is not None and result > maximum):
        raise ValueError(f"{name}: число вне допустимого диапазона")
    return result


def standalone_flag(value):
    if value not in {'0', '1'}:
        raise ValueError('PHOTO_BOSS_STANDALONE: укажите 0 или 1')
    return value == '1'


@dataclass(frozen=True)
class Config:
    bot_token: str = field(repr=False)
    database_url: str
    admin_ids: tuple[int, ...]
    photo_price: float
    manager_percent: float
    photographer_percent: float
    database_connect_attempts: int
    database_connect_delay: float
    telegram_api_base: str | None
    training_timezone: str
    openai_api_key: str = field(default="", repr=False)
    receipt_analysis_model: str = "gpt-4.1-mini"
    academy_analysis_model: str = "gpt-4.1-mini"
    redis_url: str | None = field(default=None, repr=False)
    ai_provider: str = 'openai'
    yandex_cloud_api_key: str = field(default='', repr=False)
    yandex_cloud_folder_id: str = ''
    yandex_vision_model: str = 'qwen3.6-35b-a3b'
    yandex_vision_pipeline: str = 'direct'
    yandex_vision_live: bool = False
    yandex_text_model: str = 'aliceai-llm-flash'
    yandex_text_live: bool = False
    yandex_realtime_model: str = 'speech-realtime-deepseek-v4-flash'
    yandex_realtime_turn_mode: str = 'server_vad'
    operational_start_date: str = ''
    standalone: bool = False
    app_auth_secret: str = field(default='', repr=False)
    qr_signing_key: str = field(default='', repr=False)
    payment_mode: str = 'standard'

    @classmethod
    def from_env(cls):
        standalone = standalone_flag(os.getenv('PHOTO_BOSS_STANDALONE', '0').strip())
        auth_secret = os.getenv('APP_AUTH_SECRET', '').strip()
        ids = []
        for value in ('' if standalone else os.getenv("ADMIN_TELEGRAM_IDS", "")).split(","):
            value = value.strip()
            if not value:
                continue
            if not value.isascii() or not value.isdigit() or not 0 < int(value) < 2**52:
                raise ValueError(
                    "ADMIN_TELEGRAM_IDS: нужны положительные Telegram ID через запятую"
                )
            ids.append(int(value))
        database_url = os.getenv("DATABASE_URL", "").strip()
        if not database_url:
            port = integer(
                os.getenv("POSTGRES_PORT", "5432"), "POSTGRES_PORT", maximum=65535
            )
            database_url = URL.create(
                "postgresql+asyncpg",
                username=os.getenv("POSTGRES_USER", "postgres"),
                password=os.getenv("POSTGRES_PASSWORD", ""),
                host=os.getenv("POSTGRES_HOST", "localhost").strip(),
                port=port,
                database=os.getenv("POSTGRES_DB", "hotel_photo_bot"),
            ).render_as_string(hide_password=False)
        telegram_api_base = (
            os.getenv("TELEGRAM_API_BASE", "").strip().rstrip("/") or None
        )
        return cls(
            bot_token=auth_secret if standalone else os.getenv("BOT_TOKEN", "").strip(),
            database_url=database_url,
            admin_ids=tuple(dict.fromkeys(ids)),
            photo_price=number(
                os.getenv("PHOTO_PRICE", "400"), "PHOTO_PRICE", minimum=0.01
            ),
            manager_percent=number(
                os.getenv("MANAGER_PERCENT", "15"), "MANAGER_PERCENT", maximum=100
            ),
            photographer_percent=number(
                os.getenv("PHOTOGRAPHER_PERCENT", "10"),
                "PHOTOGRAPHER_PERCENT",
                maximum=100,
            ),
            database_connect_attempts=integer(
                os.getenv("DB_CONNECT_ATTEMPTS", "30"),
                "DB_CONNECT_ATTEMPTS",
                maximum=120,
            ),
            database_connect_delay=number(
                os.getenv("DB_CONNECT_DELAY", "2"),
                "DB_CONNECT_DELAY",
                minimum=0.1,
                maximum=60,
            ),
            telegram_api_base=None if standalone else telegram_api_base,
            training_timezone=(
                os.getenv("TRAINING_TIMEZONE", "Europe/Moscow").strip()
                or "Europe/Moscow"
            ),
            openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
            receipt_analysis_model=(
                os.getenv("RECEIPT_ANALYSIS_MODEL", "gpt-4.1-mini").strip()
                or "gpt-4.1-mini"
            ),
            academy_analysis_model=(
                os.getenv("ACADEMY_ANALYSIS_MODEL", "gpt-4.1-mini").strip()
                or "gpt-4.1-mini"
            ),
            redis_url=os.getenv("REDIS_URL", "").strip() or None,
            ai_provider=os.getenv('AI_PROVIDER', 'openai').strip(),
            yandex_cloud_api_key=os.getenv('YANDEX_CLOUD_API_KEY', '').strip(),
            yandex_cloud_folder_id=os.getenv('YANDEX_CLOUD_FOLDER_ID', '').strip(),
            yandex_vision_model=os.getenv('YANDEX_VISION_MODEL', 'qwen3.6-35b-a3b').strip(),
            yandex_vision_pipeline=os.getenv('YANDEX_VISION_PIPELINE', 'direct').strip(),
            yandex_vision_live=os.getenv('PHOTO_BOSS_YANDEX_VISION_LIVE', '0').strip() == '1',
            yandex_text_model=os.getenv('YANDEX_TEXT_MODEL', 'aliceai-llm-flash').strip(),
            yandex_text_live=os.getenv('PHOTO_BOSS_YANDEX_TEXT_LIVE', '0').strip() == '1',
            yandex_realtime_model=os.getenv('YANDEX_REALTIME_MODEL', 'speech-realtime-deepseek-v4-flash').strip(),
            yandex_realtime_turn_mode=os.getenv('YANDEX_REALTIME_TURN_MODE', 'server_vad').strip(),
            operational_start_date=os.getenv('PHOTO_BOSS_OPERATIONAL_START_DATE', '').strip(),
            standalone=standalone,
            app_auth_secret=auth_secret,
            qr_signing_key=os.getenv('QR_SIGNING_KEY', '').strip(),
            payment_mode=os.getenv('PHOTO_BOSS_PAYMENT_MODE', 'standard'),
        )

    def validate(self):
        if self.payment_mode not in {'standard', 'receipts'}:
            raise ValueError('PHOTO_BOSS_PAYMENT_MODE: укажите standard или receipts')
        if self.payment_mode == 'receipts' and not self.standalone:
            raise ValueError('PHOTO_BOSS_PAYMENT_MODE=receipts: требуется самостоятельное приложение')
        if type(self.standalone) is not bool:
            raise ValueError('PHOTO_BOSS_STANDALONE: укажите 0 или 1')
        if self.standalone:
            if (not isinstance(self.app_auth_secret, str)
                    or not 32 <= len(self.app_auth_secret.encode()) <= 512
                    or re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]+', self.app_auth_secret)
                    or self.bot_token != self.app_auth_secret):
                raise ValueError('APP_AUTH_SECRET: нужен отдельный секрет приложения от 32 байт, не токен Telegram')
            if (not isinstance(self.qr_signing_key, str)
                    or not re.fullmatch(r'[a-fA-F0-9]{64}', self.qr_signing_key)
                    or len(set(self.qr_signing_key.lower())) == 1):
                raise ValueError('QR_SIGNING_KEY: нужен сохранённый ключ QR из 64 шестнадцатеричных символов')
            if self.telegram_api_base:
                raise ValueError('TELEGRAM_API_BASE: в самостоятельном приложении Telegram отключён')
        else:
            try:
                validate_token(self.bot_token)
            except TokenValidationError:
                raise ValueError(
                    "BOT_TOKEN: укажите действующий токен отдельной строкой в .env"
                ) from None
            if not self.admin_ids:
                raise ValueError("ADMIN_TELEGRAM_IDS: укажите Telegram ID владельца")
        try:
            url = make_url(self.database_url)
        except ArgumentError:
            raise ValueError("DATABASE_URL: некорректный адрес базы данных") from None
        if url.drivername != "postgresql+asyncpg":
            raise ValueError(
                "Для запуска нужен DATABASE_URL с драйвером postgresql+asyncpg"
            )
        if not url.database or not url.username or not url.password:
            raise ValueError("Укажите имя базы, пользователя и пароль PostgreSQL")
        if self.telegram_api_base:
            parsed = urlparse(self.telegram_api_base)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.username
                or parsed.password
            ):
                raise ValueError(
                    "TELEGRAM_API_BASE: укажите безопасный HTTP(S)-адрес без логина и пароля"
                )
        try:
            ZoneInfo(self.training_timezone)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError(
                "TRAINING_TIMEZONE: укажите часовой пояс в формате Europe/Moscow"
            ) from None
        from .services.ai_provider import responses_profile
        if self.yandex_vision_pipeline not in {'direct', 'observations'}:
            raise ValueError('YANDEX_VISION_PIPELINE: неизвестный способ анализа')
        responses_profile(self)
        from .operational_start import parse_start_date
        parse_start_date(self.operational_start_date)


config = Config.from_env()
