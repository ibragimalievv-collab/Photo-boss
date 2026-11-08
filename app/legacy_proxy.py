"""Fixed HTTPS relay for existing Render browser sessions and offline queues.

Run this module alone on Render after the Timeweb cutover. It never imports the
application configuration, database, bot entry points or background workers.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
import ipaddress
import json
import os
import re
import ssl
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from aiohttp import web
from multidict import CIMultiDict
from yarl import URL

UPSTREAM = 'https://photo-boss.ru'
LEGACY_ORIGIN = 'https://photo-boss.onrender.com'
BUFFER_SIZE = 64 * 1024
MAX_HEALTH_BYTES = 64 * 1024
MAX_WS_MESSAGE_BYTES = 25 * 1024 * 1024
HOP_HEADERS = frozenset({'connection', 'keep-alive', 'proxy-authenticate',
                         'proxy-authorization', 'proxy-connection', 'te',
                         'trailer', 'transfer-encoding', 'upgrade'})
WS_HEADERS = frozenset({'sec-websocket-key', 'sec-websocket-version',
                        'sec-websocket-extensions', 'sec-websocket-protocol',
                        'sec-websocket-accept'})
SAFE_HEADERS = {'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
                'X-Content-Type-Options': 'nosniff'}
PROTOCOL_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


class RelayUnavailable(RuntimeError):
    """A fixed internal failure without an upstream URL or provider response."""


def excluded_headers(headers, *, websocket=False):
    excluded = set(HOP_HEADERS)
    for value in headers.getall('Connection', []):
        excluded.update(part.strip().lower() for part in value.split(','))
    if websocket:
        excluded.update(WS_HEADERS)
    return excluded


def rewrite_location(value):
    """Rewrite canonical redirects, preserve safe provider HTTPS redirects.

    The relay never follows a redirect itself. Relative application redirects
    keep the old origin, and externally hosted downloads remain browser-owned.
    """
    if (not isinstance(value, str) or not value or value != value.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or '\\' in value):
        raise RelayUnavailable()
    try:
        parsed = urlsplit(value)
        if parsed.username or parsed.password:
            raise ValueError
        if not parsed.scheme and not parsed.netloc:
            # Browsers normalize three or more leading slashes as a network
            # authority even though urllib parses them as a relative path.
            if value.startswith('//'):
                raise ValueError
            return value
        if parsed.scheme != 'https' or not parsed.hostname:
            raise ValueError
        # Accessing .port also rejects malformed or out-of-range ports.
        port = parsed.port
        if parsed.hostname == 'photo-boss.ru' and port in (None, 443):
            legacy = urlsplit(LEGACY_ORIGIN)
            return urlunsplit((legacy.scheme, legacy.netloc, parsed.path,
                               parsed.query, parsed.fragment))
        return value
    except (ValueError, UnicodeError):
        raise RelayUnavailable() from None


def safe_close_code(value):
    if type(value) is int and (value in {1000, 1001, 1002, 1003, 1007, 1008,
                                       1009, 1010, 1011, 1012, 1013, 1014}
                              or 3000 <= value <= 4999):
        return value
    return 1001


class LegacyProxy:
    def __init__(self):
        self.upstream = URL(UPSTREAM)
        self.tls = ssl.create_default_context()
        self.session = None
        self._handshake_headers = ContextVar('legacy_relay_handshake_headers', default=None)

    @classmethod
    def for_testing(cls, upstream):
        """Explicit local-HTTP seam; production has no configurable upstream."""
        value = URL(upstream)
        try:
            local = value.host == 'localhost' or ipaddress.ip_address(value.host).is_loopback
        except (ValueError, TypeError):
            local = False
        if (value.scheme != 'http' or not local or value.user or value.password
                or value.path not in ('', '/') or value.query_string or value.fragment):
            raise ValueError('Tests require a loopback HTTP origin')
        result = cls()
        result.upstream = value.origin()
        return result

    async def lifecycle(self, app):
        trace = aiohttp.TraceConfig()

        async def deny_redirect(_session, _context, _params):
            # ws_connect has no allow_redirects option in the pinned aiohttp;
            # this supported trace runs before any redirect request is made.
            raise RelayUnavailable()

        async def capture_handshake(_session, _context, params):
            destination = self._handshake_headers.get()
            if destination is not None and params.response.status == 101:
                destination.extend(self.response_headers(params.response.headers, websocket=True).items())

        trace.on_request_redirect.append(deny_redirect)
        trace.on_request_end.append(capture_handshake)
        connector = aiohttp.TCPConnector(ssl=self.tls, limit=100)
        timeout = aiohttp.ClientTimeout(total=None, connect=15, sock_connect=15, sock_read=180)
        self.session = aiohttp.ClientSession(
            connector=connector, timeout=timeout, cookie_jar=aiohttp.DummyCookieJar(),
            auto_decompress=False, trust_env=False, trace_configs=[trace],
            skip_auto_headers={'User-Agent', 'Accept-Encoding', 'Content-Type'},
            max_line_size=32768, max_field_size=32768,
        )
        try:
            yield
        finally:
            await self.session.close()
            self.session = None

    @staticmethod
    def unavailable(status=503):
        message = ('Запрос отклонён. Откройте Фотобосс по его адресу.' if status == 403 else
                   'Фотобосс временно недоступен. Повторите попытку.')
        headers = dict(SAFE_HEADERS)
        if status == 503:
            headers['Retry-After'] = '5'
        return web.json_response({'error': message}, status=status, headers=headers)

    @staticmethod
    def is_websocket(request):
        return request.headers.get('Upgrade', '').lower() == 'websocket'

    def origin_allowed(self, request):
        values = request.headers.getall('Origin', [])
        if len(values) > 1:
            return False
        if values:
            return values[0] == LEGACY_ORIGIN
        return request.method in {'GET', 'HEAD'} and not self.is_websocket(request)

    def target(self, request):
        raw = request.raw_path
        if (not raw.startswith('/') or '#' in raw
                or any(ord(c) < 32 or ord(c) == 127 for c in raw)):
            raise RelayUnavailable()
        result = URL(str(self.upstream) + raw, encoded=True)
        if result.origin() != self.upstream.origin():
            raise RelayUnavailable()
        return result

    def request_headers(self, request, *, websocket=False):
        excluded = excluded_headers(request.headers, websocket=websocket)
        headers = CIMultiDict()
        for key, value in request.headers.items():
            lower = key.lower()
            if (lower in excluded or lower in {'host', 'origin', 'expect', 'x-real-ip', 'forwarded'}
                    or lower.startswith(('x-forwarded-', 'forwarded-'))):
                continue
            headers.add(key, value)
        headers['Host'] = self.upstream.raw_authority
        if request.headers.getall('Origin', []):
            headers['Origin'] = UPSTREAM
        # Never hide a browser's cross-site signal through Connection tokens.
        headers.popall('Sec-Fetch-Site', None)
        for value in request.headers.getall('Sec-Fetch-Site', []):
            headers.add('Sec-Fetch-Site', value)
        peer = request.remote or 'unknown'
        try:
            parsed = ipaddress.ip_address(peer)
            forwarded_peer = '[' + str(parsed) + ']' if parsed.version == 6 else str(parsed)
        except ValueError:
            peer = forwarded_peer = 'unknown'
        headers['X-Forwarded-For'] = peer
        headers['X-Real-IP'] = peer
        headers['X-Forwarded-Host'] = urlsplit(LEGACY_ORIGIN).netloc
        headers['X-Forwarded-Proto'] = 'https'
        headers['X-Forwarded-Port'] = '443'
        headers['Forwarded'] = (f'for="{forwarded_peer}";proto=https;'
                                f'host="{urlsplit(LEGACY_ORIGIN).netloc}"')
        return headers

    @staticmethod
    def response_headers(source, *, websocket=False):
        excluded = excluded_headers(source, websocket=websocket)
        headers = CIMultiDict()
        locations = source.getall('Location', [])
        if len(locations) > 1:
            raise RelayUnavailable()
        for key, value in source.items():
            if key.lower() in excluded:
                continue
            headers.add(key, rewrite_location(value) if key.lower() == 'location' else value)
        return headers

    async def health(self, request):
        try:
            target = URL(str(self.upstream) + '/health', encoded=True)
            async with self.session.get(target, allow_redirects=False,
                                        timeout=aiohttp.ClientTimeout(total=8)) as upstream:
                if upstream.status != 200:
                    raise RelayUnavailable()
                data = bytearray()
                while True:
                    chunk = await upstream.content.read(min(BUFFER_SIZE, MAX_HEALTH_BYTES + 1 - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) > MAX_HEALTH_BYTES:
                        raise RelayUnavailable()
                value = json.loads(data)
                if not isinstance(value, dict):
                    raise RelayUnavailable()
                value['legacyRelay'] = True
                return web.json_response(value, headers=SAFE_HEADERS)
        except asyncio.CancelledError:
            raise
        except Exception:
            return self.unavailable()

    async def http(self, request):
        response = None
        try:
            headers = self.request_headers(request)
            data = request.content.iter_chunked(BUFFER_SIZE) if request.can_read_body else None
            async with self.session.request(request.method, self.target(request), headers=headers,
                                            data=data, allow_redirects=False) as upstream:
                response = web.StreamResponse(status=upstream.status,
                                              headers=self.response_headers(upstream.headers))
                await response.prepare(request)
                if request.method != 'HEAD':
                    async for chunk in upstream.content.iter_chunked(BUFFER_SIZE):
                        await response.write(chunk)
                await response.write_eof()
                return response
        except asyncio.CancelledError:
            raise
        except Exception:
            if response is not None and response.prepared:
                # Never acknowledge a truncated upload/download as complete.
                response.force_close()
                if request.transport is not None:
                    request.transport.close()
                return response
            return self.unavailable()

    async def websocket(self, request):
        downstream = None
        upstream = None
        tasks = []
        try:
            protocols = tuple(p.strip() for value in request.headers.getall('Sec-WebSocket-Protocol', [])
                              for p in value.split(','))
            if len(protocols) > 64 or any(not PROTOCOL_RE.fullmatch(p) for p in protocols):
                raise RelayUnavailable()
            handshake = []
            context_token = self._handshake_headers.set(handshake)
            try:
                upstream = await self.session.ws_connect(
                    self.target(request), headers=self.request_headers(request, websocket=True),
                    protocols=protocols, autoping=False, autoclose=False,
                    timeout=aiohttp.ClientWSTimeout(ws_close=5),
                    max_msg_size=MAX_WS_MESSAGE_BYTES,
                )
            finally:
                self._handshake_headers.reset(context_token)
            downstream = web.WebSocketResponse(
                protocols=(upstream.protocol,) if upstream.protocol else (),
                autoping=False, autoclose=False, timeout=5, max_msg_size=MAX_WS_MESSAGE_BYTES,
            )
            for key, value in handshake:
                downstream.headers.add(key, value)
            await downstream.prepare(request)

            async def pump(source, destination):
                # aiohttp's async iterator discards CLOSE frames, including
                # their application code. Receive explicitly so clients keep
                # the upstream's session-expiry/reconnect semantics.
                while True:
                    message = await source.receive()
                    if message.type == aiohttp.WSMsgType.TEXT:
                        await destination.send_str(message.data)
                    elif message.type == aiohttp.WSMsgType.BINARY:
                        await destination.send_bytes(message.data)
                    elif message.type == aiohttp.WSMsgType.PING:
                        await destination.ping(message.data)
                    elif message.type == aiohttp.WSMsgType.PONG:
                        await destination.pong(message.data)
                    elif message.type == aiohttp.WSMsgType.CLOSE:
                        await destination.close(code=safe_close_code(message.data),
                                                message=(message.extra or '').encode())
                        return
                    elif message.type in {aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED}:
                        await destination.close(code=safe_close_code(source.close_code))
                        return
                    elif message.type == aiohttp.WSMsgType.ERROR:
                        raise RelayUnavailable()

            tasks = [asyncio.create_task(pump(downstream, upstream)),
                     asyncio.create_task(pump(upstream, downstream))]
            done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            return downstream
        except asyncio.CancelledError:
            raise
        except Exception:
            if downstream is not None and downstream.prepared:
                await downstream.close(code=1011, message=b'Temporarily unavailable')
                return downstream
            return self.unavailable()
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            closers = [endpoint.close() for endpoint in (upstream, downstream)
                       if endpoint is not None and not endpoint.closed]
            if closers:
                try:
                    async with asyncio.timeout(6):
                        await asyncio.gather(*closers, return_exceptions=True)
                except TimeoutError:
                    if request.transport is not None:
                        request.transport.close()

    async def handle(self, request):
        if not self.origin_allowed(request):
            return self.unavailable(403)
        if request.path == '/health' and request.method in {'GET', 'HEAD'}:
            return await self.health(request)
        if self.is_websocket(request):
            return await self.websocket(request)
        return await self.http(request)


RELAY_KEY = web.AppKey('legacy_proxy', LegacyProxy)


def _application(relay):
    app = web.Application(handler_args={'auto_decompress': False,
                                        'max_line_size': 32768, 'max_field_size': 32768},
                          client_max_size=32 * 1024 * 1024)
    app[RELAY_KEY] = relay
    app.cleanup_ctx.append(relay.lifecycle)
    app.router.add_route('*', '/{path:.*}', relay.handle)
    return app


def create_app():
    return _application(LegacyProxy())


def create_test_app(upstream):
    return _application(LegacyProxy.for_testing(upstream))


def main():
    raw = os.environ.get('PORT', '10000')
    if not raw.isascii() or not raw.isdigit() or not 0 < int(raw) < 65536:
        raise SystemExit('Invalid relay port')
    web.run_app(create_app(), host='0.0.0.0', port=int(raw), print=None, access_log=None)


if __name__ == '__main__':
    main()
