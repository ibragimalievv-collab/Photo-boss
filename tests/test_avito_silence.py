from app.avito_dialogue import respond

TERMS = {'pay': 'Оплата — 15% от продаж.', 'schedule': 'График 3/1.'}


def test_confirmed_question_and_mixed_message():
    for value in ('Какая оплата?', 'Спасибо! Какая оплата?'):
        turn = respond(value, 'MANAGER', terms=TERMS)
        assert '15%' in turn.answer and '?' not in turn.answer and not turn.escalate


def test_silence_and_owner_handoff():
    for value in ('Спасибо', 'Хорошо', 'Понятно', 'До свидания', 'Какая погода?'):
        turn = respond(value, 'MANAGER', terms=TERMS)
        assert turn.answer == '' and not turn.escalate
    turn = respond('Какой адрес работы?', 'MANAGER', terms=TERMS)
    assert turn.answer == '' and turn.escalate
    turn = respond('Готов выйти завтра', 'MANAGER', terms=TERMS)
    assert turn.answer == '' and turn.escalate and turn.status == 'READY'
    assert respond('Не пишите больше', 'MANAGER', terms=TERMS).status == 'STOPPED'


def test_owner_confirmed_facts_and_phone_stage():
    import json
    from pathlib import Path
    rows = json.loads(Path('app/avito_hr_terms_20261020.json').read_text())
    facts = rows[0]['terms']
    assert '9:00' in respond('Во сколько начинается рабочий день?', 'PHOTOGRAPHER', terms=facts).answer
    assert '5 000' in respond('Сколько стоит жильё?', 'PHOTOGRAPHER', terms=facts).answer
    assert 'собственные' in respond('Камеру и ноутбук выдадут?', 'PHOTOGRAPHER', terms=facts).answer
    assert respond('Здравствуйте', 'PHOTOGRAPHER', terms=facts).answer == 'Здравствуйте!'
    assert respond('Можно с собакой?', 'PHOTOGRAPHER', terms=facts).answer == ''
    history = [{'type': 'text', 'direction': 'out', 'created': 1, 'content': {'text': facts['pay']}}]
    turn = respond('Условия подходят. Как пройти собеседование?', 'PHOTOGRAPHER', history, facts)
    assert 'номер телефона' in turn.answer and 'В какой день' not in turn.answer


def test_interview_requests_use_existing_phone_and_no_24_hours():
    history = [{'type': 'text', 'direction': 'in', 'created': 1, 'content': {'text': '+79990000000'}}]
    turn = respond('Можно записаться на собеседование?', 'MANAGER', history, TERMS)
    assert turn.answer == '' and turn.escalate and turn.profile['phone'] == '+79990000000'
    turn = respond('Можно на собеседование?', 'MANAGER', terms=TERMS)
    assert 'В ближайшее время' in turn.answer and '24' not in turn.answer
