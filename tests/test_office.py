import io
import shutil

import pytest

pandoc = pytest.mark.skipif(shutil.which("pandoc") is None, reason="pandoc not installed")


@pandoc
def test_docx_chinese_math_table_and_east_asian_font(api):
    code, res = api.call("docx_create", {
        "filename": "周报/最终", "title": "周报", "toc": True,
        "markdown": "# 本周\n\n完成了**线代**作业。$a^2+b^2=c^2$\n\n| 科目 | 分数 |\n|---|---|\n| MATH | 95 |\n"})
    assert code == 200
    f = res["files"][0]
    assert f["name"] == "周报_最终.docx" and "wordprocessingml" in f["mime"]
    z = api.zip(res)
    doc = z.read("word/document.xml").decode()
    assert "线代" in doc and "<m:oMath" in doc and "<w:tbl>" in doc
    styles = z.read("word/styles.xml").decode()
    assert 'w:eastAsia="Microsoft YaHei"' in styles and "eastAsiaTheme" not in styles
    from docx import Document

    d = Document(io.BytesIO(api.file(res)))
    assert any(p.text == "本周" and p.style.name.startswith("Heading") for p in d.paragraphs)


@pandoc
def test_docx_does_not_embed_files_or_fetch_urls(api, tmp_path):
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"\x89PNG\r\n\x1a\nSECRET-BYTES")
    code, res = api.call("docx_create", {"filename": "x", "markdown": f"![a]({secret})\n\n![b](http://127.0.0.1:9/leak?q=1)\n\ntext"})
    assert code == 200
    z = api.zip(res)
    assert not [n for n in z.namelist() if "media" in n]
    assert b"SECRET-BYTES" not in b"".join(z.read(n) for n in z.namelist())


@pandoc
def test_docx_cjk_font_can_be_disabled(server):
    _, c = server(DOCX_CJK_FONT="")
    code, res = c.call("docx_create", {"filename": "x", "markdown": "你好"})
    assert code == 200 and "Microsoft YaHei" not in c.zip(res).read("word/styles.xml").decode()


@pandoc
def test_pptx_slides_notes_and_title(api):
    code, res = api.call("pptx_create", {
        "filename": "deck", "title": "演示", "author": "Ray",
        "markdown": "# 第一部分\n\n## 要点\n\n- 甲\n- 乙\n\n::: notes\n备注内容\n:::\n\n## 表格\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"})
    assert code == 200
    from pptx import Presentation

    p = Presentation(io.BytesIO(api.file(res)))
    slides = list(p.slides)
    assert len(slides) == 4  # title, section, 要点, 表格
    texts = [" ".join(sh.text_frame.text for sh in s.shapes if sh.has_text_frame) for s in slides]
    assert "演示" in texts[0] and "第一部分" in texts[1] and "甲" in texts[2]
    assert slides[2].notes_slide.notes_text_frame.text == "备注内容"
    assert any(sh.has_table for sh in slides[3].shapes)


def test_xlsx_formulas_dates_formats_and_sheet_names(api):
    code, res = api.call("xlsx_create", {"filename": "账本", "sheets": [
        {"name": "2026/11:月?", "columns": ["日期", "项目", "金额"], "formats": {"日期": "yyyy-mm-dd", "金额": "#,##0.00"},
         "rows": [["2026-11-01", "午饭", 35.5], ["2026-11-02", "咖啡", 22], ["", "合计", "=SUM(C2:C3)"]]},
        {"name": "2026/11:月?", "columns": ["a"], "rows": []}]})
    assert code == 200 and res["files"][0]["name"] == "账本.xlsx"
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(api.file(res)))
    assert wb.sheetnames == ["2026_11_月_", "2026_11_月__2"]
    ws = wb.worksheets[0]
    assert ws["A2"].value.date().isoformat() == "2026-11-01" and ws["A2"].number_format == "yyyy-mm-dd"
    assert ws["C2"].value == 35.5 and ws["C2"].number_format == "#,##0.00" and ws["C4"].value == "=SUM(C2:C3)"
    assert ws["A1"].font.bold and ws.freeze_panes == "A2" and ws.column_dimensions["B"].width >= 8


def test_xlsx_rejects_bad_input(api):
    assert api.call("xlsx_create", {"filename": "x", "sheets": []})[0] == 400
    code, res = api.call("xlsx_create", {"filename": "x", "sheets": [{"columns": ["a"], "rows": [[1, 2]]}]})
    assert code == 400 and "多于" in res["error"]
    code, res = api.call("xlsx_create", {"filename": "x", "sheets": [{"columns": ["a"], "rows": ["not a list"]}]})
    assert code == 400
    code, res = api.call("xlsx_create", {"filename": "x", "sheets": [{"columns": ["a", "b"], "rows": [[1, 2]] * 120000}]})
    assert code == 400 and "太大" in res["error"]


def test_file_names_are_safe(api):
    for asked, want in {"../../etc/passwd": ".._.._etc_passwd.xlsx", "a.xlsx": "a.xlsx", "": "table.xlsx", "x" * 200: "x" * 80 + ".xlsx", "报表.XLSX": "报表.xlsx"}.items():
        code, res = api.call("xlsx_create", {"filename": asked, "sheets": [{"columns": ["a"], "rows": [[1]]}]})
        assert code == 200
        got = res["files"][0]["name"]
        assert "/" not in got and got.endswith(".xlsx")
        if asked in ("a.xlsx", "", "x" * 200, "报表.XLSX"):
            assert got == want
