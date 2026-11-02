"""Scenario regressions: no network, candidate messages or paid AI calls."""
from app.avito_hr import reply

def history(text):
    return [{"type": "text", "direction": "out", "created": 1, "content": {"text": text}}]

def test_known_questions_and_context():
    for role in ("PHOTOGRAPHER", "MANAGER"):
        answer, owner = reply("Какой график и оклад?", role)
        assert "15%" in answer and "3/1" in answer and not owner
        assert reply("Вакансия открыта?", role)[1] is False
        answer, owner = reply("Да", role, history("Подходит такой формат оплаты?"))
        assert "собеседование" in answer and not owner
        answer, owner = reply("Завтра в 12:00", role, history(answer))
        assert owner and "не подтверждена" in answer

def test_interview_arrival_and_work_readiness():
    assert reply("Можно на собеседование записаться", "MANAGER")[1] is False
    assert reply("Сегодня получится провести собеседование?", "MANAGER")[1] is True
    answer, owner = reply("Я стою возле КПП", "MANAGER")
    assert owner and "кто вас встретит" in answer
    answer, owner = reply("Готова выйти, но опыта нет", "MANAGER")
    assert owner and "Когда" in answer and "без опыта" in answer

def test_refusal_and_unconfirmed_compensation():
    assert reply("Не актуально", "MANAGER") == ("", False)
    answer, owner = reply("Нет дорого", "MANAGER", history("Жилье"))
    assert not owner and "условия" in answer and "проезд" not in answer
    answer, _ = reply("Жилье и проезд", "MANAGER")
    assert "10 000" not in answer and "месяца работы" not in answer
