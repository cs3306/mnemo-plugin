import json
import sys
import types

import pytest

from mnemo_plugin.registry import ToolError
from mnemo_plugin.tools import devexec


def test_resolve_cwd_stays_inside_root():
    assert devexec.resolve_cwd("/workspace/ai", None) == "/workspace/ai"
    assert devexec.resolve_cwd("/workspace/ai", "proj/src") == "/workspace/ai/proj/src"
    assert devexec.resolve_cwd("/workspace/ai", "/workspace/ai/x") == "/workspace/ai/x"
    for bad in ("..", "../etc", "/etc", "/workspace/ai/../other", "/workspace/aix"):
        with pytest.raises(ToolError):
            devexec.resolve_cwd("/workspace/ai", bad)


def test_build_command_quotes_everything():
    cmd = devexec.build_command("/workspace/ai/a b", "echo 'hi'; ls", 30)
    assert cmd == "mkdir -p '/workspace/ai/a b' && cd '/workspace/ai/a b' && timeout -k 5 30 bash -c 'echo '\"'\"'hi'\"'\"'; ls' 2>&1"


def test_disabled_without_host(server):
    _, c = server()
    code, res = c.req("GET", "/healthz", token=None)
    assert code == 200 and "dev_exec" in res["disabled"] and "dev_exec" not in res["tools"]


@pytest.fixture
def enabled(server, tmp_path, monkeypatch):
    key = tmp_path / "key"
    key.write_text("x")
    monkeypatch.setitem(sys.modules, "paramiko", types.ModuleType("paramiko"))   # only presence matters here
    calls = []

    def fake_run(cfg, cwd, command, timeout):
        calls.append((cwd, command, timeout))
        return 3, b"line1\nline2\n", False

    monkeypatch.setattr(devexec, "_run_remote", fake_run)
    _, c = server(DEV_SSH_HOST="dev", DEV_SSH_KEY=str(key), DATA_DIR=str(tmp_path), DEV_SSH_MAX_TIMEOUT="100")
    return c, calls, tmp_path


def test_dev_exec_returns_exit_code_and_output_and_audits(enabled):
    c, calls, data = enabled
    code, res = c.call("dev_exec", {"command": "make test", "cwd": "proj", "timeout": 999})
    assert code == 200
    assert res["text"].startswith("退出码 3，目录 /workspace/ai/proj") and "line2" in res["text"]
    assert calls == [("/workspace/ai/proj", "make test", 100)]          # timeout clamped to DEV_SSH_MAX_TIMEOUT
    rec = json.loads((data / "dev_exec.log").read_text().splitlines()[-1])
    assert rec["command"] == "make test" and rec["exit"] == 3 and rec["chat"] == 7


def test_dev_exec_rejects_escape_and_empty(enabled):
    c, calls, _ = enabled
    assert c.call("dev_exec", {"command": "ls", "cwd": "../.."})[0] == 400
    assert c.call("dev_exec", {"command": "   "})[0] == 400
    assert calls == []


def _fake_paramiko(chunks, exit_code=0):
    m = types.ModuleType("paramiko")

    class SSHException(Exception):
        pass

    class Chan:
        def recv_exit_status(self):
            return exit_code

    class Out:
        channel = Chan()

        def __init__(self):
            self._c = list(chunks)

        def read(self, n):
            return self._c.pop(0) if self._c else b""

    class In:
        def close(self):
            pass

    class Client:
        def __init__(self):
            self.cmd = None

        def load_host_keys(self, p): pass
        def set_missing_host_key_policy(self, p): pass
        def connect(self, *a, **k): self.kw = k
        def exec_command(self, cmd, timeout=None):
            Client.last = cmd
            return In(), Out(), None
        def close(self): pass

    m.SSHClient, m.SSHException, m.MissingHostKeyPolicy = Client, SSHException, object
    m.BadHostKeyException = type("BadHostKeyException", (SSHException,), {})
    m.AuthenticationException = type("AuthenticationException", (SSHException,), {})
    return m, Client


def test_run_remote_truncates_but_reports_real_exit_code(monkeypatch, tmp_path):
    from mnemo_plugin.config import Config

    mod, client = _fake_paramiko([b"a" * 700, b"b" * 700, b"c" * 700], exit_code=5)
    monkeypatch.setitem(sys.modules, "paramiko", mod)
    cfg = Config.from_env({"DEV_SSH_HOST": "dev", "DATA_DIR": str(tmp_path), "DEV_SSH_MAX_OUTPUT_KB": "1"})
    code, out, truncated = devexec._run_remote(cfg, "/workspace/ai", "yes", 10)
    assert (code, len(out), truncated) == (5, 1024, True)
    assert client.last.startswith("mkdir -p /workspace/ai && cd /workspace/ai && timeout -k 5 10 bash -c yes")


def test_run_remote_small_output_not_truncated(monkeypatch, tmp_path):
    from mnemo_plugin.config import Config

    mod, _ = _fake_paramiko([b"hello\n"])
    monkeypatch.setitem(sys.modules, "paramiko", mod)
    cfg = Config.from_env({"DEV_SSH_HOST": "dev", "DATA_DIR": str(tmp_path)})
    assert devexec._run_remote(cfg, "/w", "echo hello", 5) == (0, b"hello\n", False)


# ---------------------------------------------------------------------------------------------- files sent to the bot

def test_safe_filename():
    assert devexec.safe_filename("../../etc/pass wd.zip") == "pass_wd.zip"
    assert devexec.safe_filename("C:\\Users\\a\\报告 终稿.pdf") == "报告_终稿.pdf"
    assert devexec.safe_filename(".bashrc") == "bashrc"
    assert devexec.safe_filename("///") == "file"
    assert len(devexec.safe_filename("a" * 300 + ".tar.gz").encode()) <= 120
    assert devexec.safe_filename("a" * 300 + ".tar.gz").endswith(".tar.gz")


def test_upload_command_never_overwrites(tmp_path):
    import subprocess

    inbox = tmp_path / "in"
    cmd = devexec.build_upload_command(str(inbox), "a b'; rm -rf x.tar.gz".replace(" ", "_"))
    names = []
    for i in range(3):
        r = subprocess.run(["bash", "-c", cmd], input=f"d{i}".encode(), capture_output=True)
        assert r.returncode == 0, r.stderr
        names.append(r.stdout.decode())
    assert len(set(names)) == 3 and names[1].endswith(".tar.gz") and "-1" in names[1]
    assert sorted(p.read_text() for p in inbox.iterdir()) == ["d0", "d1", "d2"]      # no temp files left behind


@pytest.fixture
def inbox_server(server, tmp_path, monkeypatch):
    key = tmp_path / "key"
    key.write_text("x")
    monkeypatch.setitem(sys.modules, "paramiko", types.ModuleType("paramiko"))
    saved = []

    def fake_upload(cfg, inbox, name, data):
        saved.append((inbox, name, data))
        return name

    monkeypatch.setattr(devexec, "_upload_remote", fake_upload)
    _, c = server(DEV_SSH_HOST="dev", DEV_SSH_KEY=str(key), DATA_DIR=str(tmp_path), DEV_UPLOAD_MAX_MB="1")
    return c, saved, tmp_path


def _post_file(c, name, data, **kw):
    import base64

    return c.req("POST", "/v1/files", {"name": name, "mime": "text/plain", "data": base64.b64encode(data).decode(), "context": {"chat": 7, "channel": "telegram"}}, **kw)


def test_file_upload_saves_into_inbox_and_audits(inbox_server):
    c, saved, data = inbox_server
    code, res = _post_file(c, "../x y.txt", b"hello")
    assert code == 200 and res["text"].startswith("已保存到开发容器：/workspace/ai/inbox/x_y.txt")
    assert saved == [("/workspace/ai/inbox", "x_y.txt", b"hello")]
    rec = json.loads((data / "dev_exec.log").read_text().splitlines()[-1])
    assert rec["command"].startswith("[upload] x_y.txt") and rec["exit"] == 0


def test_file_upload_auth_size_and_bad_body(inbox_server):
    c, saved, _ = inbox_server
    assert _post_file(c, "a.txt", b"x", token=None)[0] == 401
    assert _post_file(c, "a.bin", b"\0" * (2 << 20))[0] == 413            # limit is 1 MB here
    assert c.req("POST", "/v1/files", raw=b"{}")[0] == 400
    assert c.req("POST", "/v1/files", {"name": "a", "data": "!!!not base64"})[0] == 400
    assert saved == []


def test_file_upload_404_when_dev_container_not_configured(server):
    _, c = server()
    assert _post_file(c, "a.txt", b"x")[0] == 404
