"""Offline conversation regressions; facts are synthetic per-vacancy fixtures."""
import pytest

from app.avito_dialogue import candidate_profile, reply, respond, validate_terms

TERMS = {'title': 'Фотограф в Анапе.', 'pay': 'Оплата — 15% от продаж.',
         'payout': 'Выплаты еженедельно.', 'schedule': 'График — 3/1.',
         'location': 'Работа в Анапе.', 'training': 'Можно без опыта, обучаем с наставником.',
         'housing': 'Частичная компенсация жилья.', 'travel': 'Условия проезда согласует владелец.',
         'duties': 'Фотограф снимает гостей отеля.'}


def message(value, direction='out', created=1, mid='1'):
    return {'id': mid, 'type': 'text', 'direction': direction, 'created': created, 'content': {'text': value}}


@pytest.mark.parametrize('question,expected', [
    ('Какой график и оклад?', ('15%', '3/1')),
    ('Здравствуйте, сколько платите?', ('15%',)),
    ('Где работа и как часто выплаты?', ('Анапе', 'еженедельно')),
    ('Нужно жильё', ('компенсация жилья',)),
    ('Можно без опыта?', ('наставником',)),
    ('Какие обязанности?', ('снимает гостей',)),
])
def test_known_conditions_before_follow_up(question, expected):
    turn = respond(question, 'PHOTOGRAPHER', terms=TERMS)
    assert all(part in turn.answer for part in expected)
    assert not turn.escalate
    assert 'уточню' not in turn.answer.lower()


def test_each_vacancy_has_its_own_facts_and_missing_facts_are_not_invented():
    assert '15%' in reply('Какая оплата?', 'PHOTOGRAPHER', terms=TERMS)[0]
    other = dict(TERMS, pay='Оплата — 20% от продаж.', location='Работа в Сочи.')
    answer, _ = reply('Оплата и где работа?', 'MANAGER', terms=other)
    assert '20%' in answer and 'Сочи' in answer and '15%' not in answer
    answer, owner = reply('Какая оплата?', 'PHOTOGRAPHER', terms={})
    assert owner and '15%' not in answer and '5000' not in answer and 'менеджера' in answer


def test_first_response_explains_conditions_and_asks_name():
    answer, owner = reply('Здравствуйте', 'PHOTOGRAPHER', terms=TERMS)
    assert not owner and all(s in answer for s in ('автоматический', '15%', '3/1', 'Как к вам обращаться?'))


def test_candidate_goes_through_complete_profile_without_repeating_questions():
    history = []
    candidate_inputs = ['Здравствуйте', 'Алексей', 'Краснодар', 'Нет', 'С понедельника', 'Да']
    expected = ['Как к вам обращаться?', 'В каком городе', 'Есть ли у вас опыт',
                'Когда вы сможете', 'Подходят ли вам', 'Передам менеджеру']
    for index, (value, part) in enumerate(zip(candidate_inputs, expected)):
        history.append(message(value, 'in', index*2, str(index*2)))
        turn = respond(value, 'PHOTOGRAPHER', history, TERMS)
        assert part in turn.answer, (value, turn)
        history.append(message(turn.answer, 'out', index*2+1, str(index*2+1)))
    assert turn.status == 'READY' and turn.escalate
    assert turn.profile == {'name': 'Алексей', 'city': 'Краснодар', 'experience': 'Нет',
                            'start': 'С понедельника', 'accepted': 'Да'}


def test_question_does_not_become_candidate_name_and_replies_to_question():
    history = [message('Как к вам обращаться?')]
    turn = respond('Какая зарплата?', 'PHOTOGRAPHER', history, TERMS)
    assert 'name' not in turn.profile and '15%' in turn.answer
    assert 'Как к вам обращаться?' in turn.answer


def test_explicit_information_is_saved_without_sensitive_inferences():
    profile = candidate_profile([], 'Меня зовут Алина, живу в Краснодаре. Опыта нет. Могу выйти завтра.')
    assert profile['name'] == 'Алина' and profile['city'] == 'Краснодаре'
    assert profile['start'] == 'завтра' and 'нет' in profile['experience'].lower()
    assert 'age' not in profile and 'citizenship' not in profile


def test_profile_statements_survive_a_question_in_the_same_message():
    turn = respond('Меня зовут Алина, живу в Краснодаре. Какой график?', 'PHOTOGRAPHER', terms=TERMS)
    assert turn.profile['name'] == 'Алина' and turn.profile['city'] == 'Краснодаре'
    assert '3/1' in turn.answer and 'Есть ли у вас опыт' in turn.answer


def test_active_vacancy_answers_availability_without_guessing_closed_status():
    turn = respond('Вакансия актуальна?', 'PHOTOGRAPHER', terms=TERMS, active=True)
    assert 'Да, вакансия актуальна' in turn.answer and not turn.escalate
    turn = respond('Вакансия актуальна?', 'PHOTOGRAPHER', terms=TERMS, active=False)
    assert 'Да, вакансия актуальна' not in turn.answer and turn.escalate


def test_portfolio_is_saved_as_text_without_fetching_external_link():
    turn = respond('Портфолио https://portfolio.example/album', 'PHOTOGRAPHER', terms=TERMS)
    assert turn.profile['portfolio'] == 'https://portfolio.example/album'


def test_manager_request_and_refusal():
    assert respond('Соедините с менеджером', 'MANAGER', terms=TERMS).status == 'HUMAN'
    assert respond('Не пишите мне больше', 'MANAGER', terms=TERMS).status == 'STOPPED'
    turn = respond('Мне не подходит график', 'MANAGER', terms=TERMS)
    assert turn.status == 'REVIEW' and turn.answer


def test_interview_and_arrival_do_not_promise_employment():
    answer, owner = reply('Можно пройти собеседование?', 'MANAGER', terms=TERMS)
    assert 'В какой день и время' in answer and not owner
    answer, owner = reply('Завтра в 12:00', 'MANAGER', [message(answer)], TERMS)
    assert owner and 'не подтверждена' in answer
    turn = respond('Я стою возле КПП', 'MANAGER', terms=TERMS)
    assert turn.escalate and 'кто вас встретит' in turn.answer


def test_long_facts_cannot_crash_worker_or_drop_next_question():
    answer, _ = reply('Все условия', 'MANAGER', terms={k: 'Условие '+k+' '+('а'*450) for k in TERMS})
    assert len(answer) <= 1000 and 'Как к вам обращаться?' in answer


@pytest.mark.parametrize('question', ['Сколько стоит жильё?', 'Какая сумма компенсации проезда?', 'Во сколько начинается смена?', 'Сколько длится обучение?'])
def test_unconfirmed_details_go_to_manager_without_invented_values(question):
    turn = respond(question, 'PHOTOGRAPHER', terms=TERMS)
    assert turn.escalate and 'Нужно уточнение менеджера' in turn.answer


def test_owner_confirmed_fixed_pay_is_not_contradicted():
    turn = respond('Какой фиксированный оклад?', 'PHOTOGRAPHER', terms=dict(TERMS, pay='Фиксированный оклад — 60 000 ₽.'))
    assert '60 000' in turn.answer and 'нет' not in turn.answer and not turn.escalate


@pytest.mark.parametrize('value', [{'unknown': 'x'}, {'pay': 15}, {'pay': 'x'*501}, []])
def test_invalid_terms_rejected(value):
    with pytest.raises(ValueError):
        validate_terms(value)
