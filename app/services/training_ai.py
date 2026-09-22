"""AI assessment for Academy practice using one Photo Boss rubric for everyone."""

import base64
import json
import logging

import aiohttp

from ..config import config

logger = logging.getLogger(__name__)
MAX_IMAGE_BYTES = 8 * 1024 * 1024

SCHEMA = {
    "type": "object",
    "properties": {
        "frame_feedback": {"type": "array", "minItems": 5, "maxItems": 5, "items": {
            "type": "object", "properties": {
                "index": {"type": "integer", "minimum": 1, "maximum": 5},
                "technical": {"type": "string"}, "subjective": {"type": "string"},
                "correction": {"type": "string"}, "next_task": {"type": "string"},
                "uncertain": {"type": "boolean"}},
            "required": ["index", "technical", "subjective", "correction", "next_task", "uncertain"],
            "additionalProperties": False}},
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "decision": {"type": "string", "enum": ["ACCEPT", "REVISION", "MANUAL_REVIEW"]},
        "critical": {"type": "boolean"},
        "duplicate_pairs": {"type": "array", "items": {"type": "array", "items": {"type": "integer", "minimum": 1, "maximum": 5}, "minItems": 2, "maxItems": 2}, "maxItems": 10},
        "reshoot_indexes": {"type": "array", "items": {"type": "integer", "minimum": 1, "maximum": 5}, "maxItems": 5},
        "strengths": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
        "issues": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        "next_action": {"type": "string"},
        "criteria": {"type": "object", "properties": {
            "focus": {"type": "integer", "minimum": 0, "maximum": 20},
            "light": {"type": "integer", "minimum": 0, "maximum": 15},
            "composition": {"type": "integer", "minimum": 0, "maximum": 15},
            "pose": {"type": "integer", "minimum": 0, "maximum": 20},
            "emotion": {"type": "integer", "minimum": 0, "maximum": 15},
            "variety": {"type": "integer", "minimum": 0, "maximum": 15}},
            "required": ["focus", "light", "composition", "pose", "emotion", "variety"], "additionalProperties": False},
    },
    "required": ["frame_feedback", "score", "decision", "critical", "duplicate_pairs", "reshoot_indexes", "strengths", "issues", "next_action", "criteria"],
    "additionalProperties": False,
}


def validate(result):
    if not isinstance(result, dict) or set(result) != set(SCHEMA["required"]):
        raise ValueError("Unexpected Academy analysis")
    if result['decision'] not in {'ACCEPT', 'REVISION', 'MANUAL_REVIEW'} or type(result['critical']) is not bool:
        raise ValueError('Invalid decision')
    for key, maximum in [('strengths',5),('issues',8)]:
        values=result[key]
        if not isinstance(values,list) or len(values)>maximum or any(not isinstance(v,str) or not v.strip() or len(v)>1200 for v in values):
            raise ValueError('Invalid feedback')
        if len({v.strip().casefold() for v in values}) != len(values):
            raise ValueError('Repeated feedback')
    if not isinstance(result['next_action'],str) or not 1 <= len(result['next_action'].strip()) <= 2000:
        raise ValueError('Invalid next task')
    indexes=result['reshoot_indexes']
    if not isinstance(indexes,list) or len(indexes)>5 or any(type(i) is not int or i not in range(1,6) for i in indexes) or len(set(indexes))!=len(indexes):
        raise ValueError('Invalid reshoot indexes')
    pairs=result['duplicate_pairs']
    if not isinstance(pairs,list) or len(pairs)>10 or any(not isinstance(pair,list) or len(pair)!=2 or any(type(i) is not int or i not in range(1,6) for i in pair) or pair[0]==pair[1] for pair in pairs):
        raise ValueError('Invalid duplicate pairs')
    frames=result['frame_feedback']
    keys={'index','technical','subjective','correction','next_task','uncertain'}
    if not isinstance(frames,list) or len(frames)!=5:
        raise ValueError('Missing frame feedback')
    for frame in frames:
        if not isinstance(frame,dict) or set(frame)!=keys or type(frame['index']) is not int or frame['index'] not in range(1,6) or type(frame['uncertain']) is not bool:
            raise ValueError('Invalid frame feedback')
        if any(not isinstance(frame[k],str) or not 1<=len(frame[k].strip())<=1200 for k in keys-{'index','uncertain'}):
            raise ValueError('Empty frame feedback')
    if {f['index'] for f in frames} != set(range(1,6)):
        raise ValueError('Repeated frame feedback')
    if any(f['uncertain'] for f in frames) and result['decision'] != 'MANUAL_REVIEW':
        raise ValueError('Uncertain review requires owner')
    if pairs and result['decision']=='ACCEPT':
        raise ValueError('Duplicates require review')
    score = result["score"]
    if not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= 100:
        raise ValueError("Invalid score")
    criteria = result["criteria"]
    limits = {"focus": 20, "light": 15, "composition": 15, "pose": 20, "emotion": 15, "variety": 15}
    if not isinstance(criteria, dict) or set(criteria) != set(limits):
        raise ValueError("Invalid criteria")
    if any(not isinstance(criteria[k], int) or isinstance(criteria[k], bool) or not 0 <= criteria[k] <= limit for k, limit in limits.items()):
        raise ValueError("Invalid criterion")
    if sum(criteria.values()) != score:
        raise ValueError("Score mismatch")
    if result["decision"] == "ACCEPT" and (score < 85 or result["critical"] or result["reshoot_indexes"]):
        raise ValueError("Unsafe acceptance")
    return result


def data_url(content):
    if not content.startswith(b"\xff\xd8\xff") or len(content) > MAX_IMAGE_BYTES:
        raise ValueError("Expected bounded JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(content).decode("ascii")


async def analyze_training_set(category, references, submissions):
    if not config.openai_api_key:
        return {"status": "not_configured"}
    if len(references) != 5 or len(submissions) != 5:
        return {"status": "unavailable"}
    content = [{"type": "input_text", "text": (
        f"Категория: {category.title}. Проверь 5 пар: сначала эталон, затем работа фотографа. "
        "План кадров: " + " | ".join(f"{i+1}: {x}" for i, x in enumerate(category.shot_plan))
    )}]
    for index, (reference, submission) in enumerate(zip(references, submissions), 1):
        content.extend([
            {"type": "input_text", "text": f"Кадр {index}: эталон"},
            {"type": "input_image", "detail": "high", "image_url": data_url(reference)},
            {"type": "input_text", "text": f"Кадр {index}: работа фотографа"},
            {"type": "input_image", "detail": "high", "image_url": data_url(submission)},
        ])
    payload = {
        "model": config.academy_analysis_model, "store": False, "max_output_tokens": 6000,
        "instructions": (
            "Ты строгий проверяющий Академии Photo Boss. Применяй один стандарт ко всем. "
            "Оцени наблюдаемые свойства, не личность и не привлекательность человека. "
            "Телосложение учитывай только для безопасного и выигрышного ракурса, без унизительных формулировок. "
            "Критерии: фокус/техника 20, свет 15, композиция 15, поза 20, эмоция 15, разнообразие 15. "
            "Два кадра считаются дублями, если люди, поза, план и угол практически одинаковы; небольшая смена мимики или руки не создаёт новый ракурс. "
            "Каждый из пяти кадров обязан иметь отличающийся ракурс, высоту камеры, план, действие или композицию согласно плану. "
            "ACCEPT только при 85+ баллах, без критических ошибок и без пересъёмки. 70–84 или отдельные слабые/дублирующиеся кадры — REVISION. "
            "Ниже 70, нерезкие лица, сильный пересвет кожи, опасная поза ребёнка или невозможность уверенно оценить — REVISION либо MANUAL_REVIEW. "
            "Для каждого кадра frame_feedback: technical — наблюдаемый технический брак либо его отсутствие; subjective — субъективная оценка; correction — конкретное исправление; next_task — следующее упражнение. Привязывай ошибку к номеру реального кадра. Не повторяй общий совет пять раз. При сомнении uncertain=true и решение MANUAL_REVIEW. Не выдумывай загруженность и особенности локаций, EXIF или настройки камеры. "
            "Не выдумывай детали вне изображения. Пиши замечания и следующее действие кратко по-русски."
        ),
        "input": [{"role": "user", "content": content}],
        "text": {"format": {"type": "json_schema", "name": "academy_review", "strict": True, "schema": SCHEMA}},
    }
    try:
        async with (
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=75)) as client,
            client.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {config.openai_api_key}"},
                json=payload,
            ) as response,
        ):
            if response.status != 200:
                logger.warning("Academy analysis HTTP status %s", response.status)
                return {"status": "unavailable"}
            result = await response.json()
        if not isinstance(result, dict) or result.get("status") != "completed":
            return {"status": "unavailable"}
        text = "".join(part["text"] for item in result.get("output", []) if item.get("type") == "message" for part in item.get("content", []) if part.get("type") == "output_text")
        return {"status": "completed", "review": validate(json.loads(text))}
    except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        logger.warning("Academy AI analysis unavailable; owner review required")
        return {"status": "unavailable"}


def review_text(data):
    if data.get("status") != "completed":
        return "ИИ-проверка временно недоступна. Задание передано владельцу."
    review = data["review"]
    c = review["criteria"]
    rows = [f"🤖 Проверка Photo Boss: {review['score']}/100",
            f"Техника {c['focus']}/20 · свет {c['light']}/15 · композиция {c['composition']}/15",
            f"Поза {c['pose']}/20 · эмоция {c['emotion']}/15 · разнообразие {c['variety']}/15"]
    if review["strengths"]: rows.append("\nСильные стороны: " + "; ".join(review["strengths"]))
    if review["issues"]: rows.append("\nИсправить: " + "; ".join(review["issues"]))
    if review["duplicate_pairs"]: rows.append("\nПохожие ракурсы: " + ", ".join(f"{a}–{b}" for a,b in review["duplicate_pairs"]))
    rows.append("\nСледующее действие: " + review["next_action"])
    return "\n".join(rows)
