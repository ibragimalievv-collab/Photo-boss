from dataclasses import dataclass
from pathlib import Path

TRAINING_ASSET_DIR = Path(__file__).resolve().parents[1] / "assets" / "training"


@dataclass(frozen=True, slots=True)
class TrainingCategory:
    slug: str
    title: str
    filename: str

    @property
    def image_path(self) -> Path:
        return TRAINING_ASSET_DIR / self.filename


TRAINING_CATEGORIES = (
    TrainingCategory("family", "👨‍👩‍👧 Семья в движении", "family-lifestyle.jpg"),
    TrainingCategory("woman", "👩 Девушка", "woman.jpg"),
    TrainingCategory("man", "👨 Парень", "man.jpg"),
    TrainingCategory("child", "🧒 Ребёнок", "child.jpg"),
    TrainingCategory("family_portrait", "🖼 Семейный портрет", "family-portrait.jpg"),
    TrainingCategory("couple", "💑 Парное фото", "couple.jpg"),
    TrainingCategory("adults", "👥 Взрослые", "adult-group.jpg"),
    TrainingCategory("plus_size", "➕ Полная комплекция", "plus-size.jpg"),
    TrainingCategory("slim", "➖ Худощавые", "slim.jpg"),
)

CATEGORY_BY_SLUG = {category.slug: category for category in TRAINING_CATEGORIES}


def category_rows():
    buttons = [
        (category.title, f"training:{category.slug}")
        for category in TRAINING_CATEGORIES
    ]
    return [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
