import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo

import pytest

from mnemo_plugin.registry import Context, ToolError, active_tools
from mnemo_plugin.scheduler import PermanentError, ScheduleError, ScheduleStore, Scheduler, TransientError, http_trigger, parse_time
from conftest import make_cfg

VAN = ZoneInfo("America/Vancouver")


def utc(y, mo, d, h, mi, tz=VAN):
    return datetime(y, mo, d, h, mi, tzinfo=tz).astimezone(timezone.utc)


def add(store, **kw):
    base = dict(label="晨报", prompt="给我晨报", at="07:30", days=None, on=None, tz="America/Vancouver", chat=42)
    base.update(kw)
    return store.add(**base)


@pytest.fixture
def store(tmp_path):
    return ScheduleStore(tmp_path / "schedules.json")


def sched(store, now, fire, **env):
    clock = {"t": now}
    s = Scheduler(store, make_cfg(**env), trigger=fire, now=lambda: clock["t"])
    return s, clock


def test_parse_time():
    assert parse_time("7:30") == "07:30" and parse_time(" 19:05 ") == "19:05" and parse_time("7：30") == "07:30"
    for bad in ["24:00", "7:60", "7", "abc", ""]:
        with pytest.raises(ScheduleError):
            parse_time(bad)


def test_persistence_roundtrip_and_atomic_file(store, tmp_path):
    t = add(store)
    again = ScheduleStore(tmp_path / "schedules.json")
    assert again.list()[0]["id"] == t["id"] and again.list()[0]["prompt"] == "给我晨报"
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".schedules-")]


def test_corrupt_file_is_moved_aside(tmp_path):
    p = tmp_path / "schedules.json"
    p.write_text("{not json")
    s = ScheduleStore(p)
    assert s.list() == [] and (tmp_path / "schedules.corrupt").exists()


def test_validation(store):
    with pytest.raises(ScheduleError):
        add(store, tz="Mars/Base")
    with pytest.raises(ScheduleError):
        add(store, days=["mon"], on="2099-01-01")
    with pytest.raises(ScheduleError):
        add(store, days=["xyz"])
    with pytest.raises(ScheduleError):
        add(store, on="2001-01-01")
    with pytest.raises(ScheduleError):
        add(store, on="nope")
    with pytest.raises(ScheduleError):
        add(store, label="  ")
    add(store)
    with pytest.raises(ScheduleError, match="已经有名为"):
        add(store)
    assert add(store, label="x", days=list("mon tue wed thu fri sat sun".split()))["days"] is None


def test_task_limit(store):
    for i in range(20):
        add(store, label=f"t{i}")
    with pytest.raises(ScheduleError, match="上限"):
        add(store, label="one more")


def test_remove_by_id_label_and_ambiguity(store):
    a = add(store, label="晨报")
    add(store, label="晨读")
    with pytest.raises(ScheduleError, match="多个"):
        store.remove("晨")
    assert store.remove("晨报")[0]["id"] == a["id"]
    assert [t["label"] for t in store.list()] == ["晨读"]
    store.remove(store.list()[0]["id"])
    with pytest.raises(ScheduleError, match="没有找到"):
        store.remove("晨读")


def test_fires_once_per_day_at_local_time(store):
    add(store)
    fired = []
    s, clock = sched(store, utc(2026, 11, 7, 7, 29), fired.append)
    s.tick()
    assert fired == []
    clock["t"] = utc(2026, 11, 7, 7, 30)
    s.tick(); s.tick()
    assert len(fired) == 1 and fired[0]["prompt"] == "给我晨报"
    clock["t"] = utc(2026, 11, 7, 12, 0)
    s.tick()
    assert len(fired) == 1
    clock["t"] = utc(2026, 11, 8, 7, 31)
    s.tick()
    assert len(fired) == 2


def test_survives_restart_without_double_fire(store, tmp_path):
    add(store)
    fired = []
    s, _ = sched(store, utc(2026, 11, 7, 7, 31), fired.append)
    s.tick()
    store2 = ScheduleStore(tmp_path / "schedules.json")
    s2, _ = sched(store2, utc(2026, 11, 7, 7, 35), fired.append)
    s2.tick()
    assert len(fired) == 1


def test_late_start_within_grace_fires_but_beyond_is_skipped(store):
    add(store)
    fired = []
    s, _ = sched(store, utc(2026, 11, 7, 8, 45), fired.append)   # 75 min late
    s.tick()
    assert len(fired) == 1
    store2 = ScheduleStore(store.path.parent / "other.json")
    add(store2)
    s2, _ = sched(store2, utc(2026, 11, 7, 15, 0), fired.append)  # 7.5 h late
    s2.tick()
    assert len(fired) == 1


def test_weekday_filter(store):
    add(store, days=["mon", "tue", "wed", "thu", "fri"])
    fired = []
    s, clock = sched(store, utc(2026, 11, 7, 7, 40), fired.append)    # Saturday
    s.tick()
    assert fired == []
    clock["t"] = utc(2026, 11, 9, 7, 40)                                # Monday
    s.tick()
    assert len(fired) == 1


def test_dst_follows_local_wall_clock(store):
    add(store)
    fired = []
    s, clock = sched(store, utc(2026, 3, 7, 7, 30), fired.append)      # PST
    s.tick()
    clock["t"] = utc(2026, 3, 9, 7, 30)                                  # after the spring-forward: PDT
    s.tick()
    assert len(fired) == 2
    clock["t"] = utc(2026, 3, 10, 7, 29)
    s.tick()
    assert len(fired) == 2


def test_one_off_is_deleted_after_firing(store):
    add(store, label="once", on="2099-01-05")
    fired = []
    s, clock = sched(store, utc(2099, 1, 5, 7, 31), fired.append)
    s.tick()
    assert len(fired) == 1 and store.list() == []


def test_transient_failure_retries_with_backoff_then_succeeds(store):
    add(store)
    calls = []

    def fire(t):
        calls.append(1)
        if len(calls) < 3:
            raise TransientError("down")

    s, clock = sched(store, utc(2026, 11, 7, 7, 30), fire)
    s.tick()
    assert len(calls) == 1 and "down" in store.list()[0]["last_error"]
    s.tick()
    assert len(calls) == 1                       # backoff not elapsed
    clock["t"] += timedelta(minutes=1)
    s.tick()
    assert len(calls) == 2
    clock["t"] += timedelta(minutes=5)
    s.tick()
    assert len(calls) == 3 and store.list()[0]["last_fired"] == "2026-11-07" and store.list()[0]["last_error"] is None
    clock["t"] += timedelta(minutes=30)
    s.tick()
    assert len(calls) == 3


def test_permanent_failure_is_not_retried_and_recorded(store):
    add(store)
    calls = []

    def fire(t):
        calls.append(1)
        raise PermanentError("401")

    s, clock = sched(store, utc(2026, 11, 7, 7, 30), fire)
    s.tick()
    clock["t"] += timedelta(minutes=10)
    s.tick()
    assert len(calls) == 1 and store.list()[0]["last_error"] == "401"
    clock["t"] = utc(2026, 11, 8, 7, 30)
    s.tick()
    assert len(calls) == 2                       # tomorrow it tries again


def test_a_failing_task_does_not_block_others(store):
    add(store, label="bad", prompt="bad")
    add(store, label="good", prompt="good")
    got = []

    def fire(t):
        if t["label"] == "bad":
            raise TransientError("x")
        got.append(t["label"])

    s, _ = sched(store, utc(2026, 11, 7, 7, 30), fire)
    s.tick()
    assert got == ["good"]


class _Mnemo(BaseHTTPRequestHandler):
    status = 202
    seen: list = []

    def do_POST(self):
        n = int(self.headers["Content-Length"])
        _Mnemo.seen.append((self.path, self.headers.get("Authorization"), json.loads(self.rfile.read(n))))
        self.send_response(_Mnemo.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"accepted"}')

    def log_message(self, *a):
        pass


@pytest.fixture
def mnemo():
    _Mnemo.seen, _Mnemo.status = [], 202
    srv = HTTPServer(("127.0.0.1", 0), _Mnemo)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown(); srv.server_close()


def test_http_trigger_request_shape_and_error_classes(mnemo):
    cfg = make_cfg(MNEMO_URL=mnemo, MNEMO_API_KEY="k")
    fire = http_trigger(cfg)
    fire({"prompt": "p", "label": "L", "chat": 42})
    path, auth, body = _Mnemo.seen[0]
    assert path == "/v1/trigger" and auth == "Bearer k" and body == {"prompt": "p", "label": "L", "chat": 42}
    fire({"prompt": "p", "label": "L", "chat": None})
    assert "chat" not in _Mnemo.seen[1][2]
    _Mnemo.status = 401
    with pytest.raises(PermanentError):
        fire({"prompt": "p", "label": "L"})
    _Mnemo.status = 503
    with pytest.raises(TransientError):
        fire({"prompt": "p", "label": "L"})
    with pytest.raises(TransientError):
        http_trigger(make_cfg(MNEMO_URL="http://127.0.0.1:1", MNEMO_API_KEY="k"))({"prompt": "p", "label": "L"})


# ------------------------------------------------------------------------------------------------ the tools

def tool_ctx(tmp_path, **env):
    cfg = make_cfg(MNEMO_URL="http://x", MNEMO_API_KEY="k", DATA_DIR=str(tmp_path), **env)
    return Context(config=cfg, chat=42, channel="telegram", tz="America/Vancouver")


def test_tools_are_disabled_without_mnemo_settings():
    ok, off = active_tools(make_cfg())
    assert "schedule_add" in off and "schedule_add" not in ok
    ok, off = active_tools(make_cfg(MNEMO_URL="http://x", MNEMO_API_KEY="k", DATA_DIR="/tmp"))
    assert {"schedule_add", "schedule_list", "schedule_remove"} <= set(ok)


def test_tools_end_to_end(tmp_path):
    from mnemo_plugin.registry import all_tools

    ctx = tool_ctx(tmp_path)
    T = all_tools()
    r = T["schedule_add"].func({"label": "晨报", "prompt": "搜索新闻写晨报", "time": "7:30"}, ctx)
    assert "晨报" in r.text and "07:30" in r.text and "America/Vancouver" in r.text
    assert "搜索新闻写晨报" in T["schedule_list"].func({}, ctx).text
    with pytest.raises(ToolError):
        T["schedule_add"].func({"label": "晨报", "prompt": "x", "time": "7:30"}, ctx)
    assert "已删除" in T["schedule_remove"].func({"target": "晨报"}, ctx).text
    assert "没有待执行" in T["schedule_list"].func({}, ctx).text
    with pytest.raises(ToolError):
        T["schedule_remove"].func({"target": "晨报"}, ctx)


def test_negative_api_chat_is_not_stored(tmp_path):
    from mnemo_plugin.registry import all_tools

    ctx = tool_ctx(tmp_path)
    ctx.chat, ctx.channel = -123456, "api"
    all_tools()["schedule_add"].func({"label": "a", "prompt": "b", "time": "08:00"}, ctx)
    from mnemo_plugin.scheduler import get_store
    assert get_store(ctx.config).list()[0]["chat"] is None


def test_server_passes_tz_to_tools(server, tmp_path):
    srv, c = server(MNEMO_URL="http://x", MNEMO_API_KEY="k", DATA_DIR=str(tmp_path))
    st, res = c.req("POST", "/v1/tools/schedule_add", {"arguments": {"label": "L", "prompt": "P", "time": "09:00"},
                                                       "context": {"chat": 5, "channel": "telegram", "tz": "Asia/Shanghai"}})
    assert st == 200 and "Asia/Shanghai" in res["text"]
    st, res = c.req("POST", "/v1/tools/schedule_add", {"arguments": {"label": "L2", "prompt": "P", "time": "9", "days": ["funday"]}})
    assert st == 400


def test_history_survives_one_off_deletion_and_restart(store, tmp_path):
    add(store, label="once", on="2099-01-05")
    s, _ = sched(store, utc(2099, 1, 5, 7, 31), lambda t: None)
    s.tick()
    assert store.list() == [] and store.history[-1]["ok"] and store.history[-1]["label"] == "once"
    again = ScheduleStore(tmp_path / "schedules.json")
    assert again.history[-1]["label"] == "once"


def test_history_records_failures_and_is_capped(store):
    add(store)
    s, clock = sched(store, utc(2026, 11, 7, 7, 30), lambda t: (_ for _ in ()).throw(PermanentError("401")))
    s.tick()
    assert store.history[-1]["ok"] is False and "401" in store.history[-1]["detail"]
    for i in range(30):
        store.record({"label": "x", "id": "i", "tz": "UTC"}, True)
    assert len(store.history) == 10


def test_list_tool_shows_history(tmp_path):
    from mnemo_plugin.registry import all_tools
    from mnemo_plugin.scheduler import get_store

    ctx = tool_ctx(tmp_path)
    get_store(ctx.config).record({"label": "晨报", "id": "a1", "tz": "America/Vancouver"}, True, "ok")
    out = all_tools()["schedule_list"].func({}, ctx).text
    assert "没有待执行" in out and "已提交「晨报」" in out


def test_add_and_remove_send_a_direct_notice(server, tmp_path):
    srv, c = server(MNEMO_URL="http://x", MNEMO_API_KEY="k", DATA_DIR=str(tmp_path))
    ctx = {"chat": 5, "channel": "telegram", "tz": "America/Vancouver"}
    st, res = c.req("POST", "/v1/tools/schedule_add", {"arguments": {"label": "晨报", "prompt": "P", "time": "7:30"}, "context": ctx})
    assert st == 200 and res["notify"].startswith("✅ 已创建定时任务") and "07:30" in res["notify"] and "晨报" in res["notify"]
    st, res = c.req("POST", "/v1/tools/schedule_remove", {"arguments": {"target": "晨报"}, "context": ctx})
    assert st == 200 and "晨报" in res["notify"]
    st, res = c.req("POST", "/v1/tools/schedule_list", {"arguments": {}, "context": ctx})
    assert st == 200 and "notify" not in res
