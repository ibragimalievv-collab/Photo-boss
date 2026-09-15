from datetime import UTC, date, datetime

DAILY_POSE_COUNT = 5

POSE_LIBRARY = (
    "Поворот корпуса на 45°, плечи расслаблены, взгляд в камеру.",
    "Шаг вперёд в движении: вес на передней ноге, руки свободны.",
    "Поза сидя на краю кресла: ровная спина, колени под углом.",
    "Взгляд через плечо: корпус от камеры, лицо повернуть к свету.",
    "Руки в кадре: одна у талии, вторая мягко касается волос.",
    "Опора на стену одним плечом, дальнюю ногу немного согнуть.",
    "Диагональ тела: одна нога впереди, подбородок слегка опустить.",
    "Живой смех в движении: сделать два шага и посмотреть в сторону.",
    "Парная поза: плечи близко, лица на разных уровнях.",
    "Портрет у окна: лицо к свету, корпус развернуть от окна.",
)


def daily_poses(day: date | None = None) -> list[str]:
    """Return a rotating, deterministic set of daily practice poses."""
    current_day = day or datetime.now(UTC).date()
    start = current_day.toordinal() % len(POSE_LIBRARY)
    return [
        POSE_LIBRARY[(start + index) % len(POSE_LIBRARY)]
        for index in range(DAILY_POSE_COUNT)
    ]


def daily_training_text(day: date | None = None) -> str:
    poses = daily_poses(day)
    tasks = "\n".join(f"{index}. {pose}" for index, pose in enumerate(poses, 1))
    return (
        "🎓 Обучение · практика на сегодня\n\n"
        f"{tasks}\n\n"
        "Отработайте каждую позу минимум в трёх ракурсах. "
        "Разбор загруженных работ и персональные рекомендации добавляются следующим этапом."
    )
