"""Deterministic initial knowledge check; results are provisional, not job certification."""

VERSION = 1
SKILLS = {
    'approach': ('Начало разговора', ('booking-approach',)),
    'contact': ('Контакт и вопросы', ('booking-contact',)),
    'conditions': ('Объяснение условий', ('booking-value', 'booking-trust')),
    'objections': ('Работа с возражениями', ('booking-time', 'booking-camera')),
    'booking': ('Договорённость о записи', ('booking-booking', 'booking-handoff')),
    'respect': ('Уважение к гостю', ('booking-approach', 'booking-booking')),
}


def question(key, skill, prompt, options, scores):
    return {'id': key, 'skill': skill, 'prompt': prompt, 'options': options, 'scores': scores}


# Scores never leave the server. Partial answers can be reasonable but incomplete.
QUESTIONS = (
    question('start', 'approach', 'Гость спокойно гуляет по территории. Как начать приглашение?', (
        'Представиться и спросить, удобно ли полминуты поговорить о фотосессии.',
        'Начать с описания красивых мест, а представиться после появления интереса.',
        'Сначала предложить ближайшее время съёмки, затем объяснить, кто вы.'), (100, 50, 0)),
    question('wish', 'contact', 'Семья говорит: «У нас почти нет общих фотографий». Что спросить дальше?', (
        'Какой пакет фотографий они обычно покупают?',
        'Какие общие кадры им хотелось бы получить и кто будет участвовать?',
        'Готовы ли они записаться на семейную съёмку?'), (0, 100, 50)),
    question('listen', 'contact', 'Гость хочет живые кадры детей без долгого позирования. Как ответить?', (
        'Подробно перечислить все форматы, чтобы гость сам выбрал подходящий.',
        'Предложить обычную семейную съёмку и уточнить только время.',
        'Повторить пожелание и уточнить, какие игры и спокойный формат подойдут детям.'), (50, 0, 100)),
    question('price', 'conditions', 'Гость спрашивает: «Что именно бесплатно и сколько стоят фотографии?»', (
        'Съёмка бесплатная; назову подтверждённый тариф кадра этого отеля до записи; покупать фотографии необязательно.',
        'Съёмка бесплатная; стоимость фотографий фотограф назовёт при просмотре.',
        'Назову цену выбранного кадра; остальные условия обсудим после записи.'), (100, 0, 50)),
    question('discount', 'conditions', 'Гость согласен записаться, если вы обещаете скидку. Ваш ответ?', (
        'Пообещать небольшую скидку и попросить фотографа подтвердить её позже.',
        'Объяснить цену и запросить скидку 50% на всю съёмку у владельца или администратора, не обещая её до подтверждения.',
        'Сразу сказать, что скидок не бывает, чтобы сохранить понятные условия.'), (0, 100, 50)),
    question('time', 'objections', 'Гость заинтересован, но говорит: «Сегодня нет времени». Что сделать?', (
        'Предложить ближайшую короткую съёмку, не проверяя её длительность.',
        'Сказать, что можно подойти позже, и закончить без уточнения.',
        'Признать ограничение и с разрешения гостя предложить два доступных времени.'), (0, 50, 100)),
    question('camera', 'objections', 'Гость говорит: «Я не люблю фотографироваться». Как продолжить?', (
        'Признать его ощущение, предложить спокойный формат и спросить, интересен ли он.',
        'Заверить, что опытный фотограф гарантированно сделает идеальные фотографии.',
        'Сразу предложить снимать остальных членов семьи, не уточняя пожелания гостя.'), (100, 0, 50)),
    question('confirm', 'booking', 'Гость согласился на съёмку. Что нужно согласовать перед сохранением записи?', (
        'Достаточно выбрать свободное время: остальные детали фотограф уточнит на месте.',
        'Дату, доступное время, место, участников и способ подтверждения; повторить условия.',
        'Время и контакт гостя; место встречи можно отправить позже без подтверждения.'), (0, 100, 50)),
    question('move', 'booking', 'Записанный слот стал недоступен. Как правильно перенести съёмку?', (
        'Выбрать ближайшее свободное время и отправить гостю уведомление о переносе.',
        'Сообщить об изменении, предложить позвонить позже и пока оставить старую запись.',
        'Предложить доступные варианты, получить согласие гостя, затем обновить запись и уведомить фотографа.'), (0, 50, 100)),
    question('consent', 'respect', 'Гость согласен на съёмку, но не хочет давать телефон. Ваше действие?', (
        'Уважить решение и согласовать доступный способ подтверждения без обязательной рассылки.',
        'Попросить номер у родственника, поскольку запись уже согласована.',
        'Объяснить пользу телефона и ещё раз попросить номер, не предложив другого способа.'), (100, 0, 50)),
)


def public_questions():
    return [{'id': q['id'], 'prompt': q['prompt'], 'options': list(q['options'])} for q in QUESTIONS]


def assess(answers):
    if (not isinstance(answers, list) or len(answers) != len(QUESTIONS)
            or any(type(value) is not int or not 0 <= value < 3 for value in answers)):
        raise ValueError('Ответьте на все 10 вопросов: выберите один из трёх вариантов.')
    values = {key: [] for key in SKILLS}
    for item, answer in zip(QUESTIONS, answers, strict=True):
        values[item['skill']].append(item['scores'][answer])
    skills = [{'key': key, 'label': label, 'score': round(sum(values[key]) / len(values[key])),
               'questions': len(values[key])} for key, (label, _) in SKILLS.items()]
    score = round(sum(sum(points) for points in values.values()) / len(QUESTIONS))
    # Transparent conditions and respectful conduct are essential even on a high total.
    confident = score >= 80 and min(item['score'] for item in skills) >= 50 and all(
        point >= 50 for key in ('conditions', 'respect') for point in values[key])
    level = 'confident' if confident else 'developing' if score >= 50 else 'beginner'
    labels = {'beginner': 'Начальный уровень знаний', 'developing': 'Знания в развитии',
              'confident': 'Уверенные ответы на входные вопросы'}
    weaknesses = sorted(skills, key=lambda item: item['score'])
    recommended = []
    for item in weaknesses:
        if item['score'] >= 80:
            continue
        for slug in SKILLS[item['key']][1]:
            if slug not in recommended:
                recommended.append(slug)
    return {'version': VERSION, 'score': score, 'level': level, 'levelLabel': labels[level],
            'provisional': True, 'roleplayRequired': True,
            'basis': 'Входная проверка знаний по 10 ситуациям, без оценки реального разговора.',
            'summary': 'Это предварительная оценка знаний. Подтвердите навыки в диалоге с AI-гостем; '
                       'тест не доказывает профессиональное мастерство.',
            'skills': skills, 'recommendedLessonSlugs': recommended[:4] or ['booking-booking']}
