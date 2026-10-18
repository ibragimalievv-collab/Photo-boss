from dataclasses import dataclass

from .academy_materials import lesson_text


@dataclass(frozen=True, slots=True)
class AcademyLesson:
    slug: str
    title: str
    body: str
    day: int = 1
    block: int = 1


@dataclass(frozen=True, slots=True)
class AcademyBlock:
    number: int
    title: str
    practice_categories: tuple[str, ...]
    practice_title: str


@dataclass(frozen=True, slots=True)
class ReviewTemplate:
    slug: str
    title: str
    composition: int
    light: int
    pose: int
    emotion: int
    color: int
    quality: int
    strengths: str
    issues: str
    recommendation: str


ACADEMY_BLOCKS = (
    AcademyBlock(1, "Основа сильного кадра", ("woman",), "Портрет девушки: 5 разных ракурсов"),
    AcademyBlock(2, "Индивидуальный портрет", ("man",), "Портрет мужчины: поза, свет и характер"),
    AcademyBlock(3, "Пары и взаимодействие", ("couple",), "Пара: контакт и 5 неповторяющихся планов"),
    AcademyBlock(4, "Семья", ("family", "family_portrait"), "Семья: связь, уровни и чистые лица"),
    AcademyBlock(5, "Дети и движение", ("child",), "Ребёнок: безопасная живая серия"),
    AcademyBlock(6, "Группы и сложные условия", ("adults",), "Группа взрослых: композиция и управление"),
    AcademyBlock(7, "Разные комплекции и итог", ("plus_size", "slim"), "Итоговая серия: уважительная подача человека"),
)


ACADEMY_LESSONS = tuple(
    AcademyLesson(slug, title, lesson_text(slug), day, block)
    for slug, title, day, block in (
        ('pose', '📸 Поза клиента', 1, 1),
        ('light', '💡 Свет', 2, 1),
        ('color', '🎨 Цвет', 3, 1),
        ('client', '🗣 Работа с клиентом', 4, 1),
        ('portrait-angle', '📐 Ракурс и пропорции', 5, 2),
        ('portrait-hands', '🤲 Руки и опора', 6, 2),
        ('portrait-body', '🧍 Линия корпуса', 7, 2),
        ('portrait-character', '🎭 Характер портрета', 8, 2),
        ('couple-distance', '❤️ Дистанция в паре', 9, 3),
        ('couple-action', '🚶 Действие вместо позы', 10, 3),
        ('couple-levels', '↕️ Уровни и силуэты', 11, 3),
        ('couple-story', '🎬 История пары', 12, 3),
        ('family-triangle', '🔺 Композиция семьи', 13, 4),
        ('family-contact', '🤗 Связь между людьми', 14, 4),
        ('family-body-types', '🫶 Комфорт и комплекция', 15, 4),
        ('family-series', '🖼 Полная семейная серия', 16, 4),
        ('child-safety', '🛡 Безопасность ребёнка', 17, 5),
        ('child-eye-level', '👀 Уровень глаз', 18, 5),
        ('child-motion', '🏃 Движение и фокус', 19, 5),
        ('child-emotion', '😊 Настоящая эмоция', 20, 5),
        ('group-layout', '👥 Расстановка группы', 21, 6),
        ('group-attention', '📣 Управление вниманием', 22, 6),
        ('location-light', '🌤 Локация и сложный свет', 23, 6),
        ('group-variety', '🔄 Разнообразие без дублей', 24, 6),
        ('body-respect', '🤝 Уважительная режиссура', 25, 7),
        ('body-angle', '📷 Ракурс для разных комплекций', 26, 7),
        ('selection', '✅ Отбор сильных кадров', 27, 7),
        ('final-standard', '🏆 Итоговый стандарт Photo Boss', 28, 7),
    )
)

LESSON_BY_SLUG = {lesson.slug: lesson for lesson in ACADEMY_LESSONS}
BLOCK_BY_NUMBER = {block.number: block for block in ACADEMY_BLOCKS}


def block_lessons(number: int) -> tuple[AcademyLesson, ...]:
    return tuple(lesson for lesson in ACADEMY_LESSONS if lesson.block == number)


def block_practice_done(number: int, completed_categories: set[str]) -> bool:
    block = BLOCK_BY_NUMBER[number]
    return bool(set(block.practice_categories) & completed_categories)


def unlocked_block(completed_lessons: set[str], completed_categories: set[str]) -> int:
    unlocked = 1
    for number in range(1, len(ACADEMY_BLOCKS)):
        lessons_done = all(
            lesson.slug in completed_lessons for lesson in block_lessons(number)
        )
        if not lessons_done or not block_practice_done(number, completed_categories):
            break
        unlocked = number + 1
    return unlocked


def lesson_is_unlocked(
    slug: str, completed_lessons: set[str], completed_categories: set[str]
) -> bool:
    lesson = LESSON_BY_SLUG.get(slug)
    return bool(
        lesson and lesson.block <= unlocked_block(completed_lessons, completed_categories)
    )


REVIEW_TEMPLATES = {
    "strong": ReviewTemplate(
        "strong",
        "✅ Сильный кадр",
        9, 9, 9, 9, 9, 9,
        "Композиция, эмоция и момент работают вместе.",
        "",
        "Сохрани этот приём и повтори его в похожей ситуации, не копируя позу механически.",
    ),
    "light": ReviewTemplate(
        "light",
        "💡 Лицо в тени",
        8, 5, 8, 8, 8, 7,
        "Момент и контакт с клиентом сохранены.",
        "Лицо в тени.",
        "В следующий раз поставь человека ближе к открытому свету и поверни лицо к источнику примерно на 30°.",
    ),
    "horizon": ReviewTemplate(
        "horizon",
        "📐 Заваленный горизонт",
        5, 8, 8, 8, 8, 7,
        "Эмоция и свет читаются хорошо.",
        "Заваленный горизонт.",
        "Перед спуском проверь линию моря, пола или архитектуры и выровняй кадр по самому стабильному ориентиру.",
    ),
    "pose": ReviewTemplate(
        "pose",
        "🧍 Слабая поза",
        8, 8, 5, 7, 8, 7,
        "Композиция собрана и человек отделён от фона.",
        "Поза выглядит напряжённо.",
        "Дай человеку действие: шаг, поворот корпуса или касание. Руки отведи от корпуса и сократи статичность.",
    ),
    "emotion": ReviewTemplate(
        "emotion",
        "🎭 Слабая эмоция",
        8, 8, 8, 5, 8, 7,
        "Технически кадр собран аккуратно.",
        "Эмоция клиента выглядит формально.",
        "Не проси просто улыбнуться: задай короткое действие или вопрос, который вызывает живую реакцию, и снимай между позами.",
    ),
    "angle": ReviewTemplate(
        "angle",
        "📷 Неудачный угол",
        6, 8, 7, 8, 8, 7,
        "Хорошо пойман момент.",
        "Угол съёмки искажает пропорции или ослабляет композицию.",
        "Сместись на полшага в сторону и чуть подними точку съёмки. Проверь края кадра и фон за головой.",
    ),
    "color": ReviewTemplate(
        "color",
        "🎨 Цвет",
        8, 8, 8, 8, 5, 7,
        "Композиция и эмоция работают.",
        "Тон кожи или общий цвет выбивается из стиля серии.",
        "Сначала выровняй баланс белого по коже, затем корректируй контраст и насыщенность всей серии одинаково.",
    ),
}


LEVELS = (
    (2500, "👑 Мастер Photo Boss"),
    (1000, "🥇 Профессионал"),
    (300, "🥈 Средний"),
    (0, "🥉 Новичок"),
)


def level_for(points: int) -> str:
    for threshold, title in LEVELS:
        if points >= threshold:
            return title
    return LEVELS[-1][1]


def next_level(points: int) -> tuple[str | None, int]:
    ascending = tuple(reversed(LEVELS))
    for threshold, title in ascending:
        if threshold > points:
            return title, threshold - points
    return None, 0


SKILL_LABELS = {
    "composition": "композиция",
    "light": "свет",
    "pose": "позирование",
    "emotion": "эмоция",
    "color": "цвет",
}


def strongest_and_weakest(averages: dict[str, float | None]) -> tuple[str, str]:
    available = {key: value for key, value in averages.items() if value is not None}
    if not available:
        return "пока нет оценок", "пока нет оценок"
    strongest = max(available, key=available.get)
    weakest = min(available, key=available.get)
    return SKILL_LABELS[strongest], SKILL_LABELS[weakest]
