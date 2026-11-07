import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32


@pytest.fixture
def backend():
    seen = {}
    mode = {"kind": "b64"}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            seen["path"] = self.path
            seen["auth"] = self.headers.get("Authorization")
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            k = mode["kind"]
            if k == "err":
                self.send_response(500); self.end_headers(); self.wfile.write(b"model not loaded"); return
            if k == "text":
                data = {"data": [{"b64_json": base64.b64encode(b"not an image").decode()}]}
            elif k == "url":
                data = {"data": [{"url": f"http://127.0.0.1:{self.server.server_address[1]}/img.png"}]}
            elif k == "bad":
                data = {"nope": 1}
            else:
                data = {"data": [{"b64_json": base64.b64encode(PNG).decode()}]}
            out = json.dumps(data).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

        def do_GET(self):
            self.send_response(200); self.send_header("Content-Length", str(len(PNG))); self.end_headers(); self.wfile.write(PNG)

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1", seen, mode
    srv.shutdown()
    srv.server_close()


def test_tool_absent_without_backend(api):
    code, res = api.call("image_generate", {"prompt": "x"})
    assert code == 404


def test_generates_png_from_b64(server, backend):
    url, seen, _ = backend
    _, c = server(IMAGE_BASE_URL=url, IMAGE_API_KEY="k", IMAGE_MODEL="sd", IMAGE_EXTRA_BODY='{"steps": 20}')
    code, res = c.call("image_generate", {"prompt": "一只猫", "size": "512x768", "filename": "猫"})
    assert code == 200 and res["files"][0]["name"] == "猫.png" and res["files"][0]["mime"] == "image/png"
    assert c.file(res) == PNG
    assert seen["path"] == "/v1/images/generations" and seen["auth"] == "Bearer k"
    assert seen["body"]["prompt"] == "一只猫" and seen["body"]["size"] == "512x768" and seen["body"]["model"] == "sd" and seen["body"]["steps"] == 20


def test_downloads_url_results(server, backend):
    url, _, mode = backend
    mode["kind"] = "url"
    _, c = server(IMAGE_BASE_URL=url)
    code, res = c.call("image_generate", {"prompt": "x"})
    assert code == 200 and c.file(res) == PNG


def test_backend_problems_are_reported(server, backend):
    url, _, mode = backend
    _, c = server(IMAGE_BASE_URL=url)
    for kind, want in (("err", "HTTP 500"), ("text", "不是图片"), ("bad", "OpenAI 格式")):
        mode["kind"] = kind
        code, res = c.call("image_generate", {"prompt": "x"})
        assert code == 400 and want in res["error"], (kind, res)
    assert c.call("image_generate", {"prompt": "x", "size": "huge"})[0] == 400
    _, down = server(IMAGE_BASE_URL="http://127.0.0.1:9/v1")
    code, res = down.call("image_generate", {"prompt": "x"})
    assert code == 400 and "无法连接" in res["error"]
