import json

from mnemo_plugin.registry import Result, ToolError, tool, validate
import pytest

from conftest import Client


@tool("t_echo", "echo", {"type": "object", "properties": {"text": {"type": "string"}, "n": {"type": "integer"}}, "required": ["text"]})
def _echo(args, ctx):
    return Result(text=f"{args['text']}|{ctx.chat}|{ctx.channel}")


@tool("t_fail", "always fails")
def _fail(args, ctx):
    raise ToolError("没法做")


@tool("t_crash", "crashes")
def _crash(args, ctx):
    raise RuntimeError("secret internals /etc/shadow")


@tool("t_off", "unavailable", available=lambda cfg: "needs a gadget")
def _off(args, ctx):
    return Result(text="never")


def test_list_requires_token_and_hides_unavailable(server):
    srv, c = server()
    assert c.req("GET", "/v1/tools", token=None)[0] == 401
    assert c.req("GET", "/v1/tools", token="wrong")[0] == 401
    code, body = c.req("GET", "/v1/tools")
    names = [t["name"] for t in body["tools"]]
    assert code == 200 and "t_echo" in names and "t_off" not in names
    assert all({"name", "description", "parameters"} <= set(t) for t in body["tools"])
    # health is open and says what is off, and why
    code, h = c.req("GET", "/healthz", token=None)
    assert code == 200 and h["disabled"]["t_off"] == "needs a gadget"


def test_call_passes_arguments_and_context(server):
    _, c = server()
    code, res = c.call("t_echo", {"text": "hi"})
    assert code == 200 and res == {"text": "hi|7|telegram", "files": []}


def test_errors_are_json_and_do_not_leak(server):
    _, c = server()
    assert c.call("t_echo", {"text": "x"}, token=None)[0] == 401
    assert c.call("t_nope", {})[0] == 404
    assert c.call("t_off", {})[0] == 404
    code, res = c.call("t_fail", {})
    assert (code, res["error"]) == (400, "没法做")
    code, res = c.call("t_crash", {})
    assert code == 500 and "shadow" not in json.dumps(res)
    code, res = c.call("t_echo", {})
    assert code == 400 and "text" in res["error"]
    code, res = c.call("t_echo", {"text": 5})
    assert code == 400 and "string" in res["error"]
    code, res = c.req("POST", "/v1/tools/t_echo", raw=b"not json")
    assert code == 400
    code, res = c.req("POST", "/v1/tools/t_echo", raw=b'{"arguments": [1]}')
    assert code == 400
    assert c.req("GET", "/v1/nothing")[0] == 404


def test_request_size_limit(server):
    _, c = server(MAX_REQUEST_MB="1")
    big = {"arguments": {"text": "x" * (2 << 20)}}
    code, res = c.req("POST", "/v1/tools/t_echo", big)
    assert code == 413


def test_server_refuses_to_run_without_token():
    from mnemo_plugin.config import Config

    assert Config.from_env({}).validate()
    assert not Config.from_env({"PLUGIN_TOKEN": "x"}).validate()
    assert not Config.from_env({"PLUGIN_ALLOW_ANONYMOUS": "true"}).validate()


def test_anonymous_mode_allows_calls(server):
    from conftest import make_cfg  # noqa: F401

    srv, c = server(PLUGIN_TOKEN="", PLUGIN_ALLOW_ANONYMOUS="true")
    assert Client(srv, token=None).call("t_echo", {"text": "a"})[0] == 200


def test_validate_schema_subset():
    s = {"type": "object", "properties": {"a": {"type": "array", "items": {"type": "integer"}, "minItems": 1, "maxItems": 2}, "m": {"enum": ["x", "y"]}}}
    validate({"a": [1], "m": "x"}, s)
    for bad in ({"a": []}, {"a": [1, 2, 3]}, {"a": ["s"]}, {"m": "z"}, {"a": [True]}):
        with pytest.raises(ToolError):
            validate(bad, s)


def test_extra_tools_dir(tmp_path):
    from mnemo_plugin.loader import load_extra
    from mnemo_plugin.registry import all_tools

    (tmp_path / "hello.py").write_text(
        "from mnemo_plugin import tool, Result\n@tool('t_extra', 'x')\ndef f(a, c):\n    return Result(text='hi')\n")
    (tmp_path / "broken.py").write_text("raise SystemError('boom')\n")
    (tmp_path / "_skip.py").write_text("raise SystemError('must not be imported')\n")
    errs = load_extra(str(tmp_path))
    assert "t_extra" in all_tools()
    assert len(errs) == 1 and "broken.py" in errs[0]
    assert load_extra(str(tmp_path / "missing"))


def test_example_extra_tool_works(server):
    import pathlib

    from mnemo_plugin.loader import load_extra

    here = pathlib.Path(__file__).resolve().parent.parent / "examples" / "extra_tools"
    assert load_extra(str(here)) == []
    _, c = server()
    code, res = c.call("make_ics", {"title": "交作业, 线代;", "date": "2026-11-13", "time": "09:30", "minutes": 90})
    assert code == 200 and res["files"][0]["name"] == "event.ics"
    ics = c.file(res).decode()
    assert "DTSTART:20261113T093000" in ics and "DTEND:20261113T110000" in ics and "交作业\\, 线代\;" in ics and ics.count("\r\n") >= 9
    code, res = c.call("make_ics", {"title": "x", "date": "13/11/2026"})
    assert code == 400
    assert c.call("make_ics", {"title": "全天", "date": "2026-11-13"})[0] == 200
