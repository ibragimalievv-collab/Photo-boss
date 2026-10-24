"""Strict, deterministic schedule parser. No inferred years or employee identity."""
from __future__ import annotations

import re
from collections import Counter
from datetime import date, timedelta

from .miniapp_security import AccessError

DATE = r"\d{2}\.\d{2}\.\d{4}"
RANGE = rf"({DATE})(?:\s*[-–—]\s*({DATE}))?"
ROW = re.compile(rf"^{RANGE}\s*[-–—:]\s*(менеджер(?:ом)?|фотограф(?:ом)?|выходн(?:ой|ые))\s*[.!]?$", re.IGNORECASE)
PERIOD = re.compile(rf"^период:\s*{RANGE}(?:\s+включительно)?\s*[.]?$", re.IGNORECASE)
ROLES = {"менеджер": "MANAGER", "менеджером": "MANAGER",
         "фотограф": "PHOTOGRAPHER", "фотографом": "PHOTOGRAPHER",
         "выходной": "OFF", "выходные": "OFF"}
TOTAL = re.compile(r"^(всего|менеджер|фотограф|выходные)\s*[-–—:]\s*(\d+)\s*(?:календарн\w+\s+)?(?:день|дня|дней)\s*[.]?$", re.IGNORECASE)


def day(value: str) -> date:
    try:
        d, m, y = map(int, value.split("."))
        return date(y, m, d)
    except (ValueError, TypeError) as exc:
        raise AccessError(f"Некорректная дата: {value}. Нужен формат ДД.ММ.ГГГГ.", 400) from exc


def parse_schedule(source: str) -> dict:
    if not isinstance(source, str) or not source.strip() or len(source) > 16000:
        raise AccessError("Вставьте график текстом, не более 16 000 символов.", 400)
    assignments, period, declared = {}, None, {}
    for number, raw in enumerate(source.splitlines(), 1):
        line = raw.strip()
        if not line or re.fullmatch(r"график(?:\s+[^\d:]+)?|контрольные итоги", line, re.IGNORECASE):
            continue
        match = PERIOD.fullmatch(line)
        if match:
            if period is not None:
                raise AccessError("Укажите период только один раз.", 400)
            period = (day(match[1]), day(match[2] or match[1]))
            continue
        total = TOTAL.fullmatch(line)
        if total:
            label = total[1].lower()
            key = "total" if label == "всего" else ROLES[label]
            if key in declared:
                raise AccessError("Контрольные итоги повторяются.", 400)
            declared[key] = int(total[2])
            continue
        match = ROW.fullmatch(line)
        if not match:
            raise AccessError(f"Строка {number}: нужна дата или диапазон с годом и роль «Менеджер», «Фотограф», «Выходной». Вставляйте только сам график.", 400)
        first, last = day(match[1]), day(match[2] or match[1])
        if not 0 <= (last - first).days <= 30:
            raise AccessError(f"Строка {number}: диапазон должен содержать от 1 до 31 дня.", 400)
        for offset in range((last - first).days + 1):
            current = first + timedelta(days=offset)
            if current in assignments:
                raise AccessError(f"Дата {current:%d.%m.%Y} указана несколько раз.", 400)
            assignments[current] = ROLES[match[3].lower()]
        if len(assignments) > 31:
            raise AccessError("За один раз можно применить график не более чем на 31 день.", 400)
    if not assignments:
        raise AccessError("В тексте не найден график.", 400)
    first, last = min(assignments), max(assignments)
    if (last - first).days + 1 != len(assignments) or (period and period != (first, last)):
        raise AccessError("В графике есть пропущенные дни или даты не совпадают с указанным периодом. Укажите и рабочие дни, и выходные.", 400)
    counts = {key: 0 for key in ("MANAGER", "PHOTOGRAPHER", "OFF")}
    counts.update(Counter(assignments.values()))
    counts["total"] = len(assignments)
    if any(counts[key] != value for key, value in declared.items()):
        raise AccessError("Контрольные итоги не совпадают с датами графика.", 400)
    return {"from": str(first), "to": str(last), "counts": counts,
            "days": [{"date": str(d), "role": assignments[d]} for d in sorted(assignments)]}
