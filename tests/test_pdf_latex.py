import shutil
import subprocess

import pytest

tex = pytest.mark.skipif(shutil.which("xelatex") is None or shutil.which("pandoc") is None, reason="TeX / pandoc not installed")


def pdftext(data: bytes) -> str:
    if shutil.which("pdftotext") is None:
        pytest.skip("pdftotext not installed")
    return subprocess.run(["pdftotext", "-", "-"], input=data, capture_output=True).stdout.decode()


@tex
def test_pdf_chinese_math_and_long_lines(api):
    para = "这是一个很长的中文段落，用来测试换行，" * 12
    code, res = api.call("pdf_create", {"filename": "笔记", "title": "线代笔记", "toc": True, "number_sections": True,
                                         "markdown": f"# 特征值\n\n{para}\n\n$$\\int_0^1 x\\,dx=\\frac12$$\n\n- 甲\n- 乙\n"})
    assert code == 200 and res["files"][0]["name"] == "笔记.pdf"
    data = api.file(res)
    assert data.startswith(b"%PDF")
    text = pdftext(data)
    assert "特征值" in text and "线代笔记" in text and "甲" in text
    # no line may run off the page: with CJK line breaking each extracted line stays short
    assert max(len(l) for l in text.splitlines()) < 60


@tex
def test_pdf_ignores_raw_tex_and_yaml_injection(api, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET-CONTENT")
    md = f"---\nheader-includes: '\\input{{{secret}}}'\n---\n\n\\input{{{secret}}}\n\n```{{=latex}}\n\\input{{{secret}}}\n```\n\nbody text\n"
    code, res = api.call("pdf_create", {"filename": "x", "markdown": md})
    assert code == 200
    assert "TOP-SECRET-CONTENT" not in pdftext(api.file(res))


@tex
def test_pdf_without_cjk_font_says_so(server, monkeypatch):
    from mnemo_plugin.tools import pdf

    monkeypatch.setattr(pdf, "_installed_families", lambda: frozenset())
    _, c = server()
    code, res = c.call("pdf_create", {"filename": "x", "markdown": "你好"})
    assert code == 400 and "中文字体" in res["error"]
    assert c.call("pdf_create", {"filename": "x", "markdown": "hello"})[0] == 200  # Latin text needs none


@tex
def test_latex_compiles_and_returns_pdf_and_source(api):
    src = "\\documentclass{article}\\usepackage{fontspec}\\setmainfont{Noto Serif CJK SC}\n\\XeTeXlinebreaklocale \"zh\"\n\\begin{document}\n你好 \\ref{x} $E=mc^2$\n\\section{甲}\\label{x}\n\\end{document}"
    code, res = api.call("latex_create", {"filename": "t", "source": src})
    assert code == 200
    assert [f["name"] for f in res["files"]] == ["t.pdf", "t.tex"]
    text = pdftext(api.file(res, 0))
    assert "你好" in text and "1" in text  # the \ref resolved on the second pass
    assert api.file(res, 1).decode() == src


@tex
def test_latex_error_returns_log_lines(api):
    code, res = api.call("latex_create", {"filename": "t", "source": "\\documentclass{article}\\begin{document}\\nosuchcommand\\end{document}"})
    assert code == 400 and "Undefined control sequence" in res["error"] and len(res["error"]) < 2500


@tex
def test_latex_cannot_read_outside_or_run_commands(api, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET-CONTENT")
    marker = tmp_path / "pwned"
    src = f"\\documentclass{{article}}\\begin{{document}}\\immediate\\write18{{touch {marker}}}\\input{{{secret}}}\\end{{document}}"
    code, res = api.call("latex_create", {"filename": "t", "source": src})
    assert code == 400
    assert not marker.exists()


def test_latex_without_a_tex_install_still_delivers_source(api, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    code, res = api.call("latex_create", {"filename": "t", "source": "\\documentclass{article}"})
    assert code == 200 and [f["name"] for f in res["files"]] == ["t.tex"] and "没有" in res["text"]


def test_latex_compile_false_returns_only_source(api):
    code, res = api.call("latex_create", {"filename": "t", "source": "x", "compile": False})
    assert code == 200 and [f["name"] for f in res["files"]] == ["t.tex"]


def test_pdf_tool_disappears_when_tex_is_missing(monkeypatch):
    from mnemo_plugin.registry import active_tools
    from conftest import make_cfg

    monkeypatch.setenv("PATH", "/nonexistent")
    ok, off = active_tools(make_cfg())
    assert "pdf_create" in off and "docx_create" in off and "latex_create" in ok and "xlsx_create" in ok
