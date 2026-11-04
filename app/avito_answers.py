"""Compose bounded, contextual answers exclusively from confirmed vacancy facts.

This is a rule-based parser, not a language model. Unsupported details are
routed privately to the owner, including when the rest of a question is known.
"""
import re

from .avito_dialogue import message_text, normal, ordered

TOPICS = {
    'pay': r'оклад|зарплат|доход|процент|оплат|заработ|платите|сколько.*получ|сдельн|фиксирован|минимал.*выплат',
    'payout': r'выплат|когда плат|как часто плат|раз в месяц|каждую неделю|по четверг|перв.*(?:деньг|зарплат)',
    'schedule': r'график|смен|рабочий день|час|во сколько|ноч|выходн|неполн|подработ|совмещ|после 20|задерж|переработ|обед',
    'housing': r'жиль|жиле|прожив|комнат|сосед|засел|общежит',
    'travel': r'проезд|билет|дорог|компенсац',
    'equipment': r'камер|ноутбук|фотоаппарат|техник|оборудован|телефон|макбук|macbook|lightroom|объектив|программ',
    'training': r'обуч|опыт|наставник|стажир',
    'duties': r'обязанност|чем заним|что делать|продавать фото|продаж.*заним|напоминан|запис.*гост',
    'location': r'адрес|где.*работ|место работ|витязев|анап|выбрать.*отел|перевод',
    'contract': r'договор|оформлен|документ|самозанят|трудоустр',
    'bonuses': r'бонус|преми',
    'meals': r'питан|кормят|еда|покушать',
    'start': r'когда.*(?:начинать|приступать|нужно выйти|можно выйти)|начал.*работ',
    'seasonality': r'сезон|круглогод|круглый год|минимальн.*срок|срок.*работ',
    'workplace': r'ходить|на ногах|сидяч|подвижн|на улице|в помещени|где.*съем|мор[ея]',
    'requirements': r'навык|требован.*менедж|коммуникаб',
    'portfolio': r'портфолио|пример.*(?:работ|сним|фото)',
}


def selected_sentences(fact, query, field):
    """Select requested details from the current DB text, including owner edits."""
    parts = re.split(r'(?<=[.!])\s+', fact)
    selectors = {
        'payout': [(r'перв', r'перв'), (r'когда|день|часто|месяц|недел|четверг', r'еженед|четверг|предыдущ')],
        'schedule': [(r'ноч', r'ноч'), (r'после 20|задерж|переработ', r'после 20|задерж'),
                     (r'неполн|подработ|совмещ', r'неполн'), (r'коррект|менять|обмен|выходн', r'коррект|3/1'),
                     (r'во сколько|начина|заканчив|до скольки|час', r'9:00|20:00|перерыв'),
                     (r'обед', r'перерыв'), (r'график|смен', r'3/1')],
        'pay': [(r'оклад|фиксирован|минимал|гарант', r'оклад|фиксирован|минимал|гарант'),
                (r'процент|личн|команд|сдельн|рассчит|счита', r'15%')],
        'housing': [(r'сколько|стоим|цен', r'5 000|сто'), (r'человек|комнат|сосед', r'комнат'),
                    (r'далеко|пешком|идти', r'пешком')],
        'equipment': [(r'lightroom|программ', r'lightroom|программ'), (r'макбук|macbook', r'macbook|ноутбук')],
        'training': [(r'оплач|платн|заработ|начисл', r'оплач|начисл|вознагражден'),
                     (r'без опыта|опыт', r'без опыта'), (r'как|где|наставник', r'академ|помога|на месте')],
    }
    matches = []
    for trigger, keep in selectors.get(field, []):
        if re.search(trigger, query):
            matches.extend(p for p in parts if re.search(keep, normal(p)))
    # Never invent a short answer when custom text cannot be split confidently.
    return ' '.join(dict.fromkeys(matches)) if matches else fact


def compose(value, role, history, facts, profile, active, turn):
    query = normal(value)
    fields = [f for f, pattern in TOPICS.items() if re.search(pattern, query)]
    last = next((normal(message_text(m)) for m in reversed(ordered(history)) if m.get('direction') == 'out'), '')
    elliptical = bool(re.fullmatch(r'(а |и |тогда )?(сколько|когда|как часто|почему|как это|это обязательно|а можно без него)[?!. ]*', query))
    if not fields and elliptical:
        context = [f for f, pattern in TOPICS.items() if re.search(pattern, last)]
        if len(context) == 1:
            fields = context
        elif context:
            return turn('', True, profile, 'REVIEW')
    substantive = '?' in value or bool(re.search(
        r'како|сколько|когда|почему|расскаж|подскаж|объясн|где|что делать|чем заним|можно|нуж|надо|актуаль|выдают|платите|кормят|предоставля|интересу|есть', query))
    if not substantive:
        return turn('', False, profile)
    vacancy_question = bool(re.search(r'ваканси|что за работа|условия работ', query))
    if vacancy_question and not fields:
        if re.search(r'актуаль|открыт', query):
            return turn('Вакансия актуальна.' if active is True else '', active is not True, profile)
        fields = ['duties', 'schedule', 'pay']
    # Unknown conditions remain private; no assertions about pets, deposits,
    # returns, minimum employment duration or specific equipment suitability.
    unknown = bool(re.search(r'собак|кошк|животн|ребен|штраф|возраст|гражданств|залог|коммуналь|возврат|отмен.*заказ|испытател|паспорт|форма|одежд|увольн|полом|оплат.*переработ', query))
    if unknown and not fields:
        return turn('', True, profile, 'REVIEW')
    answers = []
    for field in fields:
        fact = facts.get(field, '')
        detail = re.search(r'сколько|сумм|стоим|срок|когда|долго|формул|рассчит', query)
        unsupported = (field in {'travel', 'bonuses'} and bool(detail))
        unsupported |= field == 'training' and bool(re.search(r'сколько.*(?:дн|обуч)|долго|длится|срок|бесплатн|платить за обуч|\bплатн', query))
        unsupported |= field == 'housing' and bool(re.search(r'коммуналь|залог|неполн.*месяц|сво[еем].*жиль|одному|кошка|собак', query))
        unsupported |= field == 'equipment' and role == 'PHOTOGRAPHER' and bool(re.search(r'подойдет.*(?:камера|canon|nikon|sony)|любая камера|како.*объектив|программ', query))
        unsupported |= field == 'seasonality' and bool(re.search(r'минимальн.*срок|на месяц|на неделю', query))
        if not fact or unsupported:
            unknown = True
            continue
        # A broad income question must never turn into a promised earning range.
        answers.append(selected_sentences(fact, query, field))
    if not answers and not fields and not unknown:
        return turn('', False, profile)
    answer = ' '.join(dict.fromkeys(answers))
    # Do not truncate a condition mid-sentence or silently drop a requested topic.
    if len(answer) > 1000:
        return turn('', True, profile, 'REVIEW')
    return turn(answer, unknown, profile, 'REVIEW' if unknown else 'ACTIVE')
