from app.services.academy import level_for, next_level, strongest_and_weakest
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
