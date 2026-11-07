from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .registry import ToolError

MIME = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pdf": "application/pdf",
    ".tex": "application/x-tex",
    ".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif",
}

_BAD = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]')


def file_name(requested: str | None, ext: str, default: str) -> str:
    """A safe download name that always ends in ext ('.docx'), whatever the model asked for."""
    base = _BAD.sub("_", (requested or "").strip()).strip(" .")
    if base.lower().endswith(ext):
        base = base[: -len(ext)]
    base = base.strip(" .") or default
    return base[:80] + ext


@contextmanager
def workdir():
    d = tempfile.mkdtemp(prefix="mnemo-plugin-")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def have(*programs: str) -> str | None:
    """None if every program is on PATH, else a message naming the first missing one."""
    for p in programs:
        if shutil.which(p) is None:
            return f"{p} 未安装"
    return None


def run(cmd: list[str], cwd: Path, timeout: int, env: dict | None = None, stdin: str | None = None) -> subprocess.CompletedProcess:
    full_env = {"PATH": os.environ.get("PATH", ""), "HOME": str(cwd), "LANG": "C.UTF-8", "TMPDIR": str(cwd)}
    if env:
        full_env.update(env)
    try:
        return subprocess.run(cmd, cwd=cwd, env=full_env, input=stdin, capture_output=True, text=True, timeout=timeout, errors="replace")
    except subprocess.TimeoutExpired:
        raise ToolError(f"{cmd[0]} 超时（{timeout} 秒）") from None
    except FileNotFoundError:
        raise ToolError(f"{cmd[0]} 未安装") from None


def read_output(path: Path, max_mb: int) -> bytes:
    if not path.exists():
        raise ToolError("没有生成文件")
    if path.stat().st_size > max_mb << 20:
        raise ToolError(f"生成的文件超过 {max_mb} MB 上限")
    return path.read_bytes()


def has_cjk(s: str) -> bool:
    return any("㐀" <= ch <= "鿿" or "　" <= ch <= "〿" or "＀" <= ch <= "￯" for ch in s)


# ---------------------------------------------------------------------------------------------- pandoc

# Markdown as the model writes it: no YAML header or raw TeX/HTML passthrough. Those can inject LaTeX
# (\input, header-includes) or HTML into the converter, and a document request needs none of them.
MD = "markdown-yaml_metadata_block-pandoc_title_block-raw_tex-raw_html-raw_attribute-implicit_figures"

# Images are dropped: pandoc would otherwise read local files or fetch URLs named in the text, which is both an
# exfiltration channel (a URL carrying data) and a way to reach internal services.
DROP_IMAGES = "function Image(el) return el.caption end\n"


def trim(s: str, n: int = 1500) -> str:
    s = s.strip()
    return s if len(s) <= n else s[:n] + "…"


_sandbox: bool | None = None


def _sandbox_supported(cwd: Path) -> bool:
    """--sandbox needs pandoc to carry its templates inside the binary; some distro builds read them from disk instead."""
    global _sandbox
    if _sandbox is None:
        r = run(["pandoc", "--sandbox", "-t", "docx", "-o", "probe.docx"], cwd, 30, stdin="x")
        _sandbox = r.returncode == 0
    return _sandbox


def pandoc(markdown: str, out: Path, to: str, timeout: int, args: list[str] | None = None, env: dict | None = None) -> None:
    """Convert Markdown (read from stdin) into out."""
    (out.parent / "drop-images.lua").write_text(DROP_IMAGES)
    cmd = ["pandoc", "-f", MD, "-t", to, "--lua-filter=drop-images.lua", "-o", out.name, *(args or [])]
    if _sandbox_supported(out.parent):
        cmd.insert(1, "--sandbox")
    r = run(cmd, out.parent, timeout, env=env, stdin=markdown)
    if r.returncode != 0:
        raise ToolError("pandoc 转换失败：" + trim(r.stderr or r.stdout))
