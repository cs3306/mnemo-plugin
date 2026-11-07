"""Word, PowerPoint and Excel."""
from __future__ import annotations

import datetime as dt
import re

from ..registry import Context, File, Result, ToolError, tool
from ..util import MIME, file_name, have, pandoc, read_output, workdir

MD_HELP = (
    "Markdown 语法：# / ## / ### 标题，- 无序列表，1. 有序列表，**粗体**，*斜体*，`代码`，``` 代码块，"
    "| 表格 |，行内公式 $a^2+b^2=c^2$，独立公式 $$\\int_0^1 x\\,dx$$。不支持插入图片。"
)


# ---------------------------------------------------------------------------------------------------- docx
def _cjk_fonts(path, font: str) -> None:
    """Pandoc's Word template has no East-Asian font, so Chinese falls back to whatever Word picks. Name one."""
    from docx import Document
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    doc = Document(str(path))

    def fix(rpr) -> None:
        rf = rpr.find(qn("w:rFonts"))
        if rf is None:
            rf = OxmlElement("w:rFonts")
            rpr.insert(0, rf)
        for a in ("w:eastAsiaTheme",):
            if rf.get(qn(a)) is not None:
                del rf.attrib[qn(a)]
        rf.set(qn("w:eastAsia"), font)

    styles = doc.styles.element
    defaults = styles.find(qn("w:docDefaults"))
    if defaults is not None:
        rpr = defaults.find(qn("w:rPrDefault") + "/" + qn("w:rPr"))
        if rpr is not None:
            fix(rpr)
    for rpr in styles.iter(qn("w:rPr")):
        fix(rpr)
    doc.save(str(path))


@tool(
    "docx_create",
    "生成 Word 文档（.docx）并发给用户。用 Markdown 写正文。" + MD_HELP + "用户要报告、作业、简历、信函、笔记等可编辑文档时使用。",
    {"type": "object", "properties": {
        "filename": {"type": "string", "description": "文件名，不含扩展名也可，如“周报”"},
        "markdown": {"type": "string", "description": "文档正文（Markdown）。标题请用 # 开头，不要在正文里重复写文件名"},
        "title": {"type": "string", "description": "可选：文档大标题（显示在最前面）"},
        "author": {"type": "string"},
        "toc": {"type": "boolean", "description": "是否生成目录，默认 false"},
    }, "required": ["filename", "markdown"]},
    available=lambda cfg: have("pandoc"),
)
def docx_create(args: dict, ctx: Context) -> Result:
    cfg = ctx.config
    name = file_name(args["filename"], ".docx", "document")
    with workdir() as d:
        out = d / "out.docx"
        extra = []
        for k in ("title", "author"):
            if args.get(k):
                extra += ["-M", f"{k}={args[k]}"]
        if args.get("toc"):
            extra += ["--toc"]
        pandoc(args["markdown"], out, "docx", cfg.tool_timeout, extra)
        if cfg.docx_cjk_font:
            _cjk_fonts(out, cfg.docx_cjk_font)
        data = read_output(out, cfg.max_file_mb)
    return Result(f"已生成 Word 文档《{name}》。", [File(name, MIME[".docx"], data)])


# ---------------------------------------------------------------------------------------------------- pptx
@tool(
    "pptx_create",
    "生成 PowerPoint 演示文稿（.pptx）并发给用户。用 Markdown 写：每个 `## 标题` 是一页幻灯片，其下的列表/表格/公式/文字是该页内容；"
    "`# 标题` 是章节过渡页；`::: notes` 与 `:::` 之间的内容是演讲者备注。每页只放 3–6 个要点。" + "不支持插入图片。",
    {"type": "object", "properties": {
        "filename": {"type": "string"},
        "markdown": {"type": "string", "description": "幻灯片内容（Markdown），用 ## 分页"},
        "title": {"type": "string", "description": "封面标题"},
        "subtitle": {"type": "string"},
        "author": {"type": "string"},
    }, "required": ["filename", "markdown"]},
    available=lambda cfg: have("pandoc"),
)
def pptx_create(args: dict, ctx: Context) -> Result:
    cfg = ctx.config
    name = file_name(args["filename"], ".pptx", "slides")
    with workdir() as d:
        out = d / "out.pptx"
        extra = ["--slide-level=2"]
        for k in ("title", "subtitle", "author"):
            if args.get(k):
                extra += ["-M", f"{k}={args[k]}"]
        pandoc(args["markdown"], out, "pptx", cfg.tool_timeout, extra)
        data = read_output(out, cfg.max_file_mb)
    return Result(f"已生成 PowerPoint《{name}》。", [File(name, MIME[".pptx"], data)])


# ---------------------------------------------------------------------------------------------------- xlsx
_BAD_SHEET = re.compile(r"[\[\]:*?/\\]")
MAX_CELLS = 200_000


def _sheet_title(name: str, used: set[str]) -> str:
    base = _BAD_SHEET.sub("_", (name or "Sheet").strip()).strip("'")[:31] or "Sheet"
    title, i = base, 2
    while title.lower() in used:
        suffix = f"_{i}"
        title, i = base[: 31 - len(suffix)] + suffix, i + 1
    used.add(title.lower())
    return title


def _width(v) -> int:
    s = "" if v is None else str(v)
    return sum(2 if ord(c) > 0x2E80 else 1 for c in s.split("\n")[0])


@tool(
    "xlsx_create",
    "生成 Excel 表格（.xlsx）并发给用户。适合账目、清单、成绩表、数据整理。每个工作表给出列名和行数据；"
    "单元格以 = 开头会作为公式（如 \"=SUM(B2:B10)\"，行号从 2 开始，因为第 1 行是表头）；数字请给数字类型而不是字符串。"
    "formats 可按列名设置显示格式，如 {\"金额\":\"#,##0.00\",\"日期\":\"yyyy-mm-dd\"}（日期列可直接给 \"2026-11-07\" 字符串）。",
    {"type": "object", "properties": {
        "filename": {"type": "string"},
        "sheets": {"type": "array", "minItems": 1, "maxItems": 20, "items": {"type": "object", "properties": {
            "name": {"type": "string", "description": "工作表名（最多 31 字符）"},
            "columns": {"type": "array", "items": {"type": "string"}, "description": "表头"},
            "rows": {"type": "array", "items": {"type": "array"}, "description": "每行一个数组，与 columns 对齐"},
            "formats": {"type": "object", "description": "列名 -> Excel 数字格式"},
        }, "required": ["columns", "rows"]}},
    }, "required": ["filename", "sheets"]},
)
def xlsx_create(args: dict, ctx: Context) -> Result:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    cfg = ctx.config
    name = file_name(args["filename"], ".xlsx", "table")
    wb = Workbook()
    wb.remove(wb.active)
    used: set[str] = set()
    total = 0
    for sh in args["sheets"]:
        cols = [str(c) for c in sh["columns"]]
        if not cols:
            raise ToolError("columns 不能为空")
        total += len(cols) * (len(sh["rows"]) + 1)
        if total > MAX_CELLS:
            raise ToolError(f"表格太大（超过 {MAX_CELLS} 个单元格）")
        ws = wb.create_sheet(_sheet_title(sh.get("name", ""), used))
        formats = {str(k): str(v) for k, v in (sh.get("formats") or {}).items()}
        date_cols = {i for i, c in enumerate(cols) if c in formats and any(t in formats[c].lower() for t in ("yy", "dd")) and "0" not in formats[c]}
        ws.append(cols)
        widths = [_width(c) for c in cols]
        for r, row in enumerate(sh["rows"], start=2):
            if not isinstance(row, list):
                raise ToolError(f"rows[{r - 2}] 必须是数组")
            if len(row) > len(cols):
                raise ToolError(f"rows[{r - 2}] 有 {len(row)} 个值，多于 {len(cols)} 列")
            vals = []
            for i, v in enumerate(row):
                if i in date_cols and isinstance(v, str):
                    try:
                        v = dt.date.fromisoformat(v.strip()[:10])
                    except ValueError:
                        pass
                elif isinstance(v, (dict, list)):
                    v = str(v)
                vals.append(v)
                widths[i] = max(widths[i], 10 if isinstance(v, dt.date) else (_width(v) if not (isinstance(v, str) and v.startswith("=")) else 10))
            ws.append(vals)
        for i, c in enumerate(cols):
            cell = ws.cell(row=1, column=i + 1)
            cell.font = Font(bold=True)
            cell.fill = PatternFill("solid", fgColor="E8EEF7")
            cell.alignment = Alignment(vertical="center")
            if c in formats:
                for cc in ws.iter_cols(min_col=i + 1, max_col=i + 1, min_row=2, max_row=len(sh["rows"]) + 1):
                    for x in cc:
                        x.number_format = formats[c]
            ws.column_dimensions[get_column_letter(i + 1)].width = min(max(widths[i] + 2, 8), 60)
        ws.freeze_panes = "A2"
        if sh["rows"]:
            ws.auto_filter.ref = ws.dimensions
    if not wb.sheetnames:
        raise ToolError("至少需要一个工作表")
    from io import BytesIO

    buf = BytesIO()
    wb.save(buf)
    data = buf.getvalue()
    if len(data) > cfg.max_file_mb << 20:
        raise ToolError(f"生成的文件超过 {cfg.max_file_mb} MB 上限")
    return Result(f"已生成 Excel《{name}》，{len(wb.sheetnames)} 个工作表。公式会在 Excel / WPS 打开时自动计算。", [File(name, MIME[".xlsx"], data)])
