from app.services.academy import level_for, next_level, strongest_and_weakest


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
