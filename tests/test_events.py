import json
import urllib.request

from mnemo_plugin.registry import event

from conftest import Client

SEEN: list = []


@event("user_message", "turn_done")
def _record(ev, ctx):
    SEEN.append((ev["type"], ctx.chat, ctx.channel, ctx.tz, ev.get("data")))


@event("turn_done")
def _boom(ev, ctx):
    raise RuntimeError("secret internals")


def post(c: Client, body, token="default"):
    """POST /v1/events; returns (status, body bytes)."""
    r = urllib.request.Request(c.base + "/v1/events", data=json.dumps(body).encode(), method="POST")
    t = c.token if token == "default" else token
    if t:
        r.add_header("Authorization", "Bearer " + t)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_tool_list_announces_the_events_handlers_asked_for(server):
    _, c = server()
    code, body = c.req("GET", "/v1/tools")
    assert code == 200 and {"user_message", "turn_done"} <= set(body["events"])


def test_event_reaches_handlers_with_context(server):
    _, c = server()
    SEEN.clear()
    code, raw = post(c, {"type": "user_message", "chat": 42, "channel": "telegram", "tz": "America/Vancouver", "data": {"kind": "text"}})
    assert (code, raw) == (204, b"")
    assert SEEN == [("user_message", 42, "telegram", "America/Vancouver", {"kind": "text"})]


def test_a_crashing_handler_is_invisible_to_mnemo_and_does_not_stop_the_others(server):
    _, c = server()
    SEEN.clear()
    code, raw = post(c, {"type": "turn_done", "chat": 1, "data": {"origin": "user"}})
    assert code == 204 and b"secret" not in raw
    assert [s[0] for s in SEEN] == ["turn_done"]


def test_unknown_event_types_are_accepted_and_ignored(server):
    _, c = server()
    SEEN.clear()
    assert post(c, {"type": "something_new", "chat": 1})[0] == 204
    assert SEEN == []


def test_events_need_the_token_and_a_sane_body(server):
    _, c = server()
    SEEN.clear()
    assert post(c, {"type": "user_message", "chat": 1}, token=None)[0] == 401
    assert post(c, {"type": "user_message", "chat": 1}, token="wrong")[0] == 401
    assert post(c, {"chat": 1})[0] == 400
    assert post(c, ["not", "an", "object"])[0] == 400
    assert SEEN == []
