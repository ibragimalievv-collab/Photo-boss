"""Learning readiness from observable exercises, never employment permissions."""
import json

from .academy_curriculum import MANAGER_LESSONS

BOOKING_SKILL_KEYS = ('approach', 'contact', 'conditions', 'objections', 'booking', 'respect')
SCENARIO_GROUPS = (
    ('conditions', 'Объяснить условия', {'booking-conditions'}),
    ('objection', 'Разобрать сомнение гостя', {'booking-time', 'booking-camera'}),
    ('booking', 'Согласовать запись', {'booking-booking', 'booking-handoff'}),
)


def booking_readiness(completed, sessions):
    known = {lesson['slug'] for lesson in MANAGER_LESSONS}
    passed = set()
    for row in sessions:
        try:
            value = row.get('evaluation') or {}
            result = json.loads(value) if isinstance(value, str) else value
            criteria = result.get('criteria', {})
            if (type(result.get('score')) is int and 80 <= result['score'] <= 100
                    and all(type(criteria.get(key)) is int and 0 <= criteria[key] <= 100
                            for key in BOOKING_SKILL_KEYS)
                    and criteria['conditions'] >= 80 and criteria['respect'] >= 80):
                passed.add(row['client_type'])
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    groups = [{'key': key, 'title': title, 'passed': bool(types & passed)}
              for key, title, types in SCENARIO_GROUPS]
    done = set(completed) & known
    return {'ready': done == known and all(group['passed'] for group in groups),
            'lessonsDone': len(done), 'lessonsTotal': len(known),
            'scenarios': groups, 'passedScenarioTypes': sorted(passed),
            'basis': 'Учебная готовность: восемь уроков и три типа диалогов от 80/100; '
                     'условия и уважение к гостю — не ниже 80/100. '
                     'ИИ-оценка не гарантирует запись гостя и не изменяет рабочие права.'}
