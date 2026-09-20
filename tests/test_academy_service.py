from app.services.academy import (
    ACADEMY_BLOCKS,
    ACADEMY_LESSONS,
    block_lessons,
    lesson_is_unlocked,
    level_for,
    next_level,
    strongest_and_weakest,
    unlocked_block,
)
from app.services.academy_growth import personal_tip
from app.services.training import TRAINING_CATEGORIES, shot_instruction
from app.services.training_ai import validate


def test_academy_levels_progress_in_order():
    assert "Новичок" in level_for(0)
    assert "Средний" in level_for(300)
    assert "Профессионал" in level_for(1000)
    assert "Мастер" in level_for(2500)


def test_academy_next_level_reports_points_left():
    title, left = next_level(250)
    assert "Средний" in title
    assert left == 50
    title, left = next_level(2600)
    assert title is None
    assert left == 0


def test_academy_strengths_and_weaknesses_are_data_driven():
    strongest, weakest = strongest_and_weakest(
        {
            "composition": 8.0,
            "light": 5.5,
            "pose": 7.0,
            "emotion": 9.2,
            "color": 8.5,
        }
    )
    assert strongest == "эмоция"
    assert weakest == "свет"


def test_every_training_category_has_five_different_shot_instructions():
    for category in TRAINING_CATEGORIES:
        assert len(category.shot_plan) == 5
        assert len(set(category.shot_plan)) == 5
        assert all(shot_instruction(category, index) for index in range(1, 6))


def test_month_program_has_seven_blocks_and_twenty_eight_days():
    assert len(ACADEMY_BLOCKS) == 7
    assert len(ACADEMY_LESSONS) == 28
    assert [lesson.day for lesson in ACADEMY_LESSONS] == list(range(1, 29))
    assert all(len(block_lessons(block.number)) == 4 for block in ACADEMY_BLOCKS)


def test_next_block_requires_four_lessons_and_accepted_practice():
    first = {lesson.slug for lesson in block_lessons(1)}
    second_slug = block_lessons(2)[0].slug
    assert unlocked_block(first, set()) == 1
    assert not lesson_is_unlocked(second_slug, first, set())
    assert unlocked_block(first, {"woman"}) == 2
    assert lesson_is_unlocked(second_slug, first, {"woman"})


def test_personal_tip_uses_weakest_ai_criterion():
    import json
    from types import SimpleNamespace

    review = {"criteria": {"focus": 18, "light": 8, "composition": 14,
                            "pose": 16, "emotion": 15, "variety": 14}}
    tip = personal_tip([SimpleNamespace(ai_analysis=json.dumps(review))])
    assert tip["criterion"] == "light"
    assert "Свет" in tip["text"]


def test_ai_acceptance_requires_photo_boss_threshold_and_no_reshoot():
    accepted = {
        "score": 85, "decision": "ACCEPT", "critical": False,
        "duplicate_pairs": [], "reshoot_indexes": [],
        "strengths": ["Разные ракурсы"], "issues": [],
        "next_action": "Продолжить обучение",
        "criteria": {"focus": 18, "light": 13, "composition": 13,
                     "pose": 17, "emotion": 12, "variety": 12},
    }
    assert validate(accepted)["decision"] == "ACCEPT"
    rejected = accepted | {"score": 84, "criteria": accepted["criteria"] | {"variety": 11}}
    import pytest
    with pytest.raises(ValueError):
        validate(rejected)
