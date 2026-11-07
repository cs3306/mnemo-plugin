"""The plugin protocol, as seen from the server side:

    GET  /healthz                 -> {"ok": true, ...}                     (no auth)
    GET  /v1/tools                -> {"tools": [{"name", "description", "parameters"}]}
    POST /v1/tools/{name}         <- {"arguments": {...}, "context": {"chat": 1, "channel": "telegram", "tz": "America/Vancouver"}}
                                  -> {"text": "...", "files": [{"name", "mime", "data": "<base64>"}]}  or {"error": "..."}

Every /v1 request needs "Authorization: Bearer $PLUGIN_TOKEN".
"""
from __future__ import annotations

import base64
import hmac
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import __version__
from .config import Config
from .registry import Context, Result, ToolError, active_tools, validate

log = logging.getLogger("mnemo_plugin")


class PluginServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.tools, self.disabled = active_tools(cfg)
        self.slots = threading.BoundedSemaphore(cfg.max_concurrency)
        super().__init__((cfg.host, cfg.port), Handler)


class Handler(BaseHTTPRequestHandler):
    server: PluginServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        log.info("%s %s", self.address_string(), fmt % args)

    # ------------------------------------------------------------------ helpers
    def _send(self, code: int, obj: Any) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self) -> bool:
        cfg = self.server.cfg
        if not cfg.token:
            return cfg.allow_anonymous
        got = self.headers.get("Authorization", "")
        return hmac.compare_digest(got.encode(), f"Bearer {cfg.token}".encode())

    # ------------------------------------------------------------------ routes
    def do_GET(self) -> None:
        if self.path == "/healthz":
            return self._send(200, {"ok": True, "version": __version__, "tools": sorted(self.server.tools), "disabled": self.server.disabled})
        if self.path == "/v1/tools":
            if not self._authorised():
                return self._send(401, {"error": "unauthorized"})
            return self._send(200, {"tools": [
                {"name": t.name, "description": t.description, "parameters": t.parameters} for t in self.server.tools.values()
            ]})
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        prefix = "/v1/tools/"
        if not self.path.startswith(prefix):
            return self._send(404, {"error": "not found"})
        if not self._authorised():
            self._drain()
            return self._send(401, {"error": "unauthorized"})
        tool = self.server.tools.get(self.path[len(prefix):])
        if tool is None:
            self._drain()
            return self._send(404, {"error": "unknown tool"})
        limit = self.server.cfg.max_request_mb << 20
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = -1
        if n < 0 or n > limit:
            self._drain()
            return self._send(413, {"error": f"request body must be at most {self.server.cfg.max_request_mb} MB"})
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
            args = req.get("arguments", {})
            c = req.get("context") or {}
            if not isinstance(args, dict) or not isinstance(c, dict):
                raise ValueError
        except ValueError:
            return self._send(400, {"error": "body must be {\"arguments\": {...}, \"context\": {...}}"})
        try:
            validate(args, tool.parameters)
            ctx = Context(config=self.server.cfg, chat=c.get("chat") if isinstance(c.get("chat"), int) else None, channel=str(c.get("channel", "")), tz=str(c.get("tz", "")))
            with self.server.slots:
                res = tool.func(args, ctx)
            if not isinstance(res, Result):
                res = Result(text=str(res or ""))
        except ToolError as e:
            return self._send(400, {"error": str(e)})
        except Exception:  # never leak a traceback to the model; it stays in the log
            log.exception("tool %s crashed", tool.name)
            return self._send(500, {"error": "插件内部错误，详情见服务日志"})
        self._send(200, {"text": res.text, "files": [
            {"name": f.name, "mime": f.mime, "data": base64.b64encode(f.data).decode()} for f in res.files
        ]})

    def _drain(self) -> None:
        """Read and discard the body so the client sees our answer instead of a reset connection; give up on huge ones."""
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = -1
        if n < 0 or n > 4 * (self.server.cfg.max_request_mb << 20):
            self.close_connection = True
            return
        while n > 0:
            chunk = self.rfile.read(min(n, 1 << 20))
            if not chunk:
                break
            n -= len(chunk)
