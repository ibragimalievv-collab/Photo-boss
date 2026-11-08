"""Short editorial notes checked against primary educational documentation.

These are references for the mentor, not imported courses or a claim that the
underlying model has been retrained. Source articles remain with their authors.
"""

import re

SOURCES = (
    {'id': 'source:nikon-exposure', 'label': 'Nikon — основы экспозиции',
     'url': 'https://www.nikonusa.com/learn-and-explore/c/tips-and-techniques/a-basic-look-at-the-basics-of-exposure',
     'track': 'photographer', 'tags': ('выдерж', 'диафраг', 'iso', 'экспоз', 'нерез', 'движ'),
     'note': 'Диафрагма и выдержка определяют поступление света. ISO участвует в выборе яркости '
             'и качества изображения. Выдержку выбирают с учётом движения; диафрагму — с учётом '
             'желаемой глубины резкости. Стартовые числа проверяют тестовым кадром, а не применяют ко всем съёмкам.'},
    {'id': 'source:canon-depth', 'label': 'Canon — глубина резкости',
     'url': 'https://files.canon-europe.com/files/webcontent/rf-lens-world/knowledge/depth-of-field/index.html',
     'track': 'photographer', 'tags': ('диафраг', 'глубин', 'резк', 'групп', 'объектив', 'дистанц'),
     'note': 'При прочих равных открытая диафрагма уменьшает глубину резкости. На результат также '
             'влияют дистанция, фокусное расстояние и точка фокусировки. Для группы проверяют все лица '
             'на увеличении; одна удачная точка автофокуса не доказывает резкость остальных участников.'},
    {'id': 'source:canon-flash', 'label': 'Canon — высокоскоростная синхронизация вспышки',
     'url': 'https://cam.start.canon/en/A006/manual/html/UG-02_Advanced_Flash_0050.html',
     'track': 'photographer', 'tags': ('вспыш', 'hss', 'синхр', 'полден', 'жёстк'),
     'note': 'Совместимая высокоскоростная синхронизация позволяет снимать со вспышкой короче обычного '
             'предела синхронизации. При сокращении выдержки эффективная мощность снижается. '
             'Поддержку и пределы проверяют по инструкции конкретных камеры и вспышки; меню EL-5 не универсально.'},
    {'id': 'source:nikon-compensation', 'label': 'Nikon — экспокоррекция и режим M',
     'url': 'https://onlinemanual.nikonimglib.com/faq/en/shooting_issues/exposure_comp_unavailable.html',
     'track': 'photographer', 'checkedAt': '2026-11-02',
     'tags': ('экспокор', 'auto iso', 'авто iso', 'автоисо', 'режим m', 'ev'),
     'note': 'Для описанных Nikon в режиме M без Auto ISO экспокоррекция изменяет показание '
             'экспонометра, но не выбранные выдержку и диафрагму. В M с Auto ISO она может '
             'действовать через ISO. Перед советом коррекции уточняют режим и Auto ISO; '
             'поведение и ограничения конкретной камеры проверяют по её руководству.'},
    {'id': 'source:canon-fill', 'label': 'Canon — заполняющая вспышка при контровом свете',
     'url': 'https://www.canon.co.nz/get-inspired/glossary/fill-flash',
     'track': 'photographer', 'checkedAt': '2026-11-02',
     'tags': ('подсвет', 'контров', 'силуэт', 'отражат', 'лицо темн', 'лицо тёмн', 'заполняющ'),
     'note': 'Заполняющая вспышка помогает осветить объект, который оказался тёмным на ярком '
             'контровом фоне. Это принцип применения дополнительного света, а не готовая '
             'мощность, дистанция или универсальное меню. Наличие совместимой вспышки и '
             'условия её использования нужно выяснить до конкретных настроек.'},
    {'id': 'source:nikon-white-balance', 'label': 'Nikon — баланс белого и направление изменения цвета',
     'url': 'https://onlinemanual.nikonimglib.com/z7_z6/en-nsg/05_basic_settings_02.html',
     'track': 'photographer', 'checkedAt': '2026-11-02',
     'tags': ('баланс бел', 'wb', 'кельвин', 'температур', 'тёпл', 'тепл', 'холодн', 'закат'),
     'note': 'При том же освещении меньшая установленная температура баланса белого даёт '
             'более холодное изображение, большая — более тёплое. Температура источника '
             'и выбранное значение WB камеры — разные вещи. Автоматические варианты могут '
             'сохранять или уменьшать тёплый оттенок, поэтому режим проверяют по модели '
             'и тестовому кадру; универсальное число Кельвинов не задают.'},
    {'id': 'source:adobe-color', 'label': 'Adobe — тон, профиль и баланс белого',
     'url': 'https://helpx.adobe.com/lightroom-classic/desktop/process-and-develop-photos/image-tone-color.html',
     'track': 'photographer', 'tags': ('цвет', 'кож', 'бел', 'raw', 'lightroom', 'обработ', 'профил'),
     'note': 'Профиль и баланс белого решают разные задачи. В Lightroom Classic баланс белого уточняют '
             'температурой и оттенком либо нейтральной областью. RAW даёт подходящий рабочий процесс '
             'для этих правок; случайная светлая область не обязательно нейтральная. Сравнивают кожу и единообразие серии.'},
    {'id': 'source:adobe-export', 'label': 'Adobe — экспорт фотографий',
     'url': 'https://helpx.adobe.com/lightroom-classic/desktop/export-photos/export-files-disk-or-cd.html',
     'track': 'photographer', 'tags': ('экспорт', 'jpeg', 'печать', 'размер', 'srgb', 'выдач', 'метадан'),
     'note': 'В экспорте отдельно задают формат, качество сжатия, цветовое пространство, размер, '
             'выходную резкость и метаданные. Для экранной выдачи обычно выбирают совместимый JPEG/sRGB; '
             'требования печати уточняют у лаборатории. Сохранение оригинала и экспорт готовой копии — разные операции.'},
    {'id': 'source:hubspot-objections', 'label': 'HubSpot — работа с сомнениями клиента',
     'url': 'https://blog.hubspot.com/sales/handling-common-sales-objections',
     'track': 'booking', 'tags': ('возраж', 'сомнен', 'клиент', 'гост', 'вопрос', 'разговор', 'запис'),
     'note': 'Перед ответом выясняют причину сомнения: выслушивают, признают сомнение, задают '
             'уточняющий вопрос и предлагают релевантный ответ. В Photo Boss это адаптируется к приглашению '
             'на фотосессию, а не к продаже готовых кадров. Окончательный отказ уважают; условия и согласие '
             'на контакт обязательны. Успех оценивают по действиям менеджера, не по обещанию записать каждого гостя.'},
)


def sources_for(track, question=''):
    """Rank the actual subject, without padding results with unrelated citations.

    Related lighting/color topics get coverage rather than letting incidental
    words such as 'unknown lens' fill all three reference slots. The notes remain
    bounded and editorial; selection is not a claim of live browsing by the AI.
    """
    text = ' '.join(question.lower().replace('ё', 'е').split())
    score = {}
    for source in SOURCES:
        if source['track'] != track:
            continue
        score[source['id']] = sum(tag.replace('ё', 'е') in text for tag in source['tags'])
    if track == 'photographer':
        backlit = any(term in text for term in ('контров', 'силуэт', 'солнце за', 'солнце сзади',
                                               'ярком фоне', 'яркий фон'))
        dark_face = bool(re.search(r'лиц[аоеу]?\b.{0,35}\bтемн|темн\w*\s+лиц', text))
        warm = any(term in text for term in ('баланс бел', 'wb', 'кельвин', 'температур',
                                            'тепл', 'холодн', 'закат'))
        if backlit or dark_face:
            score['source:canon-fill'] += 8
            score['source:nikon-compensation'] += 7
        if warm:
            score['source:nikon-white-balance'] += 8
        if any(term in text for term in ('lightroom', 'обработ', 'профил', 'raw')):
            score['source:adobe-color'] += 8
        if any(term in text for term in ('hss', 'высокоскорост', 'синхр')):
            score['source:canon-flash'] += 10
    matches = [source for source in SOURCES if score.get(source['id'], 0) > 0]
    matches.sort(key=lambda source: -score[source['id']])
    return [{key: source[key] for key in ('id', 'label', 'url', 'note')}
            | {'checkedAt': source.get('checkedAt', '2026-10-18')} for source in matches[:3]]
