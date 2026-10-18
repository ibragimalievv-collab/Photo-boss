"""Knowledge checks must assess the lesson, not a shared block questionnaire."""

import pytest

from app.services.academy import ACADEMY_LESSONS, block_lessons, lesson_is_unlocked
from app.services.academy_curriculum import (
    BOOKING_RULES,
    MANAGER_LESSONS,
    PHOTO_QUIZZES,
    grade_quiz,
    public_quiz,
    quiz_for,
)

# These cases describe the promised learning objective and an appropriate first
# answer. A lesson with another block member's quiz must fail this check.
PHOTO_CASES = {
    'pose': ('кисти напряжены', 'коснуться кармана'),
    'light': ('выбиты в белое', 'предупреждение пересвета'),
    'color': ('жёлтая и синяя', 'баланс белого'),
    'client': ('не умеет позировать', 'простые действия'),
    'portrait-angle': ('нос кажется', 'расстояние до лица'),
    'portrait-hands': ('пальцы сильно сжаты', 'лёгкое касание'),
    'portrait-body': ('дальний заметно размыт', 'глубину резкости'),
    'portrait-character': ('спокойный портрет', 'упростить фон'),
    'couple-distance': ('второй нет', 'плоскости лиц'),
    'couple-action': ('идёт навстречу', 'следящий af'),
    'couple-levels': ('закрыто плечом', 'проверить оба лица'),
    'couple-story': ('чего не хватает истории', 'контекста места'),
    'family-triangle': ('два ряда', 'резкость каждого лица'),
    'family-contact': ('один участник стоит отдельно', 'общее действие'),
    'family-body-types': ('неудобна предложенная поза', 'уточнив комфорт'),
    'family-series': ('очищать карту', 'проверенной дополнительной копии'),
    'child-safety': ('края бассейна', 'безопасное место'),
    'child-eye-level': ('взрослым сверху', 'уровня глаз ребёнка'),
    'child-motion': ('смазан на 1/125', 'укоротить выдержку'),
    'child-emotion': ('устал повторять улыбку', 'сделать паузу'),
    'group-layout': ('два ряда', 'проверкой всех лиц'),
    'group-attention': ('не услышала инструкцию', 'одну короткую команду'),
    'location-light': ('полуденном солнце', 'открытую тень'),
    'group-variety': ('небольшой улыбкой', 'смену плана'),
    'body-respect': ('уважительно', 'удобно ли вам'),
    'body-angle': ('разным людям', 'высоту и дистанцию'),
    'selection': ('сохранить возможность', 'правки вести неразрушающе'),
    'final-standard': ('критическую ошибку', 'отсутствие критических ошибок'),
}


@pytest.mark.parametrize('slug,objective', PHOTO_CASES.items())
def test_each_photographer_lesson_checks_its_own_applied_objective(slug, objective):
    questions = quiz_for(slug)
    question = questions[0]
    prompt_fragment, answer_fragment = objective
    assert prompt_fragment in question['prompt'].lower()
    assert answer_fragment in question['options'][question['correct']].lower()
    assert len(questions) == 3
    assert {q['correct'] for q in questions} == {0, 1, 2}


def test_every_stable_lesson_slug_has_an_independent_non_repeated_quiz():
    assert set(PHOTO_CASES) == {lesson.slug for lesson in ACADEMY_LESSONS}
    assert set(PHOTO_QUIZZES) == set(PHOTO_CASES)
    quizzes = [quiz_for(lesson.slug) for lesson in ACADEMY_LESSONS]
    assert len({id(quiz) for quiz in quizzes}) == 28
    questions = [question for quiz in quizzes for question in quiz]
    assert len({q['prompt'].casefold() for q in questions}) == 84
    assert len({id(q) for q in questions}) == 84
    assert len({id(q['options']) for q in questions}) == 84
    for question in questions:
        assert len(question['options']) == len(set(question['options'])) == 3
        assert len(question['explanation'].split()) >= 12


@pytest.mark.parametrize('slug', [*PHOTO_CASES, 'booking-approach', 'booking-handoff'])
def test_quiz_grading_reports_the_failed_concept_and_keeps_existing_response_shape(slug):
    questions = quiz_for(slug)
    answers = [question['correct'] for question in questions]
    result = grade_quiz(slug, answers)
    assert set(result) == {'score', 'passed', 'feedback'}
    assert result['score'] == 100 and result['passed'] is True
    assert all(item['correct'] for item in result['feedback'])
    # A single wrong concept prevents a pass and returns that concept's own
    # explanation, instead of a generic block-level failure.
    for index, question in enumerate(questions):
        one_wrong = answers.copy()
        one_wrong[index] = (one_wrong[index] + 1) % 3
        result = grade_quiz(slug, one_wrong)
        assert result['score'] == 67 and result['passed'] is False
        assert [item['correct'] for item in result['feedback']].count(False) == 1
        assert result['feedback'][index] == {
            'correct': False, 'explanation': question['explanation'],
        }


@pytest.mark.parametrize('slug', ['pose', 'color', 'booking-booking'])
def test_public_quiz_hides_answers_and_cannot_mutate_later_questions(slug):
    public = public_quiz(slug)
    original = quiz_for(slug)[0]['options'][0]
    assert all(set(question) == {'prompt', 'options'} for question in public)
    public[0]['options'][0] = 'changed by caller'
    assert quiz_for(slug)[0]['options'][0] == original
    assert public_quiz(slug)[0]['options'][0] == original


@pytest.mark.parametrize('answers', [
    None, {}, (), [], [0, 1], [0, 1, 2, 0], [True, 1, 2],
    [0.0, 1, 2], ['0', 1, 2], [-1, 1, 2], [3, 1, 2],
])
def test_invalid_or_partial_answers_never_pass(answers):
    with pytest.raises(ValueError, match='Ответьте на все вопросы'):
        grade_quiz('pose', answers)


def test_unknown_slug_has_no_quiz_and_cannot_be_graded():
    assert quiz_for('missing-lesson') == public_quiz('missing-lesson') == []
    with pytest.raises(ValueError):
        grade_quiz('missing-lesson', [0, 1, 2])


def test_completed_lesson_slugs_keep_unlocking_the_existing_program():
    # Curriculum expansion does not renumber or replace progress identities.
    existing_completed = {lesson.slug for lesson in block_lessons(1)}
    assert existing_completed == {'pose', 'light', 'color', 'client'}
    assert lesson_is_unlocked('portrait-angle', existing_completed, {'woman'})


def test_booking_course_keeps_stable_units_and_covers_the_full_conversation():
    assert [lesson['slug'] for lesson in MANAGER_LESSONS] == [
        'booking-approach', 'booking-contact', 'booking-value', 'booking-time',
        'booking-camera', 'booking-trust', 'booking-booking', 'booking-handoff',
    ]
    assert [lesson['day'] for lesson in MANAGER_LESSONS] == list(range(1, 9))
    prompts = []
    for lesson in MANAGER_LESSONS:
        body = lesson['body']
        assert 150 <= len(body.split()) <= 250
        for component in ('Цель:', 'Пошагово.', 'Пример', 'Ошибка → исправление:', 'Практика:', 'Критерии:'):
            assert component in body
        questions = quiz_for(lesson['slug'])
        assert len(questions) == 3
        assert grade_quiz(lesson['slug'], [q['correct'] for q in questions])['passed']
        assert all(len(q['explanation'].split()) >= 12 for q in questions)
        prompts.extend(q['prompt'] for q in questions)
    assert len(set(prompts)) == 24
    lessons = {lesson['slug']: lesson['body'] for lesson in MANAGER_LESSONS}
    assert 'два реально доступных' in lessons['booking-time']
    assert 'не выдумывают доступность' in lessons['booking-time']
    assert 'согласие на рекламные сообщения автоматически' in lessons['booking-booking']
    assert 'три разных ролевых сценария' in lessons['booking-handoff']
    assert 'перенос' in lessons['booking-handoff']
    assert '400 ₽' in BOOKING_RULES
    assert 'покупать фотографии необязательно' in BOOKING_RULES


def test_booking_checks_unknown_availability_and_respects_final_refusal():
    time_questions = quiz_for('booking-time')
    availability = time_questions[1]
    assert 'не выдумывать слоты' in availability['options'][availability['correct']]
    refusal = time_questions[2]
    assert 'завершить приглашение' in refusal['options'][refusal['correct']]
    final_question = quiz_for('booking-handoff')[2]
    assert 'соблюдены отдельно' in final_question['options'][final_question['correct']]
