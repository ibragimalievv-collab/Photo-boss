import json

from app.services.academy_curriculum import MANAGER_LESSONS
from app.services.booking_readiness import booking_readiness


def sample(scenario, score=90, respect=90):
    return {'client_type': scenario, 'evaluation': json.dumps({'score': score,
            'criteria': {key: respect if key == 'respect' else 90 for key in
                         ('approach', 'contact', 'conditions', 'objections', 'booking', 'respect')}})}


def test_readiness_needs_distinct_types_and_full_lesson_set():
    done = {lesson['slug'] for lesson in MANAGER_LESSONS}
    rows = [sample('booking-conditions'), sample('booking-time'), sample('booking-booking')]
    assert booking_readiness(done, rows)['ready']
    assert not booking_readiness(done - {'booking-contact'}, rows)['ready']
    assert not booking_readiness(done, rows[:2])['ready']
    assert not booking_readiness(done, [rows[0]] * 3)['ready']


def test_incorrect_conditions_or_disrespect_cannot_be_compensated_by_total_score():
    done = {lesson['slug'] for lesson in MANAGER_LESSONS}
    rows = [sample('booking-conditions'), sample('booking-camera'), sample('booking-handoff', respect=50)]
    assert not booking_readiness(done, rows)['ready']
    rows[-1] = sample('booking-handoff', score=79)
    assert not booking_readiness(done, rows)['ready']
    assert not booking_readiness(done, [{'evaluation': '{}', 'client_type': 'booking-booking'}])['ready']
