import math
import os
from dataclasses import dataclass
from urllib.parse import urlparse

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


@dataclass(frozen=True)
class Config:
    bot_token: str
    database_url: str
    admin_ids: tuple[int, ...]
    photo_price: float
    manager_percent: float
    photographer_percent: float
    database_connect_attempts: int
    database_connect_delay: float
    telegram_api_base: str | None

    @classmethod
    def from_env(cls):
        ids = []
        for value in os.getenv("ADMIN_TELEGRAM_IDS", "").split(","):
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
            bot_token=os.getenv("BOT_TOKEN", "").strip(),
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
            telegram_api_base=telegram_api_base,
        )

    def validate(self):
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


config = Config.from_env()
