import base64
import json
import logging
import os
import re
from datetime import UTC, datetime
from typing import Any

import aiohttp
from sqlalchemy import select

from ..models import AcademyLesson, AcademyPracticeTask

logger = logging.getLogger(__name__)

CATEGORY_LABELS = {
    "pose": "📸 Позирование",
    "light": "💡 Свет",
    "color": "🎨 Цвет",
    "client": "🗣 Работа с клиентом",
    "composition": "📐 Композиция",
    "emotion": "❤️ Эмоция",
    "family": "👨‍👩‍👧 Семьи",
    "couple": "💑 Пары",
    "children": "👶 Дети",
    "wedding": "💍 Свадьбы",
    "sea": "🌊 Море",
    "evening": "🌅 Вечерние кадры",
}

LESSONS = (
    ("pose", "Руки без напряжения", "Не прижимайте руки к корпусу. Дайте человеку простое действие: держать очки, поправить волосы, коснуться партнёра или свободно опустить кисти. Следите, чтобы пальцы не выглядели зажатыми.", 10),
    ("pose", "Куда смотреть", "Вместо команды «смотрите в камеру» чередуйте три варианта: в объектив, друг на друга и в сторону света. Для живых эмоций сначала дайте действие, а потом снимайте реакцию.", 20),
    ("pose", "Как убрать напряжение", "Попросите сделать шаг, вдохнуть, выдохнуть и слегка повернуть плечи. Движение почти всегда выглядит естественнее статичной стойки.", 30),
    ("light", "Лицо без тяжёлых теней", "Разверните лицо к самому большому источнику света. При окне поставьте человека под углом 30–45°. На улице избегайте пятнистого света через листья.", 10),
    ("light", "Правильный угол к свету", "Свет сбоку даёт объём, но следите за глазами. Если один глаз уходит в глубокую тень, поверните лицо чуть ближе к источнику.", 20),
    ("light", "Работа у окна", "Ставьте героя в 0,5–1,5 м от окна и выключайте лишний верхний свет, если он портит оттенок кожи. Экспозицию контролируйте по лицу.", 30),
    ("color", "Кожа", "Сначала выровняйте баланс белого, потом насыщенность. Кожа должна оставаться естественной: не делайте её слишком оранжевой, розовой или серой.", 10),
    ("color", "Единый стиль Photo Boss", "Сохраняйте одинаковую контрастность, температуру и насыщенность внутри одной серии. Клиент воспринимает серию как цельный продукт.", 20),
    ("client", "Как расположить человека", "Начните с простого разговора, объясните, что будете подсказывать. Хвалите конкретно: «сейчас отлично лёг свет» или «вот это движение получилось естественно».", 10),
    ("client", "Как увеличить доверие", "Покажите 1–2 удачных кадра в процессе. Это снижает тревогу клиента и делает дальнейшую съёмку свободнее.", 20),
    ("client", "Как предложить больше кадров", "Не продавайте количеством. Покажите разные истории: общий план, портрет, эмоцию, детали и динамику. Разнообразие увеличивает ценность серии.", 30),
)

TASKS = (
    ("family", "Семья с ребёнком — 5 кадров", "Сделайте 5 разных кадров семьи с ребёнком: общий, средний, эмоция ребёнка, взаимодействие родителей, динамический кадр.", "Люди близко друг к другу, лица видны, есть действие и эмоция.", "Все стоят в одну линию, руки зажаты, ребёнок не вовлечён.", "Дайте семье простое действие: пройтись, обняться, поднять ребёнка, посмотреть друг на друга.", 100),
    ("couple", "Пара — 5 живых кадров", "Сделайте 5 кадров пары с разной крупностью и минимум двумя действиями.", "Контакт между людьми, чистый фон, свет на лицах.", "Одинаковые позы и взгляд только в камеру.", "Чередуйте: шаг, объятие, поворот, разговор, взгляд друг на друга.", 100),
    ("children", "Ребёнок — эмоция и движение", "Сделайте 5 кадров ребёнка: 2 портрета, 2 динамических, 1 с родителем.", "Камера на уровне ребёнка, короткие задания, живая реакция.", "Съёмка сверху и просьба долго стоять неподвижно.", "Опуститесь ниже и превратите съёмку в игру.", 100),
    ("sea", "Серия у моря", "Сделайте 5 кадров у моря с разным планом и чистым горизонтом.", "Ровный горизонт, герой отделён от фона, лицо не провалено в тень.", "Горизонт режет голову, слишком много пустого неба, лицо темнее фона.", "Проверяйте линию горизонта и экспозицию по коже перед каждым блоком.", 120),
    ("evening", "Вечерний свет", "Сделайте 5 кадров в сложном вечернем свете: 3 портрета и 2 кадра в движении.", "Свет контролируемый, кожа читается, фон не перетягивает внимание.", "Лицо в тени, шум и случайные источники света в кадре.", "Ищите один главный источник и разворачивайте лицо к нему.", 140),
)

SYSTEM_PROMPT = """Ты — практичный наставник фотографов Photo Boss.
Оцени только сам снимок: композицию, свет, эмоцию и позирование.
Не идентифицируй людей, не оценивай личность, здоровье, возраст или происхождение.
Верни только JSON:
{
  "composition": 0-10,
  "light": 0-10,
  "emotion": 0-10,
  "pose": 0-10,
  "overall": 0-10,
  "good": ["короткий плюс"],
  "errors": [
    {"category": "composition|light|emotion|pose", "title": "короткая ошибка", "recommendation": "конкретное действие на следующей съёмке"}
  ],
  "summary": "1-2 коротких предложения"
}
Рекомендации делай конкретными и короткими."""


async def seed_academy(session):
    if await session.scalar(select(AcademyLesson.id).limit(1)) is None:
        for category, title, body, sort_order in LESSONS:
            session.add(
                AcademyLesson(
                    category=category,
                    title=title,
                    body=body,
                    sort_order=sort_order,
                )
            )
    if await session.scalar(select(AcademyPracticeTask.id).limit(1)) is None:
        for row in TASKS:
            session.add(
                AcademyPracticeTask(
                    category=row[0],
                    title=row[1],
                    description=row[2],
                    strong_example_text=row[3],
                    common_mistake_text=row[4],
                    repeat_tip=row[5],
                    points=row[6],
                )
            )
    await session.commit()


def _extract_text(payload: dict[str, Any]) -> str:
    parts = []
    for item in payload.get("output", []) or []:
        for chunk in item.get("content", []) or []:
            if chunk.get("type") == "output_text" and chunk.get("text"):
                parts.append(chunk["text"])
    return "\n".join(parts).strip()


def _clean_json(text: str) -> dict[str, Any]:
    text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    data = json.loads(text)
    for key in ("composition", "light", "emotion", "pose", "overall"):
        data[key] = max(0, min(10, round(float(data.get(key, 0)))))
    data["good"] = [str(x)[:220] for x in (data.get("good") or [])][:4]
    errors = []
    for item in (data.get("errors") or [])[:5]:
        category = str(item.get("category", "composition")).lower()
        if category not in {"composition", "light", "emotion", "pose"}:
            category = "composition"
        errors.append(
            {
                "category": category,
                "title": str(item.get("title", "Ошибка"))[:180],
                "recommendation": str(item.get("recommendation", "Измените постановку кадра."))[:700],
            }
        )
    data["errors"] = errors
    data["summary"] = str(data.get("summary", ""))[:1000]
    return data


async def analyze_photo(photo_bytes: bytes) -> dict[str, Any] | None:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None
    model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini"
    encoded = base64.b64encode(photo_bytes).decode("ascii")
    payload = {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": SYSTEM_PROMPT},
                    {"type": "input_image", "image_url": f"data:image/jpeg;base64,{encoded}"},
                ],
            }
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=60)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as http, http.post(
            "https://api.openai.com/v1/responses", headers=headers, json=payload
        ) as response:
            body = await response.text()
            if response.status >= 400:
                logger.warning("Academy AI error %s: %s", response.status, body[:300])
                return None
            return _clean_json(_extract_text(json.loads(body)))
    except (aiohttp.ClientError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("Academy AI analysis failed: %s", type(exc).__name__)
        return None


def utc_now():
    return datetime.now(UTC).replace(tzinfo=None)
