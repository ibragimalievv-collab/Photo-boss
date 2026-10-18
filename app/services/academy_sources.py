"""Short editorial notes checked against primary educational documentation.

These are references for the mentor, not imported courses or a claim that the
underlying model has been retrained. Source articles remain with their authors.
"""

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
    text = question.lower()
    matches = [source for source in SOURCES if source['track'] == track]
    matches.sort(key=lambda source: -sum(tag in text for tag in source['tags']))
    return [{key: source[key] for key in ('id', 'label', 'url', 'note')}
            | {'checkedAt': '2026-10-18'} for source in matches[:3]]
