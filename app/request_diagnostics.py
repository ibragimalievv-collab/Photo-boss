"""Timing for operational requests without logging credentials or guest data."""
import asyncio
import logging
from time import monotonic
from uuid import uuid4

from aiohttp import web

logger = logging.getLogger(__name__)
SLOW_REQUEST_SECONDS = 2


@web.middleware
async def request_diagnostics(request, handler):
    if not request.path.startswith('/api/miniapp/'):
        return await handler(request)
    started = monotonic()
    request_id = uuid4().hex
    resource = request.match_info.route.resource
    route = resource.canonical if resource is not None else '<unmatched>'
    status = 500
    try:
        response = await handler(request)
        status = response.status
        response.headers['X-PhotoBoss-Request-Id'] = request_id
        return response
    except asyncio.CancelledError:
        status = 499
        raise
    except web.HTTPException as exc:
        status = exc.status
        raise
    finally:
        elapsed = monotonic() - started
        if request.method not in {'GET', 'HEAD', 'OPTIONS'} or elapsed >= SLOW_REQUEST_SECONDS or status >= 500:
            logger.log(
                logging.WARNING if elapsed >= SLOW_REQUEST_SECONDS or status >= 500 else logging.INFO,
                'API request method=%s route=%s status=%s elapsed_ms=%s request_id=%s',
                request.method, route, status, round(elapsed * 1000), request_id,
            )
