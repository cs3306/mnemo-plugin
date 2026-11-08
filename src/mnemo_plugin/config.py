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
    mnemo_url: str = ""              # where mnemo's API listens (http://mnemo:8088); needed by the schedule_* tools
    mnemo_api_key: str = ""          # mnemo's API_KEY
    data_dir: str = "/data"          # persistent state (schedules.json); mount a volume here
    schedule_tz: str = ""            # fallback time zone when mnemo does not send one ("" = UTC)
    schedule_grace_min: int = 120    # a task missed by more than this (e.g. server was down) is skipped, not run late
    dev_ssh_host: str = ""           # dev container to run commands in (dev_exec); empty disables the tool
    dev_ssh_port: int = 22
    dev_ssh_user: str = "abc"
    dev_ssh_key: str = "/run/secrets/dev_ssh"
    dev_ssh_root: str = "/workspace/ai"      # default working directory; the cwd argument must stay inside it
    dev_ssh_max_timeout: int = 300           # upper bound for a single command, seconds
    dev_ssh_max_output_kb: int = 32
    dev_inbox: str = "inbox"                 # where files sent to the bot land, relative to dev_ssh_root
    dev_upload_max_mb: int = 20              # largest file accepted from the bot (Telegram itself caps downloads at 20 MB)
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
        c.mnemo_url = e.get("MNEMO_URL", "").strip().rstrip("/")
        c.mnemo_api_key = e.get("MNEMO_API_KEY", "").strip()
        c.data_dir = e.get("DATA_DIR", c.data_dir).strip() or c.data_dir
        c.schedule_tz = e.get("SCHEDULE_TZ", "").strip()
        c.schedule_grace_min = max(1, _int("SCHEDULE_GRACE_MIN", 120, e))
        c.dev_ssh_host = e.get("DEV_SSH_HOST", "").strip()
        c.dev_ssh_port = max(1, _int("DEV_SSH_PORT", 22, e))
        c.dev_ssh_user = e.get("DEV_SSH_USER", c.dev_ssh_user).strip() or c.dev_ssh_user
        c.dev_ssh_key = e.get("DEV_SSH_KEY", c.dev_ssh_key).strip() or c.dev_ssh_key
        c.dev_ssh_root = e.get("DEV_SSH_ROOT", c.dev_ssh_root).strip() or c.dev_ssh_root
        c.dev_ssh_max_timeout = max(5, _int("DEV_SSH_MAX_TIMEOUT", 300, e))
        c.dev_ssh_max_output_kb = max(1, _int("DEV_SSH_MAX_OUTPUT_KB", 32, e))
        c.dev_inbox = e.get("DEV_INBOX", c.dev_inbox).strip().strip("/") or c.dev_inbox
        c.dev_upload_max_mb = max(1, _int("DEV_UPLOAD_MAX_MB", 20, e))
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
        if self.mnemo_url and not self.mnemo_url.startswith(("http://", "https://")):
            errs.append("MNEMO_URL must be an http(s) URL")
        if self.dev_ssh_host and not self.dev_ssh_root.startswith("/"):
            errs.append("DEV_SSH_ROOT must be an absolute path")
        if ".." in self.dev_inbox.split("/"):
            errs.append("DEV_INBOX must not contain '..'")
        if self.schedule_tz:
            try:
                from zoneinfo import ZoneInfo

                ZoneInfo(self.schedule_tz)
            except Exception:
                errs.append(f"SCHEDULE_TZ {self.schedule_tz!r} is not a valid time zone name")
        return errs
