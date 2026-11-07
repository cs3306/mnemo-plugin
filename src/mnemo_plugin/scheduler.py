"""Recurring tasks: a small JSON-backed store plus a background thread that asks mnemo to run them.

A task is "at HH:MM [on these weekdays | on this date] in this time zone, run this prompt". When it is due the
scheduler calls mnemo's POST /v1/trigger; mnemo then runs a normal turn (persona, memory, search, plugin tools)
and sends the result to Telegram. Nothing here talks to the model.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import Config

log = logging.getLogger("mnemo_plugin.schedule")

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MAX_TASKS = 20
MAX_HISTORY = 10
MAX_PROMPT = 2000
MAX_LABEL = 40
TICK_SECONDS = 20
_TIME_RE = re.compile(r"^\s*(\d{1,2})[:：](\d{2})\s*$")


class ScheduleError(Exception):
    pass


def parse_time(s: str) -> str:
    m = _TIME_RE.match(s or "")
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        raise ScheduleError(f"时间 {s!r} 不对，应为 24 小时制 HH:MM，例如 07:30")
    return f"{int(m[1]):02d}:{m[2]}"


def get_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ScheduleError(f"未知时区 {name!r}") from None


class ScheduleStore:
    """All tasks, in memory, mirrored to one JSON file (atomic replace) so they survive restarts."""

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.tasks: list[dict] = []
        self.history: list[dict] = []     # most recent runs, newest last; survives one-off tasks being deleted
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.tasks = [t for t in raw.get("tasks", []) if isinstance(t, dict) and t.get("id") and t.get("prompt")]
            self.history = [h for h in raw.get("history", []) if isinstance(h, dict)][-MAX_HISTORY:]
        except FileNotFoundError:
            self.tasks = []
        except (OSError, ValueError) as e:
            # Keep the unreadable file for inspection instead of silently overwriting it.
            bad = self.path.with_suffix(".corrupt")
            try:
                os.replace(self.path, bad)
            except OSError:
                pass
            log.error("cannot read %s (%s); moved aside to %s and starting empty", self.path, e, bad)
            self.tasks = []

    def save(self) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".schedules-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump({"version": 1, "tasks": self.tasks, "history": self.history}, f, ensure_ascii=False, indent=1)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

    # ------------------------------------------------------------------------------------------ operations

    def add(self, *, label: str, prompt: str, at: str, days: list[str] | None, on: str | None, tz: str, chat: int | None) -> dict:
        label, prompt = (label or "").strip(), (prompt or "").strip()
        if not label or not prompt:
            raise ScheduleError("label 和 prompt 都不能为空")
        if len(label) > MAX_LABEL:
            raise ScheduleError(f"label 太长（最多 {MAX_LABEL} 字符）")
        if len(prompt) > MAX_PROMPT:
            raise ScheduleError(f"prompt 太长（最多 {MAX_PROMPT} 字符）")
        at = parse_time(at)
        zone = get_zone(tz)
        if days and on:
            raise ScheduleError("days 和 date 只能二选一")
        if days:
            bad = [d for d in days if d not in DAYS]
            if bad:
                raise ScheduleError(f"days 里有无效值 {bad}，应为 {DAYS}")
            days = [d for d in DAYS if d in days]
            if len(days) == 7:
                days = None
        if on:
            try:
                d = date.fromisoformat(on)
            except ValueError:
                raise ScheduleError(f"date {on!r} 不对，应为 YYYY-MM-DD") from None
            hh, mm = map(int, at.split(":"))
            if datetime.combine(d, time(hh, mm), zone) <= datetime.now(zone):
                raise ScheduleError("这个时间已经过去了")
        with self.lock:
            if len(self.tasks) >= MAX_TASKS:
                raise ScheduleError(f"定时任务已达上限 {MAX_TASKS} 个，请先删除不用的")
            if any(t["label"] == label for t in self.tasks):
                raise ScheduleError(f"已经有名为「{label}」的定时任务；先删除它，或换个名字")
            ids = {t["id"] for t in self.tasks}
            tid = secrets.token_hex(2)
            while tid in ids:
                tid = secrets.token_hex(2)
            task = {"id": tid, "label": label, "prompt": prompt, "time": at, "days": days, "date": on or None,
                    "tz": tz or "UTC", "chat": chat, "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "last_fired": None, "last_error": None}
            self.tasks.append(task)
            self.save()
            return dict(task)

    def remove(self, target: str) -> list[dict]:
        target = (target or "").strip()
        if not target:
            raise ScheduleError("需要 id 或名称")
        with self.lock:
            hit = [t for t in self.tasks if t["id"] == target] or [t for t in self.tasks if t["label"] == target]
            if not hit:
                hit = [t for t in self.tasks if target in t["label"]]
            if not hit:
                raise ScheduleError(f"没有找到「{target}」；用 schedule_list 看现有任务")
            if len(hit) > 1:
                raise ScheduleError("匹配到多个任务：" + "、".join(f"{t['label']}({t['id']})" for t in hit) + "；请用 id")
            self.tasks = [t for t in self.tasks if t is not hit[0]]
            self.save()
            return [hit[0]]

    def record(self, task: dict, ok: bool, detail: str = "") -> None:
        """Remember a final outcome (the caller saves). Transient failures that will be retried are not recorded."""
        with self.lock:
            try:
                now = datetime.now(get_zone(task.get("tz", "UTC")))
            except ScheduleError:
                now = datetime.now(timezone.utc)
            self.history.append({"label": task["label"], "id": task["id"], "when": now.strftime("%m-%d %H:%M ") + str(now.tzinfo),
                                 "ok": ok, "detail": detail[:200]})
            del self.history[:-MAX_HISTORY]

    def list(self) -> list[dict]:
        with self.lock:
            return [dict(t) for t in self.tasks]


def describe_history(h: dict) -> str:
    return f"{h.get('when', '?')} {'已提交' if h.get('ok') else '失败'}「{h.get('label', '?')}」{h.get('detail', '')}"


def describe(t: dict) -> str:
    when = t["date"] if t.get("date") else ("每天" if not t.get("days") else "每周 " + "/".join(t["days"]))
    s = f"[{t['id']}] {t['label']} — {when} {t['time']} ({t['tz']})\n    任务：{t['prompt']}"
    if t.get("last_fired"):
        s += f"\n    上次执行：{t['last_fired']}"
    if t.get("last_error"):
        s += f"\n    最近错误：{t['last_error']}"
    return s


# ------------------------------------------------------------------------------------------------ the thread

Trigger = Callable[[dict], None]   # raises PermanentError / TransientError


class PermanentError(Exception):
    pass


class TransientError(Exception):
    pass


def http_trigger(cfg: Config) -> Trigger:
    def fire(task: dict) -> None:
        body: dict = {"prompt": task["prompt"], "label": task["label"]}
        if isinstance(task.get("chat"), int) and task["chat"] > 0:
            body["chat"] = task["chat"]
        req = urllib.request.Request(cfg.mnemo_url + "/v1/trigger", data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + cfg.mnemo_api_key})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                r.read(1 << 16)
        except urllib.error.HTTPError as e:
            msg = e.read(300).decode("utf-8", "replace")
            if e.code == 429 or e.code >= 500:
                raise TransientError(f"mnemo 返回 {e.code}: {msg}") from None
            raise PermanentError(f"mnemo 拒绝了请求 {e.code}: {msg}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise TransientError(f"连不上 mnemo：{getattr(e, 'reason', e)}") from None

    return fire


class Scheduler(threading.Thread):
    def __init__(self, store: ScheduleStore, cfg: Config, trigger: Trigger | None = None,
                 now: Callable[[], datetime] | None = None):
        super().__init__(name="scheduler", daemon=True)
        self.store, self.cfg = store, cfg
        self.trigger = trigger or http_trigger(cfg)
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.stop_event = threading.Event()
        self._retry: dict[str, tuple[int, datetime]] = {}   # task id -> (attempts, next try)

    def run(self) -> None:
        log.info("scheduler started with %d task(s)", len(self.store.list()))
        while not self.stop_event.wait(TICK_SECONDS):
            try:
                self.tick()
            except Exception:
                log.exception("scheduler tick failed")

    def stop(self) -> None:
        self.stop_event.set()

    def due_slot(self, t: dict, now: datetime) -> tuple[str, datetime] | None:
        """(slot key, scheduled instant) if the task should be fired for the current slot, else None."""
        try:
            zone = get_zone(t.get("tz", "UTC"))
        except ScheduleError:
            return None
        local = now.astimezone(zone)
        hh, mm = map(int, t["time"].split(":"))
        if t.get("date"):
            try:
                d = date.fromisoformat(t["date"])
            except ValueError:
                return None
        else:
            d = local.date()
            if t.get("days") and DAYS[d.weekday()] not in t["days"]:
                return None
        at = datetime.combine(d, time(hh, mm), zone)
        if local < at:
            return None
        return d.isoformat(), at

    def tick(self) -> None:
        now = self.now()
        grace = timedelta(minutes=self.cfg.schedule_grace_min)
        changed = False
        for t in self.store.list():
            slot = self.due_slot(t, now)
            if not slot:
                continue
            key, at = slot
            if t.get("last_fired") == key:
                continue
            if now - at.astimezone(timezone.utc) > grace:
                if t.get("date"):          # a one-off we missed for good
                    self.store.record(t, False, f"错过了 {key} {t['time']}，已删除")
                    self._finish(t["id"], drop=True, error=f"错过了 {key} {t['time']}，已删除")
                    changed = True
                self._retry.pop(t["id"], None)
                continue
            attempts, nxt = self._retry.get(t["id"], (0, now))
            if now < nxt:
                continue
            try:
                self.trigger(t)
            except PermanentError as e:
                log.error("task %s (%s): %s", t["id"], t["label"], e)
                self.store.record(t, False, str(e))
                self._finish(t["id"], fired=key, error=str(e), drop=bool(t.get("date")))
                self._retry.pop(t["id"], None)
            except TransientError as e:
                attempts += 1
                delay = min(300, 15 * 2 ** min(attempts, 5))
                self._retry[t["id"]] = (attempts, now + timedelta(seconds=delay))
                log.warning("task %s (%s): %s; retry in %ds", t["id"], t["label"], e, delay)
                self._finish(t["id"], error=str(e))
            else:
                log.info("task %s (%s) fired for %s", t["id"], t["label"], key)
                self.store.record(t, True, f"已提交给 mnemo（{key} {t['time']}）")
                self._finish(t["id"], fired=key, drop=bool(t.get("date")), error="")
                self._retry.pop(t["id"], None)
            changed = True
        if changed:
            self.store.save()

    def _finish(self, tid: str, *, fired: str | None = None, error: str | None = None, drop: bool = False) -> None:
        with self.store.lock:
            for t in self.store.tasks:
                if t["id"] == tid:
                    if drop:
                        self.store.tasks.remove(t)
                    else:
                        if fired:
                            t["last_fired"] = fired
                        if error is not None:
                            t["last_error"] = error or None
                    return


_STORES: dict[str, ScheduleStore] = {}
_STORES_LOCK = threading.Lock()


def get_store(cfg: Config) -> ScheduleStore:
    path = str(Path(cfg.data_dir) / "schedules.json")
    with _STORES_LOCK:
        if path not in _STORES:
            _STORES[path] = ScheduleStore(path)
        return _STORES[path]
