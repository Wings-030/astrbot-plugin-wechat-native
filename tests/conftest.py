import os
import sys
import tempfile
import types
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("ASTRBOT_ROOT", tempfile.mkdtemp(prefix="astrbot-native-test-"))
# Load as a plugin package exactly as AstrBot does, independent of checkout basename.
package = types.ModuleType("wechat_plugin")
package.__path__ = [str(ROOT)]
sys.modules["wechat_plugin"] = package


def module(name):
    if not (ROOT / f"{name}.py").exists():
        pytest.fail(f"missing implementation: {name}.py")
    return __import__(f"wechat_plugin.{name}", fromlist=[name])


@pytest.fixture
async def serve():
    runners = []

    async def start(handler):
        app = web.Application(client_max_size=32 * 1024 * 1024)
        app.router.add_route("*", "/{path:.*}", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        runners.append(runner)
        return f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"

    yield start
    for runner in runners:
        await runner.cleanup()


@pytest.fixture
def public_image_url(serve, monkeypatch):
    async def start(handler):
        url = await serve(handler)
        port = urlsplit(url).port

        async def resolve(self, host, port=0, family=0):
            # Route a public-name test URL to the isolated mock server only.
            return [
                {
                    "hostname": host,
                    "host": "127.0.0.1",
                    "port": urlsplit(url).port,
                    "family": 2,
                    "proto": 0,
                    "flags": 0,
                }
            ]

        assert port
        monkeypatch.setattr(module("native_client")._PublicMediaResolver, "resolve", resolve)
        return "https://public.example/image".replace("https:", "http:")

    return start
