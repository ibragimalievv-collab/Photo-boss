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
    shot_plan: tuple[str, ...]

    @property
    def image_paths(self) -> tuple[Path, ...]:
        return tuple(
            TRAINING_ASSET_DIR / self.directory / f"{index:02}.jpg"
            for index in range(1, 6)
        )


TRAINING_CATEGORIES = (
    TrainingCategory("family", "👨‍👩‍👧 Семья в движении", "family", (
        "Общий план · уровень глаз · семья идёт вместе",
        "Средний план · ракурс 3/4 · родители взаимодействуют с ребёнком",
        "Низкая точка · движение навстречу камере",
        "Высокая точка · тесный контакт и взгляд друг на друга",
        "Боковой ракурс · живая эмоция без взгляда в камеру")),
    TrainingCategory("woman", "👩 Девушка", "woman", (
        "Общий план · уровень пояса · корпус 3/4",
        "Средний план · камера немного выше глаз · действие руками",
        "Крупный портрет · уровень глаз · прямой контакт",
        "Низкая точка · шаг или поворот · вертикальный кадр",
        "Боковой ракурс · взгляд через плечо · другой фон")),
    TrainingCategory("man", "👨 Парень", "man", (
        "Общий план · низкая точка · уверенная опора",
        "Средний план · корпус 3/4 · руки заняты действием",
        "Крупный портрет · уровень глаз · спокойный взгляд",
        "Боковой ракурс · движение · горизонтальный кадр",
        "Высокая точка · сидя · другая композиция")),
    TrainingCategory("child", "🧒 Ребёнок", "child", (
        "Уровень глаз ребёнка · общий план · безопасное движение",
        "Средний план · боковой ракурс · игра",
        "Крупный портрет · уровень глаз · естественная реакция",
        "Высокая точка · ребёнок сидит безопасно",
        "Низкая точка · движение навстречу · чистый фон")),
    TrainingCategory("family_portrait", "🖼 Семейный портрет", "family-portrait", (
        "Классический общий план · уровень глаз · все смотрят в камеру",
        "Средний план · треугольная композиция · контакт",
        "Ракурс 3/4 · родители и дети на разных уровнях",
        "Высокая точка · плотная компоновка",
        "Боковой ракурс · живая сцена после классического кадра")),
    TrainingCategory("couple", "💑 Парное фото", "couple", (
        "Общий план · уровень глаз · прогулка",
        "Средний план · 3/4 · объятие без взгляда в камеру",
        "Крупный план · уровень глаз · лица и контакт",
        "Низкая точка · движение или поворот",
        "Боковой ракурс · разговор или смех · другой фон")),
    TrainingCategory("adults", "👥 Взрослые", "adult-group", (
        "Общий план · уровень глаз · классическая расстановка",
        "Средний план · 3/4 · два уровня по высоте",
        "Низкая точка · уверенная симметрия",
        "Высокая точка · плотная группа",
        "Боковой ракурс · движение и взаимодействие")),
    TrainingCategory("plus_size", "➕ Полная комплекция", "plus-size", (
        "Общий план · уровень пояса · корпус 3/4 и свободные руки",
        "Средний план · камера чуть выше глаз · мягкий разворот",
        "Крупный портрет · уровень глаз · вытянутая шея без напряжения",
        "Боковой ракурс · шаг · дистанция без широкоугольного искажения",
        "Сидя 3/4 · опора и свободная линия корпуса")),
    TrainingCategory("slim", "➖ Худощавые", "slim", (
        "Общий план · уровень пояса · мягкая асимметрия",
        "Средний план · фронтально · действие руками",
        "Крупный портрет · уровень глаз · плечи под углом",
        "Низкая точка · движение · объём через одежду и позу",
        "Сидя сбоку · линии рук и ног не сливаются")),
)

CATEGORY_BY_SLUG = {category.slug: category for category in TRAINING_CATEGORIES}


def training_day(value=None):
    timezone = ZoneInfo(config.training_timezone)
    if value is None:
        return datetime.now(timezone).date()
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(timezone).date()


def category_rows(excluded_slug=None, allowed_slugs=None):
    allowed = set(allowed_slugs) if allowed_slugs is not None else None
    buttons = [
        (category.title, f"training:{category.slug}")
        for category in TRAINING_CATEGORIES
        if category.slug != excluded_slug
        and (allowed is None or category.slug in allowed)
    ]
    return [buttons[index : index + 2] for index in range(0, len(buttons), 2)]


def shot_instruction(category: TrainingCategory, pose_index: int) -> str:
    return category.shot_plan[pose_index - 1]
