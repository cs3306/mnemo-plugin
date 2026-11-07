"""Image generation through any OpenAI-compatible /images/generations endpoint (a local Stable Diffusion / FLUX
gateway, a hosted API, ...). The tool only exists when IMAGE_BASE_URL is set."""
from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.request

from ..registry import Context, File, Result, ToolError, tool
from ..util import MIME, file_name


def _sniff(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    return None


def _http(req: urllib.request.Request, timeout: int, limit: int) -> bytes:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(limit + 1)
    except urllib.error.HTTPError as e:
        detail = e.read(500).decode(errors="replace").strip()
        raise ToolError(f"图像后端返回 HTTP {e.code}：{detail}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ToolError(f"无法连接图像后端：{getattr(e, 'reason', e)}") from None
    if len(data) > limit:
        raise ToolError("图像后端返回的内容过大")
    return data


@tool(
    "image_generate",
    "根据文字描述生成一张图片并发给用户。prompt 请写成详细的画面描述（主体、风格、构图、光线、色调）；"
    "后端对中文的理解不一定好，必要时用英文写 prompt。生成需要几十秒。",
    {"type": "object", "properties": {
        "prompt": {"type": "string", "maxLength": 4000},
        "size": {"type": "string", "description": "如 1024x1024、768x1024、1024x768；不确定就不填"},
        "filename": {"type": "string", "description": "可选"},
    }, "required": ["prompt"]},
    available=lambda cfg: None if cfg.image_base_url else "IMAGE_BASE_URL 未设置",
)
def image_generate(args: dict, ctx: Context) -> Result:
    cfg = ctx.config
    size = args.get("size") or cfg.image_size
    if not re.fullmatch(r"\d{2,4}x\d{2,4}", size):
        raise ToolError("size 格式应为 宽x高，如 1024x1024")
    body = {"prompt": args["prompt"], "n": 1, "size": size, "response_format": "b64_json", **cfg.image_extra_body}
    if cfg.image_model:
        body["model"] = cfg.image_model
    headers = {"Content-Type": "application/json"}
    if cfg.image_api_key:
        headers["Authorization"] = "Bearer " + cfg.image_api_key
    limit = cfg.max_file_mb << 20
    raw = _http(urllib.request.Request(cfg.image_base_url + "/images/generations", json.dumps(body).encode(), headers), cfg.image_timeout, limit * 2)
    try:
        item = json.loads(raw)["data"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        raise ToolError("图像后端返回的不是 OpenAI 格式（缺少 data[0]）") from None
    if item.get("b64_json"):
        try:
            data = base64.b64decode(item["b64_json"], validate=False)
        except ValueError:
            raise ToolError("图像后端返回的 b64_json 无法解码") from None
    elif str(item.get("url", "")).startswith(("http://", "https://")):
        data = _http(urllib.request.Request(item["url"]), cfg.image_timeout, limit)
    else:
        raise ToolError("图像后端既没有返回 b64_json 也没有返回 url")
    ext = _sniff(data)
    if ext is None:
        raise ToolError("图像后端返回的内容不是图片")
    name = file_name(args.get("filename"), ext, "image")
    return Result("已生成图片。", [File(name, MIME[ext], data)])
