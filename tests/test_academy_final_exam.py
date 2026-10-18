"""A new certificate requires an independent, completed practical final exam."""
import asyncio
import hashlib
import json
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.handlers import training
from app.models import (
    AcademyCertificate,
    AcademyCoachingPlan,
    AcademyLessonProgress,
    TrainingAssignment,
    User,
)
from app.services.academy import ACADEMY_BLOCKS, ACADEMY_LESSONS
from app.services.academy_growth import (
    academy_counts,
    ensure_certificate,
    final_exam_unlocked,
)
from app.services.training import (
    CATEGORY_BY_SLUG,
    FINAL_EXAM_REQUIREMENTS,
    FINAL_EXAM_SLUG,
)


async def fixture(*, missing=None):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(User(id=1, tg_id=1001, name="Photographer"))
        session.add_all(
            AcademyLessonProgress(user_id=1, topic_slug=lesson.slug)
            for lesson in ACADEMY_LESSONS
            if not (missing == "lesson" and lesson == ACADEMY_LESSONS[-1])
        )
        for index, block in enumerate(ACADEMY_BLOCKS):
            if missing == "practice" and block == ACADEMY_BLOCKS[-1]:
                continue
            session.add(TrainingAssignment(
                user_id=1, assigned_date=date(2026, 9, 1) + timedelta(days=index),
                category_slug=block.practice_categories[0], status="COMPLETED", ai_score=100,
            ))
        await session.commit()
    return engine, factory


def test_final_exam_is_separate_with_existing_distinct_reference_assets():
    exam = CATEGORY_BY_SLUG[FINAL_EXAM_SLUG]
    assert all(FINAL_EXAM_SLUG not in block.practice_categories for block in ACADEMY_BLOCKS)
    assert len(exam.shot_plan) == len(set(exam.shot_plan)) == 5
    assert len({path.parent for path in exam.image_paths}) > 1
    assert all(path.is_file() for path in exam.image_paths)
    assert len({hashlib.sha256(path.read_bytes()).digest() for path in exam.image_paths}) == 5
    assert len(FINAL_EXAM_REQUIREMENTS) == 5


def test_previous_last_block_score_does_not_issue_a_new_final_certificate():
    async def scenario():
        engine, factory = await fixture()
        try:
            async with factory() as session:
                assert await final_exam_unlocked(session, 1)
                assert await academy_counts(session, 1) == (28, 7, None)
                assert await ensure_certificate(session, 1) is None
        finally:
            await engine.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("status,score,expected", [
    ("ACTIVE", 100, None), ("PENDING_REVIEW", 100, None),
    ("COMPLETED", None, None), ("COMPLETED", 84, None),
    ("COMPLETED", 85, 85), ("COMPLETED", 100, 100),
])
def test_certificate_requires_completed_final_exam_at_pass_threshold(status, score, expected):
    async def scenario():
        engine, factory = await fixture()
        try:
            async with factory() as session:
                session.add(TrainingAssignment(user_id=1, assigned_date=date(2026, 9, 20),
                    category_slug=FINAL_EXAM_SLUG, status=status, ai_score=score))
                await session.flush()
                certificate = await ensure_certificate(session, 1)
                assert (certificate.final_score if certificate else None) == expected
                if certificate:
                    assert (await ensure_certificate(session, 1)).id == certificate.id
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_highest_completed_final_score_ignores_unfinished_higher_attempt():
    async def scenario():
        engine, factory = await fixture()
        try:
            async with factory() as session:
                for day, status, score in ((20, "COMPLETED", 91), (21, "COMPLETED", 97), (22, "PENDING_REVIEW", 100)):
                    session.add(TrainingAssignment(user_id=1, assigned_date=date(2026, 9, day),
                        category_slug=FINAL_EXAM_SLUG, status=status, ai_score=score))
                await session.flush()
                assert await academy_counts(session, 1) == (28, 7, 97)
                assert (await ensure_certificate(session, 1)).final_score == 97
        finally:
            await engine.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("missing", ["lesson", "practice"])
def test_final_exam_score_cannot_replace_core_lesson_or_block_practice(missing):
    async def scenario():
        engine, factory = await fixture(missing=missing)
        try:
            async with factory() as session:
                session.add(TrainingAssignment(user_id=1, assigned_date=date(2026, 9, 20),
                    category_slug=FINAL_EXAM_SLUG, status="COMPLETED", ai_score=100))
                # Non-course progress must not count toward the 28 core lessons.
                session.add(AcademyLessonProgress(user_id=1, topic_slug="guide-photographer-shift"))
                await session.flush()
                assert not await final_exam_unlocked(session, 1)
                assert await ensure_certificate(session, 1) is None
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_existing_issued_certificate_is_retained_without_new_prerequisites():
    async def scenario():
        engine, factory = await fixture(missing="practice")
        try:
            async with factory() as session:
                existing = AcademyCertificate(user_id=1, certificate_no="PBIC-LEGACY",
                    verification_code="legacy-proof", final_score=92)
                session.add(existing)
                await session.flush()
                result = await ensure_certificate(session, 1)
                assert result is existing
                assert result.certificate_no == "PBIC-LEGACY" and result.final_score == 92
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_bot_blocks_forged_exam_callback_even_with_saved_personal_plan(monkeypatch):
    async def scenario():
        engine, factory = await fixture(missing="lesson")
        try:
            async with factory() as session:
                session.add(AcademyCoachingPlan(user_id=1, track="photographer",
                    fingerprint="stale-plan", data=json.dumps({"category": FINAL_EXAM_SLUG})))
                await session.commit()
            monkeypatch.setattr(training, "Session", factory)
            callback = SimpleNamespace(data=f"training:{FINAL_EXAM_SLUG}",
                from_user=SimpleNamespace(id=1001), message=SimpleNamespace(answer=AsyncMock()), answer=AsyncMock())
            await training.training_reference(callback)
            assert "28 уроков" in callback.answer.await_args.args[0]
            assert callback.answer.await_args.kwargs["show_alert"]
            async with factory() as session:
                assert not (await session.scalars(select(TrainingAssignment).where(
                    TrainingAssignment.category_slug == FINAL_EXAM_SLUG))).all()
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_bot_menu_hides_exam_until_all_core_prerequisites(monkeypatch):
    async def scenario():
        engine, factory = await fixture(missing="practice")
        try:
            monkeypatch.setattr(training, "Session", factory)
            monkeypatch.setattr(training, "latest_assignment", AsyncMock(return_value=None))
            message = SimpleNamespace(from_user=SimpleNamespace(id=1001), answer=AsyncMock())
            await training.training_menu(message)
            markup = message.answer.await_args.kwargs["reply_markup"]
            callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
            assert "training:woman" in callbacks
            assert f"training:{FINAL_EXAM_SLUG}" not in callbacks
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_eligible_photographer_can_start_fixed_exam_without_ai_personal_plan(monkeypatch):
    async def scenario():
        engine, factory = await fixture()
        try:
            monkeypatch.setattr(training, "Session", factory)
            monkeypatch.setattr(training, "send_reference_set", AsyncMock())
            callback = SimpleNamespace(data=f"training:{FINAL_EXAM_SLUG}",
                from_user=SimpleNamespace(id=1001), message=SimpleNamespace(answer=AsyncMock()), answer=AsyncMock())
            await training.training_reference(callback)
            async with factory() as session:
                assignments = (await session.scalars(select(TrainingAssignment).where(
                    TrainingAssignment.category_slug == FINAL_EXAM_SLUG))).all()
                assert len(assignments) == 1 and assignments[0].status == "ACTIVE"
            training.send_reference_set.assert_awaited_once()
        finally:
            await engine.dispose()
    asyncio.run(scenario())
