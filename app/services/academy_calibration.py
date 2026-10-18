"""Compare frozen AI reviews with independent human-expert labels.

This module does not label images, call a provider, or claim that supplied expert
labels are genuine. Provenance and independent labelling belong to the collection
protocol; this utility checks the submitted records and measures their agreement.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

DECISIONS = ('ACCEPT', 'REVISION', 'MANUAL_REVIEW')


def _number(value: Any, label: str, *, maximum: float = 100) -> float:
    if type(value) not in (int, float):
        raise ValueError(f'{label} must be a number, not a boolean or string.')
    try:
        result = float(value)
    except (OverflowError, ValueError):
        raise ValueError(f'{label} must be finite and between 0 and {maximum:g}.') from None
    if not math.isfinite(result) or not 0 <= result <= maximum:
        raise ValueError(f'{label} must be finite and between 0 and {maximum:g}.')
    return result


def calibration_report(
    records: list[dict[str, Any]],
    *,
    min_samples: int = 20,
    max_false_accept: int = 0,
    max_mae: float = 8,
) -> dict[str, Any]:
    """Return metrics and explicit gates without modifying the input.

    Each record needs a unique nonempty string ``id`` and independent ``expert``
    and ``ai`` objects with a decision from DECISIONS and a finite 0..100 score.
    Additional provenance fields are allowed, but not interpreted as proof.

    ``decisionAgreement`` is a fraction in 0..1. Confusion-matrix rows are expert
    decisions and columns are AI decisions. A false accept is AI ACCEPT when the
    expert did not accept (including MANUAL_REVIEW). A false reject is automatic
    AI REVISION when the expert accepted; deferral to MANUAL_REVIEW is separate.
    Empty data has undefined (null) MAE/agreement and cannot pass the gates.
    """
    if type(min_samples) is not int or min_samples < 1:
        raise ValueError('min_samples must be a positive integer.')
    if type(max_false_accept) is not int or max_false_accept < 0:
        raise ValueError('max_false_accept must be a nonnegative integer.')
    allowed_mae = _number(max_mae, 'max_mae')
    if not isinstance(records, list):
        raise ValueError('records must be a JSON array (list).')  # noqa: TRY004 - one public validation error contract

    matrix = {expert: {ai: 0 for ai in DECISIONS} for expert in DECISIONS}
    seen_ids: set[str] = set()
    errors: list[float] = []
    agreement = false_accept = false_reject = 0
    for index, record in enumerate(records):
        label = f'records[{index}]'
        if not isinstance(record, dict):
            raise ValueError(f'{label} must be an object.')  # noqa: TRY004 - one public validation error contract
        sample_id = record.get('id')
        if not isinstance(sample_id, str) or not sample_id or sample_id != sample_id.strip():
            raise ValueError(f'{label}.id must be a nonempty string without surrounding whitespace.')
        if sample_id in seen_ids:
            raise ValueError(f'{label}.id duplicates an earlier sample id.')
        seen_ids.add(sample_id)

        reviews: dict[str, tuple[str, float]] = {}
        for role in ('expert', 'ai'):
            review = record.get(role)
            if not isinstance(review, dict):
                raise ValueError(f'{label}.{role} must be an object.')  # noqa: TRY004 - one public validation error contract
            decision = review.get('decision')
            if not isinstance(decision, str) or decision not in DECISIONS:
                raise ValueError(f'{label}.{role}.decision must be ACCEPT, REVISION, or MANUAL_REVIEW.')
            score = _number(review.get('score'), f'{label}.{role}.score')
            reviews[role] = decision, score

        expert_decision, expert_score = reviews['expert']
        ai_decision, ai_score = reviews['ai']
        matrix[expert_decision][ai_decision] += 1
        errors.append(abs(expert_score - ai_score))
        agreement += expert_decision == ai_decision
        false_accept += ai_decision == 'ACCEPT' and expert_decision != 'ACCEPT'
        false_reject += ai_decision == 'REVISION' and expert_decision == 'ACCEPT'

    n = len(records)
    mae = math.fsum(errors) / n if n else None
    gates = {
        'minimumSamples': n >= min_samples,
        'maximumFalseAccepts': false_accept <= max_false_accept,
        'maximumMeanAbsoluteScoreError': mae is not None and mae <= allowed_mae,
    }
    return {
        'n': n,
        'meanAbsoluteScoreError': round(mae, 6) if mae is not None else None,
        'decisionAgreement': round(agreement / n, 6) if n else None,
        'falseAcceptCount': false_accept,
        'falseRejectCount': false_reject,
        'confusionMatrix': matrix,
        'thresholds': {
            'minSamples': min_samples,
            'maxFalseAccept': max_false_accept,
            'maxMeanAbsoluteScoreError': allowed_mae,
        },
        'gates': gates,
        'passed': all(gates.values()),
    }


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'Duplicate JSON object key: {key}.')
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError(f'Nonfinite JSON number is not allowed: {value}.')


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument('path', type=Path, help='JSON array of independently expert-labelled samples')
    parser.add_argument('--min-samples', type=int, default=20)
    parser.add_argument('--max-false-accept', type=int, default=0)
    parser.add_argument('--max-mae', type=float, default=8)
    try:
        args = parser.parse_args(argv)
        with args.path.open(encoding='utf-8') as source:
            records = json.load(source, object_pairs_hook=_unique_object,
                                parse_constant=_invalid_constant)
        report = calibration_report(
            records,
            min_samples=args.min_samples,
            max_false_accept=args.max_false_accept,
            max_mae=args.max_mae,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False, allow_nan=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
