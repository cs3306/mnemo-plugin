"""Compile a complete LaTeX document."""
from __future__ import annotations

import re

from ..registry import Context, File, Result, ToolError, tool
from ..util import MIME, file_name, have, read_output, run, trim, workdir

ENGINES = ("xelatex", "lualatex", "pdflatex")


def _log_excerpt(log: str) -> str:
    """The lines a person would look at in a .log: errors ('! ...') with the context that follows them."""
    lines = log.splitlines()
    picks: list[str] = []
    for i, ln in enumerate(lines):
        if ln.startswith("!") or re.match(r"^\S+\.tex:\d+:", ln):
            picks.extend(lines[i:i + 6])
            picks.append("…")
            if len(picks) > 40:
                break
    return trim("\n".join(picks) if picks else "\n".join(lines[-25:]), 1800)


@tool(
    "latex_create",
    "编译一份完整的 LaTeX 文档（含 \\documentclass 和 \\begin{document}）并把 PDF 与 .tex 源文件发给用户。"
    "默认用 xelatex，中文请加 \\usepackage{ctex}，没有 ctex 时用 \\usepackage{fontspec}\\setmainfont{Noto Serif CJK SC}。"
    "编译失败会返回日志里的错误，请据此修改后重试。不能执行外部命令，也不能读取工作目录以外的文件。",
    {"type": "object", "properties": {
        "filename": {"type": "string", "description": "文件名（不含扩展名）"},
        "source": {"type": "string", "description": "完整的 .tex 源码"},
        "engine": {"type": "string", "enum": list(ENGINES), "description": "默认 xelatex"},
        "compile": {"type": "boolean", "description": "false 时只发 .tex 源文件，默认 true"},
    }, "required": ["filename", "source"]},
)
def latex_create(args: dict, ctx: Context) -> Result:
    cfg = ctx.config
    tex_name = file_name(args["filename"], ".tex", "document")
    pdf_name = tex_name[:-4] + ".pdf"
    source = args["source"]
    tex = File(tex_name, MIME[".tex"], source.encode())
    if args.get("compile", True) is False:
        return Result(f"已生成源文件 {tex_name}（未编译）。", [tex])
    engine = args.get("engine", "xelatex")
    if (why := have(engine)) is not None:
        return Result(f"服务器没有 {engine}（{why}），只发送了 .tex 源文件，请用户在本地编译。", [tex])
    with workdir() as d:
        (d / "main.tex").write_text(source)
        # Paranoid file access: only the working directory; no shell escape.
        env = {"openin_any": "p", "openout_any": "p", "shell_escape": "f"}
        cmd = [engine, "-no-shell-escape", "-interaction=nonstopmode", "-halt-on-error", "-file-line-error", "main.tex"]
        passes = 0
        while passes < 3:  # references and the table of contents need a second (sometimes third) run
            passes += 1
            r = run(cmd, d, cfg.tool_timeout, env=env)
            log = (d / "main.log").read_text(errors="replace") if (d / "main.log").exists() else r.stdout
            if r.returncode != 0:
                raise ToolError("LaTeX 编译失败：\n" + _log_excerpt(log))
            if not re.search(r"Rerun to get|Label\(s\) may have changed|undefined references", log):
                break
        if not (d / "main.pdf").exists():
            log = (d / "main.log").read_text(errors="replace") if (d / "main.log").exists() else ""
            raise ToolError("没有生成 PDF（文档可能没有任何输出内容）：\n" + _log_excerpt(log))
        pdf = File(pdf_name, MIME[".pdf"], read_output(d / "main.pdf", cfg.max_file_mb))
    return Result(f"已编译 {pdf_name}（{engine}，{passes} 遍）。", [pdf, tex])
