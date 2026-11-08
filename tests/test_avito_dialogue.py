import pytest
from app.avito_dialogue import candidate_profile, validate_terms


def test_explicit_profile_only():
    profile = candidate_profile([], 'Меня зовут Алина, живу в Краснодаре. Могу выйти завтра.')
    assert profile['name'] == 'Алина' and profile['start'] == 'завтра'
    assert 'age' not in profile


@pytest.mark.parametrize('value', [{'unknown': 'x'}, {'pay': 15}, {'pay': 'x'*501}, []])
def test_invalid_terms_rejected(value):
    with pytest.raises(ValueError):
        validate_terms(value)
