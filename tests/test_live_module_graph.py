"""Every local dependency of the live entry point must be served by the real allowlist."""
import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

from aiohttp import web

from app.delivery import Delivery
from app.miniapp_api import MiniApp


def test_live_module_graph_is_accessible_through_static_handler():
    async def run():
        root = Path(__file__).resolve().parents[1] / 'app' / 'webapp'
        api = object.__new__(MiniApp)
        api.static_dir = root
        pending = ['js/app.js', 'js/install.js']
        visited = set()
        while pending:
            name = pending.pop()
            if name in visited:
                continue
            visited.add(name)
            response = await api.static_file(SimpleNamespace(match_info={'asset': name}))
            assert isinstance(response, web.FileResponse), name
            source = (root / name).read_text()
            for match in re.finditer(r"\b(?:import|export)\s+[^;]*?from\s*['\"](\.[^'\"]+\.js)['\"]", source):
                dependency = (root / name).parent.joinpath(match.group(1)).resolve().relative_to(root)
                pending.append(dependency.as_posix())
        assert 'js/delivery.js' in visited
        document = Delivery(SimpleNamespace()).document('Example', '<p>Example</p>')
        for match in re.finditer(r'href="(/app/[^"]+\.css)"', document):
            name = match.group(1).removeprefix('/app/')
            response = await api.static_file(SimpleNamespace(match_info={'asset': name}))
            assert isinstance(response, web.FileResponse), name
    asyncio.run(run())
