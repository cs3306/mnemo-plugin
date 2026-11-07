"""Configuration, read from environment variables once at startup."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


def _bool(v: str | None, default: bool) -> bool:
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int, env: dict) -> int:
    v = env.get(name, "").strip()
    if not v:
        return default
    try:
        return int(v)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {v!r}") from None


@dataclass
class Config:
    token: str = ""
    allow_anonymous: bool = False
    host: str = "0.0.0.0"
    port: int = 8090
    max_concurrency: int = 2
    max_request_mb: int = 8
    max_file_mb: int = 30
    tool_timeout: int = 120          # seconds for pandoc / TeX subprocesses
    extra_tools_dir: str = ""
    docx_cjk_font: str = "Microsoft YaHei"   # East-Asian font written into Word styles ("" = leave untouched)
    pdf_serif_font: str = ""         # "" = auto-detect a CJK-capable font with fontconfig
    pdf_mono_font: str = ""
    image_base_url: str = ""         # OpenAI-compatible, including /v1; empty disables image_generate
    image_api_key: str = ""
    image_model: str = ""
    image_size: str = "1024x1024"
    image_extra_body: dict = field(default_factory=dict)
    image_timeout: int = 300
    errors: list = field(default_factory=list)

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Config":
        e = dict(os.environ if env is None else env)
        c = cls()
        c.token = e.get("PLUGIN_TOKEN", "").strip()
        c.allow_anonymous = _bool(e.get("PLUGIN_ALLOW_ANONYMOUS"), False)
        addr = e.get("PLUGIN_ADDR", "0.0.0.0:8090").strip()
        host, _, port = addr.rpartition(":")
        c.host, c.port = host or "0.0.0.0", int(port or 8090)
        c.max_concurrency = max(1, _int("MAX_CONCURRENCY", 2, e))
        c.max_request_mb = max(1, _int("MAX_REQUEST_MB", 8, e))
        c.max_file_mb = max(1, _int("MAX_FILE_MB", 30, e))
        c.tool_timeout = max(5, _int("TOOL_TIMEOUT", 120, e))
        c.extra_tools_dir = e.get("EXTRA_TOOLS_DIR", "").strip()
        c.docx_cjk_font = e.get("DOCX_CJK_FONT", c.docx_cjk_font).strip()
        c.pdf_serif_font = e.get("PDF_CJK_FONT", "").strip()
        c.pdf_mono_font = e.get("PDF_CJK_MONO_FONT", "").strip()
        c.image_base_url = e.get("IMAGE_BASE_URL", "").strip().rstrip("/")
        c.image_api_key = e.get("IMAGE_API_KEY", "").strip()
        c.image_model = e.get("IMAGE_MODEL", "").strip()
        c.image_size = e.get("IMAGE_SIZE", c.image_size).strip()
        c.image_timeout = max(10, _int("IMAGE_TIMEOUT", 300, e))
        raw = e.get("IMAGE_EXTRA_BODY", "").strip()
        if raw:
            try:
                v = json.loads(raw)
                if not isinstance(v, dict):
                    raise ValueError
                c.image_extra_body = v
            except ValueError:
                c.errors.append("IMAGE_EXTRA_BODY must be a JSON object")
        return c

    def validate(self) -> list[str]:
        errs = list(self.errors)
        if not self.token and not self.allow_anonymous:
            errs.append("PLUGIN_TOKEN is not set (set it, or PLUGIN_ALLOW_ANONYMOUS=true if the port is not reachable by anyone else)")
        if self.image_base_url and not self.image_base_url.startswith(("http://", "https://")):
            errs.append("IMAGE_BASE_URL must be an http(s) URL")
        return errs
