"""Bound expensive album/photo requests before authentication opens DB leases."""
import asyncio
import re

from aiohttp import web

DOWNLOAD_PATH = re.compile(
    r'^(?:/g/[A-Za-z0-9_-]{32}/(?:photos/[0-9]+|album\.zip)'
    r'|/api/miniapp/(?:delivery/[0-9]+/photos/[0-9]+|galleries/[0-9]+/archive))$'
)
MAX_DOWNLOAD_REQUESTS = 8
DOWNLOAD_WAIT_SECONDS = 2


def install_download_admission(app):
    slots = asyncio.Semaphore(MAX_DOWNLOAD_REQUESTS)

    @web.middleware
    async def admission(request, handler):
        if request.method not in {'GET', 'HEAD'} or not DOWNLOAD_PATH.fullmatch(request.path):
            return await handler(request)
        # Brief ordinary thumbnail bursts may queue, without opening any DB
        # lease. A slow provider cannot create an unlimited active download set.
        try:
            await asyncio.wait_for(slots.acquire(), DOWNLOAD_WAIT_SECONDS)
        except TimeoutError:
            return web.json_response(
                {'error': 'Сейчас скачивается несколько альбомов. Повторите через несколько секунд.'},
                status=503, headers={'Retry-After': '2', 'Cache-Control': 'no-store'},
            )
        try:
            return await handler(request)
        finally:
            slots.release()

    app.middlewares.insert(0, admission)
