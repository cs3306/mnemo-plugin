"""PDF from Markdown, through pandoc + XeLaTeX (so formulas and Chinese both work)."""
from __future__ import annotations

import functools
import subprocess

from ..config import Config
from ..registry import Context, File, Result, ToolError, tool
from ..util import MIME, file_name, has_cjk, have, pandoc, read_output, workdir

SERIF = ["Noto Serif CJK SC", "Source Han Serif SC", "Noto Sans CJK SC", "Source Han Sans SC", "WenQuanYi Zen Hei", "WenQuanYi Micro Hei", "AR PL UMing CN", "Droid Sans Fallback"]
MONO = ["Noto Sans Mono CJK SC", "Source Han Mono SC"]
OPENTYPE_CJK = ("Noto ", "Source Han")


@functools.lru_cache(maxsize=1)
def _installed_families() -> frozenset[str]:
    try:
        out = subprocess.run(["fc-list", ":", "family"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    fams = set()
    for line in out.splitlines():
        fams.update(x.strip() for x in line.split(","))
    return frozenset(fams)


def cjk_fonts(cfg: Config) -> tuple[str, str]:
    """(body font, monospace font) able to draw Chinese; "" where none is installed."""
    have_ = _installed_families()
    body = cfg.pdf_serif_font or next((f for f in SERIF if f in have_), "")
    mono = cfg.pdf_mono_font or next((f for f in MONO if f in have_), body)
    return body, mono


def tex_args(markdown: str, cfg: Config, wd) -> list[str]:
    """pandoc options that make XeLaTeX typeset the text; Chinese needs a font and line breaking between characters."""
    args = ["--pdf-engine=xelatex", "-V", "geometry:margin=2.5cm", "-V", "fontsize=11pt", "-V", "linestretch=1.25",
            "-V", "colorlinks=true", "-V", "linkcolor=black", "-V", "urlcolor=blue"]
    if has_cjk(markdown):
        body, mono = cjk_fonts(cfg)
        if not body:
            raise ToolError("服务器没有中文字体，无法生成含中文的 PDF（安装 fonts-noto-cjk，或设置 PDF_CJK_FONT）")
        args += ["-V", f"mainfont={body}", "-V", f"sansfont={body}", "-V", f"monofont={mono}"]
        if body.startswith(OPENTYPE_CJK):
            args += ["-V", "mainfontoptions=Language=Chinese Simplified"]
        # Without xeCJK/ctex, break lines between ideographs instead of overflowing the margin.
        (wd / "cjk.tex").write_text('\\XeTeXlinebreaklocale "zh"\n\\XeTeXlinebreakskip=0pt plus 1pt\n')
        args += ["-H", "cjk.tex"]
    return args


@tool(
    "pdf_create",
    "生成 PDF 文档并发给用户。用 Markdown 写正文，排版为学术/报告风格，支持中文、表格、代码块和 LaTeX 公式："
    "行内 $a^2+b^2=c^2$，独立 $$\\int_0^1 x\\,dx$$。不支持插入图片。需要精确控制版式（定理环境、宏包等）时改用 latex_create。",
    {"type": "object", "properties": {
        "filename": {"type": "string"},
        "markdown": {"type": "string", "description": "正文（Markdown）。标题用 # 开头"},
        "title": {"type": "string", "description": "可选：文档大标题"},
        "author": {"type": "string"},
        "date": {"type": "string", "description": "可选：日期文字，如 2026-11-07"},
        "toc": {"type": "boolean", "description": "是否生成目录，默认 false"},
        "number_sections": {"type": "boolean", "description": "是否给章节编号，默认 false"},
    }, "required": ["filename", "markdown"]},
    available=lambda cfg: have("pandoc", "xelatex"),
)
def pdf_create(args: dict, ctx: Context) -> Result:
    cfg = ctx.config
    name = file_name(args["filename"], ".pdf", "document")
    with workdir() as d:
        out = d / "out.pdf"
        extra = tex_args(args["markdown"], cfg, d)
        for k in ("title", "author", "date"):
            if args.get(k):
                extra += ["-M", f"{k}={args[k]}"]
        if args.get("toc"):
            extra += ["--toc"]
        if args.get("number_sections"):
            extra += ["--number-sections"]
        # TeX may only read and write inside the working directory, and never run commands.
        env = {"openin_any": "p", "openout_any": "p", "shell_escape": "f"}
        pandoc(args["markdown"], out, "pdf", cfg.tool_timeout, extra, env=env)
        data = read_output(out, cfg.max_file_mb)
    return Result(f"已生成 PDF《{name}》。", [File(name, MIME[".pdf"], data)])
