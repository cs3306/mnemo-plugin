"""schedule_add / schedule_list / schedule_remove: let the user set up recurring tasks by talking to the bot."""
from __future__ import annotations

import os

from ..config import Config
from ..registry import Context, Result, ToolError, tool
from ..scheduler import DAYS, MAX_LABEL, MAX_PROMPT, ScheduleError, describe, describe_history, get_store


def _available(cfg: Config) -> str | None:
    if not cfg.mnemo_url or not cfg.mnemo_api_key:
        return "MNEMO_URL / MNEMO_API_KEY not set (schedules need mnemo's API)"
    try:
        os.makedirs(cfg.data_dir, exist_ok=True)
    except OSError as e:
        return f"data dir {cfg.data_dir} is not writable: {e.strerror}"
    if not os.access(cfg.data_dir, os.W_OK):
        return f"data dir {cfg.data_dir} is not writable"
    return None


@tool("schedule_add",
      "创建一个定时任务：到点时系统会像用户刚发来一条消息那样，让你（带记忆、搜索和其它工具）完成 prompt 并把结果发给用户。"
      "只在用户明确要求“每天/每周/某天几点……”时才创建，不要自作主张。prompt 要写成自包含的完整指令（到点时看不到现在的对话），"
      "例如“搜索今天的重要新闻和天气，按 3-5 条要点写成晨报”。时间用用户所在时区的 24 小时制。创建后向用户复述名称和时间。",
      {"type": "object", "properties": {
          "label": {"type": "string", "maxLength": MAX_LABEL, "description": "简短名称，如“晨报”，用户之后靠它取消"},
          "prompt": {"type": "string", "maxLength": MAX_PROMPT, "description": "到点时要完成的完整指令"},
          "time": {"type": "string", "description": "HH:MM，24 小时制，如 07:30"},
          "days": {"type": "array", "items": {"type": "string", "enum": DAYS},
                   "description": "每周哪几天执行；不填 = 每天"},
          "date": {"type": "string", "description": "只执行一次的日期 YYYY-MM-DD；与 days 二选一"},
      }, "required": ["label", "prompt", "time"]},
      available=_available)
def schedule_add(args: dict, ctx: Context) -> Result:
    cfg = ctx.config
    tz = ctx.tz or cfg.schedule_tz or "UTC"
    chat = ctx.chat if isinstance(ctx.chat, int) and ctx.chat > 0 else None
    try:
        t = get_store(cfg).add(label=args["label"], prompt=args["prompt"], at=args["time"], days=args.get("days"),
                               on=args.get("date"), tz=tz, chat=chat)
    except ScheduleError as e:
        raise ToolError(str(e)) from None
    return Result(text="已创建：\n" + describe(t))


@tool("schedule_list", "列出所有定时任务（名称、时间、完整指令、上次执行）和最近几次执行记录。用户问“有哪些定时任务”、要取消前确认、或追查某个任务有没有执行时使用。",
      available=_available)
def schedule_list(args: dict, ctx: Context) -> Result:
    st = get_store(ctx.config)
    tasks, hist = st.list(), list(st.history)
    parts = ["\n".join(describe(t) for t in tasks) if tasks else "目前没有待执行的定时任务。"]
    if hist:
        parts.append("最近执行记录（一次性任务执行后会从上面的列表消失，只在这里留痕；“已提交”表示已交给 mnemo，结果会发到 Telegram）：\n"
                     + "\n".join(describe_history(h) for h in hist[-5:]))
    return Result(text="\n\n".join(parts))


@tool("schedule_remove", "删除一个定时任务。target 填任务名称或 id（见 schedule_list）。用户说“取消晨报”之类时使用。",
      {"type": "object", "properties": {"target": {"type": "string", "description": "任务名称或 id"}}, "required": ["target"]},
      available=_available)
def schedule_remove(args: dict, ctx: Context) -> Result:
    try:
        gone = get_store(ctx.config).remove(args["target"])
    except ScheduleError as e:
        raise ToolError(str(e)) from None
    return Result(text=f"已删除「{gone[0]['label']}」。")
