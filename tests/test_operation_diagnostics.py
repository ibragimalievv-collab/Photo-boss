import json
import logging

from app.miniapp_security import AccessError
from app.workday import operation_diagnostic


def test_operation_diagnostics_report_progress_without_private_packet_fields(caplog):
    caplog.set_level(logging.INFO, logger='app.workday')
    body = {'kind': 'sale_selected', 'key': 'private-operation-key',
            'data': {'phone': 'private-phone', 'room': 'private-room'}}
    operation_diagnostic(body, result={'bookingId': 32, 'draftId': 6, 'photoId': 29,
        'selectedPhotos': 4, 'declaredSold': 16, 'declaredTotal': 156,
        'amount': '6400.00', 'receipt': 'private-receipt'})
    detail = json.loads(caplog.records[0].message.split(': ', 1)[1])
    assert detail['selectedPhotos'] == 4 and detail['declaredSold'] == 16
    assert detail['duplicate'] is False
    for private in ('private-operation-key', 'private-phone', 'private-room', 'private-receipt', '6400.00'):
        assert private not in caplog.text


def test_operation_rejections_have_fixed_reason_codes_and_no_reflected_message(caplog):
    caplog.set_level(logging.INFO, logger='app.workday')
    operation_diagnostic({'kind': 'sale_counts'}, error=AccessError('Полная съёмка содержит 10 кадров. Общее количество менять нельзя.', 409))
    assert 'full_count_changed' in caplog.text
    operation_diagnostic({'kind': 'sale_selected'}, error=AccessError('Unknown private filename', 400))
    assert 'Unknown private filename' not in caplog.text
    before = len(caplog.records)
    operation_diagnostic({'kind': 'private-user-controlled-kind'}, error=AccessError('x', 400))
    assert len(caplog.records) == before
