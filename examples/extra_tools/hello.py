"""A custom tool. Mount this directory at EXTRA_TOOLS_DIR (compose: ./extra_tools:/extra_tools:ro), then
restart the plugin container and send `/plugins reload` to mnemo."""
import datetime as dt

from mnemo_plugin import File, Result, ToolError, tool


@tool(
    "make_ics",
    "生成日历事件文件（.ics），用户可以直接导入手机或电脑的日历。",
    {"type": "object", "properties": {
        "title": {"type": "string"},
        "date": {"type": "string", "description": "YYYY-MM-DD"},
        "time": {"type": "string", "description": "HH:MM，24 小时制；不填表示全天"},
        "minutes": {"type": "integer", "description": "时长（分钟），默认 60"},
    }, "required": ["title", "date"]},
)
def make_ics(args, ctx):
    try:
        day = dt.date.fromisoformat(args["date"])
        start = dt.datetime.combine(day, dt.time.fromisoformat(args["time"])) if args.get("time") else None
    except ValueError:
        raise ToolError("date 应为 YYYY-MM-DD，time 应为 HH:MM") from None
    if start:
        end = start + dt.timedelta(minutes=args.get("minutes", 60))
        when = f"DTSTART:{start:%Y%m%dT%H%M%S}\r\nDTEND:{end:%Y%m%dT%H%M%S}"
    else:
        when = f"DTSTART;VALUE=DATE:{day:%Y%m%d}\r\nDTEND;VALUE=DATE:{day + dt.timedelta(days=1):%Y%m%d}"
    title = args["title"].replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", " ")
    body = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//mnemo-plugin//EN\r\nBEGIN:VEVENT\r\n"
            f"UID:{day:%Y%m%d}-{abs(hash(title))}@mnemo-plugin\r\nDTSTAMP:{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}\r\n"
            f"{when}\r\nSUMMARY:{title}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    return Result(f"已生成日历事件：{args['title']}", [File("event.ics", "text/calendar", body.encode())])
