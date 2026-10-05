"""The public installation shell must launch inside app scope without credentials."""
import json
import struct
import unittest
from urllib.parse import urljoin, urlsplit

import test_miniapp_release as fixtures
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


class InstallableAppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.MiniAppTests()
        await self.fixture.asyncSetUp()
        app = web.Application()
        self.fixture.service.register(app)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.fixture.asyncTearDown()

    async def test_manifest_icons_and_launch_are_public_but_working_data_are_not(self):
        response = await self.client.get('/app/manifest.webmanifest')
        assert response.status == 200
        assert response.content_type == 'application/manifest+json'
        manifest = json.loads(await response.text())
        assert manifest['display'] == 'standalone'
        assert manifest['scope'] == manifest['id'] == '/app/'
        assert manifest['prefer_related_applications'] is False
        launch = urlsplit(manifest['start_url'])
        assert launch.path == '/app/' and launch.query == 'source=installed'
        assert launch.fragment == 'home'
        for icon in manifest['icons']:
            response = await self.client.get(urljoin('/app/manifest.webmanifest', icon['src']))
            assert response.status == 200 and response.content_type == 'image/png'
            data = await response.read()
            assert data[:8] == b'\x89PNG\r\n\x1a\n'
            width, height = struct.unpack('>II', data[16:24])
            assert icon['sizes'] == f'{width}x{height}'
        assert {i['sizes'] for i in manifest['icons']} >= {'192x192', '512x512'}
        assert any(i['purpose'] == 'maskable' for i in manifest['icons'])
        response = await self.client.get('/app/assets/apple-touch-icon.png')
        assert response.status == 200
        assert struct.unpack('>II', (await response.read())[16:24]) == (180, 180)
        response = await self.client.get(manifest['start_url'])
        assert response.status == 200
        page = await response.text()
        assert 'rel="manifest"' in page and 'rel="apple-touch-icon"' in page
        assert 'id="installApp"' in page and './js/install.js' in page
        assert (await self.client.get('/app/js/install.js')).status == 200
        assert (await self.client.get('/api/miniapp/me')).status == 401
        assert (await self.client.get('/app/../config.py')).status == 404

    async def test_worker_caches_only_shell_including_install_assets(self):
        response = await self.client.get('/app/sw.js')
        assert response.status == 200
        source = await response.text()
        assert "'/app/manifest.webmanifest'" in source
        assert 'icon-maskable-512' in source and "'install'" in source
        assert 'browser-login' in source
        assert '/auth/browser' not in source and '/api/miniapp' not in source
