"""All labels here are synthetic test fixtures, never empirical calibration data."""

import json
import subprocess
import sys
from copy import deepcopy

import pytest

from app.services.academy_calibration import DECISIONS, calibration_report, main


def sample(sample_id='test-fixture', expert_decision='ACCEPT', expert_score=90,
           ai_decision='ACCEPT', ai_score=90):
    return {'id': sample_id,
            'expert': {'decision': expert_decision, 'score': expert_score},
            'ai': {'decision': ai_decision, 'score': ai_score}}


def test_hand_calculated_errors_and_every_confusion_matrix_axis():
    records = [
        sample('a', 'ACCEPT', 90, 'ACCEPT', 92),
        sample('b', 'REVISION', 70, 'ACCEPT', 88),
        sample('c', 'ACCEPT', 90, 'REVISION', 78),
        sample('d', 'MANUAL_REVIEW', 65, 'MANUAL_REVIEW', 60),
    ]
    original = deepcopy(records)
    report = calibration_report(records, min_samples=4)
    assert report['n'] == 4
    assert report['meanAbsoluteScoreError'] == 9.25  # (2 + 18 + 12 + 5) / 4
    assert report['decisionAgreement'] == .5
    assert report['falseAcceptCount'] == report['falseRejectCount'] == 1
    assert report['confusionMatrix'] == {
        'ACCEPT': {'ACCEPT': 1, 'REVISION': 1, 'MANUAL_REVIEW': 0},
        'REVISION': {'ACCEPT': 1, 'REVISION': 0, 'MANUAL_REVIEW': 0},
        'MANUAL_REVIEW': {'ACCEPT': 0, 'REVISION': 0, 'MANUAL_REVIEW': 1},
    }
    assert report['gates'] == {'minimumSamples': True, 'maximumFalseAccepts': False,
                               'maximumMeanAbsoluteScoreError': False}
    assert report['passed'] is False
    assert records == original


def test_all_nine_decision_pairs_are_counted_and_manual_deferral_is_not_auto_rejection():
    records = [sample(f'{expert}-{ai}', expert, 80, ai, 80)
               for expert in DECISIONS for ai in DECISIONS]
    report = calibration_report(records, min_samples=9)
    assert all(count == 1 for row in report['confusionMatrix'].values() for count in row.values())
    assert report['decisionAgreement'] == round(1 / 3, 6)
    assert report['falseAcceptCount'] == 2  # includes expert MANUAL_REVIEW
    assert report['falseRejectCount'] == 1  # AI MANUAL_REVIEW is a deferral
    assert report['passed'] is False


def test_default_minimum_sample_count_cannot_be_satisfied_by_a_tiny_perfect_dataset():
    nineteen = [sample(str(index)) for index in range(19)]
    report = calibration_report(nineteen)
    assert report['meanAbsoluteScoreError'] == 0
    assert report['decisionAgreement'] == 1
    assert report['gates']['minimumSamples'] is False
    assert report['passed'] is False
    assert calibration_report(nineteen + [sample('20')])['passed'] is True


def test_score_error_gate_includes_its_boundary_but_uses_unrounded_error():
    assert calibration_report([sample(ai_score=82)], min_samples=1)['passed'] is True
    failed = calibration_report([sample(ai_score=81.9999999)], min_samples=1)
    assert failed['meanAbsoluteScoreError'] == 8.0  # display precision does not change the gate
    assert failed['gates']['maximumMeanAbsoluteScoreError'] is False
    assert failed['passed'] is False
    assert calibration_report([sample(ai_score=81)], min_samples=1, max_mae=9)['passed'] is True


def test_false_accept_limit_is_explicit_and_independent_of_low_score_error():
    records = [sample(expert_decision='REVISION', expert_score=84, ai_score=86)]
    report = calibration_report(records, min_samples=1)
    assert report['meanAbsoluteScoreError'] == 2
    assert report['passed'] is False
    allowed = calibration_report(records, min_samples=1, max_false_accept=1)
    assert allowed['passed'] is True
    assert allowed['thresholds'] == {'minSamples': 1, 'maxFalseAccept': 1,
                                      'maxMeanAbsoluteScoreError': 8.0}


def test_empty_dataset_has_undefined_metrics_and_never_passes():
    report = calibration_report([])
    assert report['n'] == 0
    assert report['meanAbsoluteScoreError'] is report['decisionAgreement'] is None
    assert report['falseAcceptCount'] == report['falseRejectCount'] == 0
    assert report['passed'] is False
    assert all(count == 0 for row in report['confusionMatrix'].values() for count in row.values())
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize('score', [None, True, False, '85', -1, 101, float('nan'),
                                  float('inf'), -float('inf'), 10 ** 1000])
@pytest.mark.parametrize('role', ['expert', 'ai'])
def test_invalid_score_is_rejected_in_either_review(role, score):
    record = sample()
    record[role]['score'] = score
    with pytest.raises(ValueError, match=role + '.score'):
        calibration_report([record], min_samples=1)


@pytest.mark.parametrize('score', [0, 100, 37.25])
def test_valid_scores_include_both_boundaries_and_fractional_scores(score):
    report = calibration_report([sample(expert_score=score, ai_score=score)], min_samples=1)
    assert report['meanAbsoluteScoreError'] == 0


@pytest.mark.parametrize('sample_id', ['', ' ', ' x', 'x ', None, 1, True, []])
def test_invalid_sample_ids_are_rejected(sample_id):
    with pytest.raises(ValueError, match='.id'):
        calibration_report([sample(sample_id)], min_samples=1)


def test_duplicate_ids_cannot_inflate_sample_size():
    with pytest.raises(ValueError, match='duplicates'):
        calibration_report([sample('same'), sample('same')], min_samples=1)


@pytest.mark.parametrize('decision', ['accept', 'PASS', '', 1, True, None, []])
@pytest.mark.parametrize('role', ['expert', 'ai'])
def test_unknown_or_untyped_decisions_are_rejected(role, decision):
    record = sample()
    record[role]['decision'] = decision
    with pytest.raises(ValueError, match=role + '.decision'):
        calibration_report([record], min_samples=1)


@pytest.mark.parametrize('records', [None, {}, (), [None], [[]], [{}],
                                     [{'id': 'x', 'expert': None, 'ai': {}}],
                                     [{'id': 'x', 'expert': {'decision': 'ACCEPT'}, 'ai': {}}]])
def test_missing_or_malformed_record_shape_is_rejected(records):
    with pytest.raises(ValueError):
        calibration_report(records, min_samples=1)


@pytest.mark.parametrize('kwargs', [
    {'min_samples': 0}, {'min_samples': -1}, {'min_samples': True}, {'min_samples': 1.5},
    {'max_false_accept': -1}, {'max_false_accept': True}, {'max_false_accept': .5},
    {'max_mae': -1}, {'max_mae': 101}, {'max_mae': True}, {'max_mae': '8'},
    {'max_mae': float('nan')}, {'max_mae': float('inf')},
])
def test_malformed_gate_configuration_is_rejected(kwargs):
    with pytest.raises(ValueError):
        calibration_report([], **kwargs)


def test_provenance_metadata_is_allowed_but_does_not_create_extra_samples():
    record = sample()
    record['genre'] = 'family'
    record['expert']['reviewer'] = 'human-reviewer-id'
    record['ai']['modelRevision'] = 'frozen-test-revision'
    assert calibration_report([record], min_samples=1)['n'] == 1


def test_cli_json_output_and_exit_codes_are_usable_in_release_checks(tmp_path):
    source = tmp_path / 'synthetic-fixture.json'
    source.write_text(json.dumps([sample()]), encoding='utf-8')
    args = [sys.executable, '-m', 'app.services.academy_calibration', str(source)]
    failed = subprocess.run(args, capture_output=True, text=True, check=False)
    assert failed.returncode == 1
    assert json.loads(failed.stdout)['gates']['minimumSamples'] is False
    passed = subprocess.run(args + ['--min-samples', '1'], capture_output=True, text=True, check=False)
    assert passed.returncode == 0
    assert json.loads(passed.stdout)['passed'] is True
    assert not failed.stderr and not passed.stderr


@pytest.mark.parametrize('content', ['{broken json', '{}', '[{"id": "x", "id": "y"}]',
                                     '[{"id": "x", "expert": null}]',
                                     '[{"id": "x", "metadata": NaN}]',
                                     '[{"id": "x", "metadata": Infinity}]'])
def test_cli_malformed_data_prints_json_error_and_exits_two(tmp_path, capsys, content):
    source = tmp_path / 'bad.json'
    source.write_text(content, encoding='utf-8')
    assert main([str(source)]) == 2
    assert 'error' in json.loads(capsys.readouterr().out)


@pytest.mark.parametrize('args', [[], ['missing.json'], ['file.json', '--min-samples', 'oops'],
                                 ['file.json', '--unknown-argument']])
def test_cli_bad_path_or_arguments_print_json_error(args, capsys):
    assert main(args) == 2
    assert 'error' in json.loads(capsys.readouterr().out)
