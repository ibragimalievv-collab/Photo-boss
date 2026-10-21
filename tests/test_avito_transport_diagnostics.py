import asyncio
import socket
import ssl
import urllib.error
from unittest.mock import AsyncMock, Mock

import pytest

from app.avito_hr import API, transport_diagnostics


@pytest.mark.parametrize('reason,stage,number', [
    (socket.gaierror(-3, 'private DNS detail'), 'dns', -3),
    (TimeoutError('private timeout detail'), 'timeout', None),
    (ssl.SSLError(1, 'private TLS detail'), 'tls', 1),
    (ConnectionResetError(104, 'private reset detail'), 'transport', 104),
    ('https://private.example/?token=private', 'transport', None),
])
def test_transport_classification_never_returns_error_text(reason, stage, number):
    value = transport_diagnostics(urllib.error.URLError(urllib.error.URLError(reason)))
    assert value == {'stage': stage, 'reason_type': type(reason).__name__, 'errno': number}
    assert 'private' not in str(value)


def test_read_failure_logs_only_safe_metadata_and_keeps_bounded_retries(monkeypatch, caplog):
    async def run():
        api = API()
        api.call = Mock(side_effect=urllib.error.URLError(socket.gaierror(-3, 'token=private')))
        pause = AsyncMock()
        monkeypatch.setattr('app.avito_hr.asyncio.sleep', pause)
        with pytest.raises(urllib.error.URLError):
            await api.request('GET', '/core/v1/accounts/self')
        assert api.call.call_count == 3
        assert [call.args[0] for call in pause.await_args_list] == [2, 4]
    asyncio.run(run())
    assert 'stage=dns reason_type=gaierror errno=-3' in caplog.text
    assert 'private' not in caplog.text and 'token=' not in caplog.text


def test_uncertain_send_is_single_attempt_and_has_no_read_diagnostic(monkeypatch, caplog):
    async def run():
        api = API()
        api.call = Mock(side_effect=urllib.error.URLError('token=private'))
        pause = AsyncMock()
        monkeypatch.setattr('app.avito_hr.asyncio.sleep', pause)
        with pytest.raises(urllib.error.URLError):
            await api.request('POST', '/messenger/v1/accounts/1/chats/test/messages', {})
        assert api.call.call_count == 1 and pause.await_count == 0
    asyncio.run(run())
    assert 'read transport diagnostic' not in caplog.text
    assert 'private' not in caplog.text and 'token=' not in caplog.text
