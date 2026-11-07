import base64
import io
import json
import threading
import urllib.error
import urllib.request

import pytest

from mnemo_plugin.config import Config
from mnemo_plugin.loader import load_builtin
from mnemo_plugin.registry import Context
from mnemo_plugin.server import PluginServer

load_builtin()
TOKEN = "test-token"


def make_cfg(**env) -> Config:
    e = {"PLUGIN_TOKEN": TOKEN, "PLUGIN_ADDR": "127.0.0.1:0", **env}
    return Config.from_env(e)


@pytest.fixture
def ctx():
    return Context(config=make_cfg(), chat=1, channel="telegram")


class Client:
    def __init__(self, srv: PluginServer, token: str | None = TOKEN):
        self.base = f"http://127.0.0.1:{srv.server_address[1]}"
        self.token = token

    def req(self, method, path, body=None, token="default", raw=None):
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        r = urllib.request.Request(self.base + path, data=data, method=method)
        t = self.token if token == "default" else token
        if t:
            r.add_header("Authorization", "Bearer " + t)
        if data is not None:
            r.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(r, timeout=60) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def call(self, name, args, **kw):
        return self.req("POST", f"/v1/tools/{name}", {"arguments": args, "context": {"chat": 7, "channel": "telegram"}}, **kw)

    @staticmethod
    def file(res, i=0) -> bytes:
        return base64.b64decode(res["files"][i]["data"])

    @staticmethod
    def zip(res, i=0):
        import zipfile

        return zipfile.ZipFile(io.BytesIO(Client.file(res, i)))


@pytest.fixture
def server():
    started = []

    def start(**env):
        srv = PluginServer(make_cfg(**env))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        started.append(srv)
        return srv, Client(srv)

    yield start
    for s in started:
        s.shutdown()
        s.server_close()


@pytest.fixture
def api(server):
    return server()[1]
