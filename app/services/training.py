from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..config import config

TRAINING_ASSET_DIR = Path(__file__).resolve().parents[1] / "assets" / "training"


@dataclass(frozen=True, slots=True)
class TrainingCategory:
    slug: str
    title: str
    directory: str

    @property
    def image_paths(self) -> tuple[Path, ...]:
        return tuple(
            TRAINING_ASSET_DIR / self.directory / f"{index:02}.jpg"
            for index in range(1, 6)
        )


TRAINING_CATEGORIES = (
    TrainingCategory("family", "👨‍👩‍👧 Семья в движении", "family"),
    TrainingCategory("woman", "👩 Девушка", "woman"),
    TrainingCategory("man", "👨 Парень", "man"),
    TrainingCategory("child", "🧒 Ребёнок", "child"),
    TrainingCategory("family_portrait", "🖼 Семейный портрет", "family-portrait"),
    TrainingCategory("couple", "💑 Парное фото", "couple"),
    TrainingCategory("adults", "👥 Взрослые", "adult-group"),
    TrainingCategory("plus_size", "➕ Полная комплекция", "plus-size"),
    TrainingCategory("slim", "➖ Худощавые", "slim"),
)

CATEGORY_BY_SLUG = {category.slug: category for category in TRAINING_CATEGORIES}


def training_day(value=None):
    timezone = ZoneInfo(config.training_timezone)
    if value is None:
        return datetime.now(timezone).date()
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(timezone).date()


def category_rows(excluded_slug=None):
    buttons = [
        (category.title, f"training:{category.slug}")
        for category in TRAINING_CATEGORIES
        if category.slug != excluded_slug
    ]
    return [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
