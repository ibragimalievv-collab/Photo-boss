import json
import secrets
from datetime import UTC, datetime

from sqlalchemy import func, select

from ..models import (
    AcademyCertificate,
    AcademyLessonProgress,
    TrainingAssignment,
)
from .academy import ACADEMY_BLOCKS, ACADEMY_LESSONS

FINAL_CATEGORIES = set(ACADEMY_BLOCKS[-1].practice_categories)

TIP_BY_CRITERION = {
    "focus": "Фокус и техника: проверь выдержку, точку фокуса на глазах и устойчивость камеры.",
    "light": "Свет: поверни лицо к открытому источнику на 30–45° и убери глубокие тени под глазами.",
    "composition": "Композиция: проверь края кадра, фон за головой и свободное пространство по направлению взгляда.",
    "pose": "Поза: дай человеку действие, разверни корпус и не прижимай руки к телу.",
    "emotion": "Эмоция: вместо команды «улыбнись» задай короткое действие или вопрос и снимай переходный момент.",
    "variety": "Ракурсы: каждый новый кадр меняет точку, высоту, план или действие; одной мимики недостаточно.",
}


async def academy_counts(session, user_id):
    lessons = int(await session.scalar(select(func.count(
        AcademyLessonProgress.id
    )).where(AcademyLessonProgress.user_id == user_id,
             AcademyLessonProgress.topic_slug.in_([lesson.slug for lesson in ACADEMY_LESSONS]))) or 0)
    rows = (await session.scalars(select(TrainingAssignment).where(
        TrainingAssignment.user_id == user_id,
        TrainingAssignment.status == "COMPLETED",
    ))).all()
    categories = {row.category_slug for row in rows}
    practices = sum(
        bool(set(block.practice_categories) & categories) for block in ACADEMY_BLOCKS
    )
    final_scores = [
        row.ai_score for row in rows
        if row.category_slug in FINAL_CATEGORIES and row.ai_score is not None
    ]
    return lessons, practices, max(final_scores, default=None)


async def ensure_certificate(session, user_id):
    existing = await session.scalar(select(AcademyCertificate).where(
        AcademyCertificate.user_id == user_id
    ))
    if existing is not None:
        return existing
    lessons, practices, final_score = await academy_counts(session, user_id)
    if lessons < len(ACADEMY_LESSONS) or practices < len(ACADEMY_BLOCKS):
        return None
    if final_score is None or final_score < 85:
        return None
    year = datetime.now(UTC).year
    certificate = AcademyCertificate(
        user_id=user_id,
        certificate_no=(
            f"PBIC-{year}-{user_id:06d}-{secrets.token_hex(2).upper()}"
        ),
        verification_code=secrets.token_urlsafe(24),
        final_score=final_score,
    )
    session.add(certificate)
    await session.flush()
    return certificate


def personal_tip(assignments):
    for assignment in assignments:
        if not assignment.ai_analysis:
            continue
        try:
            review = json.loads(assignment.ai_analysis)
            criteria = review.get("criteria", {})
            weakest = min(
                (key for key in TIP_BY_CRITERION if isinstance(criteria.get(key), int)),
                key=lambda key: criteria[key],
            )
            return {"criterion": weakest, "text": TIP_BY_CRITERION[weakest]}
        except (ValueError, TypeError, KeyError):
            continue
    return None
