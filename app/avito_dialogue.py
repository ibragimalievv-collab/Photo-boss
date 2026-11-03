"""Deterministic recruiting dialogue; vacancy facts are supplied by the owner.

No model calls, inferred hiring decisions, or facts shared across vacancies.
"""
import re
from dataclasses import dataclass

INTRO = 'Здравствуйте! Я автоматический HR-помощник «Фотобосс».'
FIELDS = {
    'name': 'Как к вам обращаться?',
    'city': 'В каком городе вы сейчас находитесь?',
    'experience': 'Есть ли у вас опыт работы по этой вакансии?',
    'start': 'Когда вы сможете приступить к работе?',
    'accepted': 'Подходят ли вам эти условия?',
}
TERM_FIELDS = ('title', 'duties', 'pay', 'payout', 'schedule', 'location',
               'housing', 'travel', 'training', 'equipment', 'contract', 'bonuses')
LABELS = {'name': 'Имя', 'city': 'Город', 'experience': 'Опыт', 'start': 'Готовность к выходу',
          'accepted': 'Условия', 'portfolio': 'Портфолио', 'interview': 'Время собеседования'}


def normal(value):
    return re.sub(r'\s+', ' ', str(value or '')).lower().replace('ё', 'е').strip()


def message_text(message):
    content = message.get('content') or {}
    return str(content.get('text') or '') if isinstance(content, dict) else ''


def ordered(history):
    def stamp(message):
        try:
            return int(message.get('created') or 0)
        except (ValueError, TypeError):
            return 0
    # Provider pages may be newest-first; preserve order for equal timestamps.
    return sorted(history, key=stamp)


def is_question(value):
    return '?' in value or bool(re.search(
        r'\b(како[йеяг]|сколько|почему|расскажите|подскажите|объясните|где|во сколько)\b', normal(value)))


def stopped(value):
    value = normal(value).strip(' .,!')
    return value in {'нет спасибо', 'нет, спасибо', 'не актуально', 'неактуально',
                     'неинтересно', 'не интересно', 'отказываюсь', 'больше не ищу',
                     'спасибо не надо', 'спасибо, не надо', 'работа не нужна', 'не ищу работу'} or any(
        word in value for word in ('не пишите', 'не писать мне', 'не беспокойте',
                                  'не интересует работа', 'удалите мои данные'))


def extract(value, profile):
    """Only explicit statements; preserve wording instead of guessing names/cities."""
    raw = value.strip()[:1000]
    value = normal(value)
    patterns = {
        'name': r'\b(?:меня зовут|мое имя)\s+([^,.!?\n]{1,80})',
        'city': r'\b(?:живу в|нахожусь в|я из)\s+([^,.!?\n]{1,120})',
        'experience': r'\b(?:без опыта|опыта нет|опыт[а]?\s+\d[^,.!?\n]*|работал[аи]?\s+[^,.!?\n]+)',
        'start': r'\b(?:готов[аы]?|смогу|могу)\s+(?:выйти|начать|приступить)\s+([^.!?\n]+)',
    }
    for field, pattern in patterns.items():
        match = re.search(pattern, raw, re.IGNORECASE)
        if match and not re.search(r'если|например', raw[:match.start()], re.IGNORECASE):
            profile[field] = (match.group(1) if match.lastindex else match.group(0)).strip()[:300]
    links = re.findall(r'https?://[^\s<>]+', raw)
    if links and ('портфолио' in value or any(h in value for h in ('behance.', 'vk.com/', 'disk.yandex.', 'wfolio.'))):
        profile['portfolio'] = ' '.join(links)[:500]


def candidate_profile(history, value, base=None):
    profile, last_out = dict(base or {}), ''
    inputs = []
    for message in ordered(history):
        if message.get('type') != 'text':
            continue
        content = message_text(message)
        if message.get('direction') == 'out':
            last_out = content
        elif message.get('direction') == 'in' and base is None:
            inputs.append((content, last_out))
    # The worker's history already includes the current incoming batch.
    if not inputs or normal(inputs[-1][0]) != normal(value):
        inputs.append((value, last_out))
    for content, prompt in inputs:
        extract(content, profile)
        if stopped(content) or is_question(content):
            continue
        for field, question in FIELDS.items():
            if question in prompt:
                if field == 'accepted':
                    if re.fullmatch(r'(да|да[, ]+подходит|подходит|согласен|согласна|все устраивает)[.! ]*', normal(content)):
                        profile[field] = 'Да'
                    elif re.search(r'не подходит|не устраивает|^нет\b', normal(content)):
                        profile[field] = 'Нужны другие условия: ' + content[:250]
                elif content.strip() and normal(content) not in {'да', 'нет', 'спасибо', 'ок', 'хорошо'}:
                    profile[field] = content.strip()[:300]
                elif field == 'experience' and normal(content) in {'да', 'нет'}:
                    profile[field] = content.strip()
        if 'В какой день и время' in prompt and not is_question(content) and normal(content) not in {'да', 'нет', 'хорошо'}:
            profile['interview'] = content.strip()[:300]
    return profile


def validate_terms(value):
    if not isinstance(value, dict) or set(value) - set(TERM_FIELDS):
        raise ValueError('Неизвестные поля условий вакансии.')
    result = {}
    for field in TERM_FIELDS:
        content = value.get(field, '')
        if not isinstance(content, str) or len(content) > 500 or any(ord(c) < 32 and c != '\n' for c in content):
            raise ValueError('Условие должно быть текстом до 500 символов.')
        result[field] = content.strip()
    return result


@dataclass
class Turn:
    answer: str
    escalate: bool
    profile: dict
    status: str = 'ACTIVE'


def _respond(value, role, history=(), terms=None, *, active=None, profile_state=None):
    terms = validate_terms(terms or {})
    value = str(value or '').strip()
    clean = normal(value)
    profile = candidate_profile(history, value, profile_state)
    outbound = [message_text(m) for m in ordered(history)
                if m.get('type') == 'text' and m.get('direction') == 'out']
    last = normal(outbound[-1]) if outbound else ''
    prefix = '' if outbound else INTRO + ' '
    if stopped(value):
        return Turn('', False, profile, 'STOPPED')
    # A request for a person is a durable handoff, not another automated interview.
    if re.search(r'\b(свяжите|позовите|соедините|поговорить|дайте контакт|дайте номер|можно человека|нужен менеджер)', clean) and any(
            w in clean for w in ('человек', 'владел', 'менеджер', 'руковод', 'оператор')):
        return Turn(prefix + 'Передам вашу просьбу ответственному менеджеру. Автоответы в этом диалоге остановлены.', True, profile, 'HUMAN')
    if any(w in clean for w in ('я приехал', 'я приехала', 'стою возле', 'у кпп', 'возле кпп')):
        return Turn(prefix + 'Передам владельцу, что вы уже на месте, чтобы согласовать, кто вас встретит. Встреча требует подтверждения.', True, profile, 'REVIEW')

    answers, missing = [], []
    def add(field, label):
        if terms[field]:
            answers.append(terms[field])
        else:
            missing.append(label)
    all_terms = any(w in clean for w in ('все условия', 'расскажите о работе', 'расскажите подробнее', 'что за работа'))
    all_terms = all_terms or 'интересует вакансия' in clean or 'заинтересовала вакансия' in clean
    availability = bool(re.search(r'вакансия.*(?:актуаль|открыт)|еще ищете|еще актуаль|набираете', clean))
    if availability:
        if active is True:
            answers.append('Да, вакансия актуальна.')
            all_terms = not outbound
        else:
            missing.append('актуальность вакансии')
    greeting = bool(re.fullmatch(r'(здравствуйте|здравствуй|привет|добрый день|добрый вечер|фотограф|менеджер|интересует вакансия)[.! ,]*', clean))
    if all_terms or greeting and not outbound:
        title = terms['title'] or ('Работа фотографом.' if role == 'PHOTOGRAPHER' else 'Работа менеджером по записи на фотосъёмки.')
        answers.append(title)
        for field in ('duties', 'pay', 'payout', 'schedule', 'location', 'housing', 'travel', 'training'):
            if terms[field]:
                answers.append(terms[field])
    topics = [
        ('pay', 'оплата', r'оклад|зарплат|доход|процент|оплат|заработ|платите|сколько получ|\d+\s*%|фикс'),
        ('payout', 'сроки выплат', r'выплат|расчет|когда плат|как часто плат'),
        ('schedule', 'график', r'график|выходн|рабочие дни|3/1|3 на 1|во сколько|часов|ночн'),
        ('housing', 'жильё', r'жиль|жиле|жить|прожив|общежит|квартир'),
        ('travel', 'проезд', r'проезд|билет|переезд|компенсация дороги'),
        ('location', 'место работы', r'\bгде\b|адрес|\bотел|место работ|каком город'),
        ('training', 'обучение', r'обуч|наставник|науч|нович|без опыта|опыта нет'),
        ('duties', 'обязанности', r'обязанност|что делать|что надо делать|чем заним|что нужно дел'),
        ('equipment', 'оборудование', r'оборудован|фотоаппарат|камер[аыу]|своей техник'),
        ('contract', 'оформление', r'договор|оформлен|документ|налог|гражданств|виза|штраф'),
        ('bonuses', 'бонусы', r'бонус|преми'),
    ]
    for field, label, pattern in topics:
        if re.search(pattern, clean):
            # Statements about past experience are data, not a question about training.
            if field == 'training' and not is_question(value) and 'experience' in profile:
                continue
            add(field, label)
            if (field in {'housing', 'travel', 'bonuses'}
                    and re.search(r'сколько|сумм|стоимост|точн|как рассчитыв', clean)
                    and (not re.search(r'\d', terms[field]) or re.search(r'соглас|уточн|обсужд', normal(terms[field])))):
                missing.append('точные условия: ' + label)
            if field == 'schedule' and re.search(r'во сколько|часов|ночн', clean) and not re.search(r'\b\d{1,2}[:.]\d{2}|час|ноч', normal(terms[field])):
                missing.append('часы смен')
            if field == 'training' and re.search(r'сколько|долго|срок', clean) and not re.search(r'дн|недел|месяц', normal(terms[field])):
                missing.append('срок обучения')
    earnings_detail = bool(re.search(r'средн|фикс|гарант|примерно.*(месяц|день)|сколько.*(месяц|день)', clean))
    earnings_confirmed = bool(re.search(r'средн|фикс|гарант|оклад', normal(terms['pay']))) and not re.search(r'не гарант|не предусмотр|нет', normal(terms['pay']))
    if earnings_detail and not earnings_confirmed and any(
            w in clean for w in ('оклад', 'доход', 'зарплат', 'заработ', 'сколько', 'примерно')):
        answers.append('Подтверждённого среднего заработка или гарантированной суммы в настройках нет; это нужно согласовать с владельцем.')
        missing.append('средний или гарантированный доход')
    if 'опыт' in clean and is_question(value) and not re.search(r'обуч|наставник|нович|без опыта|опыта нет', clean):
        add('training', 'требования к опыту')

    scheduling = any(w in clean for w in ('собеседован', 'встретиться'))
    suggested_time = bool(re.search(r'сегодня|завтра|понедельник|вторник|среду|четверг|пятниц|суббот|воскрес|\d{1,2}[:.]\d{2}', clean))
    if scheduling and suggested_time or 'interview' in profile:
        profile['interview'] = profile.get('interview') or value[:300]
        answers.append('Передам предложенное время менеджеру для согласования. Встреча пока не подтверждена.')
        status = 'READY'
    elif scheduling or clean in {'да', 'давайте', 'хорошо'} and 'собеседован' in last:
        answers.append('В какой день и время вам удобно пройти собеседование?')
        return finish(prefix, answers, missing, profile)
    else:
        status = 'ACTIVE'
    if missing:
        # Never promise a future answer the worker cannot itself deliver.
        answers.append('Нужно уточнение менеджера: ' + ', '.join(dict.fromkeys(missing)) + '.')
    if profile.get('accepted', '').startswith('Нужны другие') or re.search(r'не устраивает|не подходит|дорого', clean):
        answers.append('Какие условия вам нужны, чтобы рассмотреть работу?')
        return finish(prefix, answers, missing, profile, 'REVIEW')
    if re.fullmatch(r'(спасибо|понятно|ок)[.! ]*', clean) and not answers:
        return Turn('Пожалуйста. Если появятся вопросы по вакансии, напишите.', False, profile)

    if status != 'READY':
        next_field = next((field for field in FIELDS if not profile.get(field)), None)
        if next_field:
            # Do not ask agreement with conditions that have not been supplied.
            if next_field == 'accepted' and not (terms['pay'] and terms['schedule'] and terms['location']):
                missing.append('основные условия вакансии')
                answers.append('Основные условия ещё не заполнены; менеджер должен уточнить их до согласования выхода.')
                status = 'REVIEW'
            else:
                answers.append(FIELDS[next_field])
        else:
            answers.append('Спасибо! Передам менеджеру вашу анкету и вопросы, чтобы согласовать следующий шаг.')
            status = 'READY'
    if is_question(value) and not answers[:-1] and not scheduling:
        missing.append(value[:300])
        answers.insert(0, 'По этому вопросу нужна проверка менеджера.')
    return finish(prefix, answers, missing, profile, status)


def respond(value, role, history=(), terms=None, *, active=None, profile_state=None):
    """Reply only to a substantive vacancy question with confirmed facts."""
    clean = normal(value)
    profile = candidate_profile(history, value, profile_state)
    if stopped(value) or re.fullmatch(r'(не подходит|не устраивает|отказ)[.! ]*', clean):
        return Turn('', False, profile, 'STOPPED')
    if 'соедините с менеджером' in clean or 'можно человека' in clean:
        return Turn('', True, profile, 'HUMAN')
    ready = bool(re.search(r'\b(?:готов[аы]?|могу|смогу)\s+(?:выйти|приступить|начать работу)\b', clean))
    conditional = bool(re.search(r'\bесли\b|не готов|не могу|не смогу', clean))
    if ready and not conditional:
        return Turn('', True, profile, 'READY')
    if re.fullmatch(r'(здравствуйте|здравствуй|привет|добрый день|добрый вечер)[.! ]*', clean):
        return Turn('Здравствуйте!' if not history else '', False, profile)
    last_out = next((normal(message_text(m)) for m in reversed(ordered(history)) if m.get('direction') == 'out'), '')
    interested = bool(re.search(r'хочу.*(?:собеседован|связаться|поговорить)|интересно.*(?:дальше|общение)|готов.*(?:общаться|собеседован)', clean))
    interested = interested or clean in {'да', 'давайте', 'согласен', 'согласна'} and 'связи' in last_out
    phone = re.search(r'(?<!\d)(?:\+?7|8)[\d ()-]{9,18}(?!\d)', value)
    asked_phone = 'номер телефона' in last_out
    if phone and asked_phone:
        profile['phone'] = phone.group(0)
        return Turn('', True, profile, 'READY')
    if interested and history and not profile.get('phone'):
        return Turn('Собеседование проводит руководитель. Оставьте, пожалуйста, номер телефона для связи.', False, profile)
    topics = r'во сколько|рабоч|ваканси|работ|оклад|зарплат|доход|процент|оплат|заработ|выплат|график|смен|жиль|жиле|прожив|проезд|обуч|опыт|наставник|обязанност|камер|фотоаппарат|ноутбук|оборудован|договор|оформлен|бонус|преми|собеседован|адрес|отел|компенсац'
    question = is_question(value) or bool(re.search(r'можно|актуаль|открыта|расскажите|подскажите', clean))
    if not question or not re.search(topics, clean):
        return Turn('', False, profile)
    if 'собеседован' in clean:
        return Turn('Собеседование проводит руководитель. Оставьте, пожалуйста, номер телефона для связи.', False, profile)
    facts = validate_terms(terms or {})
    patterns = {
        'pay': r'оклад|зарплат|доход|процент|оплат|заработ|платите',
        'payout': r'выплат|когда плат|как часто плат',
        'schedule': r'график|смен|рабочий день|час|во сколько|ночн',
        'housing': r'жиль|жиле|прожив|комнат',
        'travel': r'проезд|билет|компенсац.*дорог',
        'equipment': r'камер|ноутбук|фотоаппарат|техник|оборудован',
        'training': r'обуч|опыт|наставник',
        'duties': r'обязанност|чем заним|что делать',
        'location': r'адрес|где.*работ|место работ',
        'contract': r'договор|оформлен|документ',
        'bonuses': r'бонус|преми',
    }
    if re.search(r'собак|кошк|животн|ребен|питан|возраст|гражданств|штраф', clean):
        return Turn('', True, profile, 'REVIEW')
    answers, unknown = [], False
    for field, pattern in patterns.items():
        if not re.search(pattern, clean):
            continue
        fact = facts[field]
        detail = re.search(r'сколько|сумм|стоим|срок|когда|долго|как рассчитыв', clean)
        if not fact or (field in {'travel','bonuses'} and detail) or (field == 'training' and re.search(r'сколько|долго|срок', clean)):
            unknown = True
        else:
            answers.append(fact)
    if re.search(r'вакансия.*(?:открыт|актуаль)', clean):
        if active is True:
            answers.append('Вакансия актуальна.')
        else:
            unknown = True
    if unknown or not answers:
        return Turn('', True, profile, 'REVIEW')
    return Turn(' '.join(dict.fromkeys(answers))[:1000], False, profile)


def finish(prefix, answers, missing, profile, status='ACTIVE'):
    # Leave room for the next question; never crash a chat on a large facts card.
    unique = list(dict.fromkeys(answers))
    question = unique.pop() if unique and unique[-1].endswith('?') else ''
    result = prefix
    for part in unique:
        if len(result) + len(part) + len(question) + 2 <= 1000:
            result += part + ' '
    if question:
        result += question
    return Turn(result.strip(), bool(missing) or status in {'READY', 'REVIEW', 'HUMAN'}, profile,
                'REVIEW' if missing and status == 'ACTIVE' else status)


def reply(value, role, history=(), terms=None):
    turn = respond(value, role, history, terms)
    return turn.answer, turn.escalate
