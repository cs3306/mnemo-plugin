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
