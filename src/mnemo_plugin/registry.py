"""Tool registry: the one interface every tool (built-in or user-supplied) implements."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import Config

NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")


class ToolError(Exception):
    """A failure the model should see and may react to (bad arguments, a document that does not compile, ...)."""


@dataclass
class File:
    name: str
    mime: str
    data: bytes


@dataclass
class Result:
    text: str = ""
    files: list[File] = field(default_factory=list)


@dataclass
class Context:
    config: Config
    chat: int | None = None
    channel: str = ""     # "telegram" or "api"


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    func: Callable[[dict, Context], Result]
    # Returns None when the tool can run, otherwise a short reason (e.g. "pandoc not installed").
    available: Callable[[Config], str | None] = lambda cfg: None


_TOOLS: dict[str, Tool] = {}


def tool(name: str, description: str, parameters: dict | None = None, available: Callable[[Config], str | None] | None = None):
    """Register a function as a tool.

        @tool("echo", "Repeat the text back", {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]})
        def echo(args, ctx):
            return Result(text=args["text"])
    """
    if not NAME_RE.match(name):
        raise ValueError(f"invalid tool name {name!r}")

    def deco(fn):
        _TOOLS[name] = Tool(name, description, parameters or {"type": "object", "properties": {}}, fn, available or (lambda cfg: None))
        return fn

    return deco


def all_tools() -> dict[str, Tool]:
    return dict(_TOOLS)


def active_tools(cfg: Config) -> tuple[dict[str, Tool], dict[str, str]]:
    """(tools that can run now, name -> reason for those that cannot)."""
    ok, off = {}, {}
    for name in sorted(_TOOLS):
        reason = _TOOLS[name].available(cfg)
        if reason:
            off[name] = reason
        else:
            ok[name] = _TOOLS[name]
    return ok, off


# --------------------------------------------------------------------------------------- argument validation

_TYPES: dict[str, Any] = {
    "string": str, "boolean": bool, "array": list, "object": dict,
}


def _type_ok(v: Any, t: str) -> bool:
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if t == "null":
        return v is None
    return isinstance(v, _TYPES[t]) if t in _TYPES else True


def validate(value: Any, schema: dict, path: str = "arguments") -> None:
    """A small JSON-Schema subset (type, required, properties, items, enum, minItems/maxItems, maxLength), enough to give
    the model a clear message instead of a stack trace. Unknown keywords are ignored."""
    t = schema.get("type")
    if t:
        types = t if isinstance(t, list) else [t]
        if not any(_type_ok(value, x) for x in types):
            raise ToolError(f"{path} 应为 {'/'.join(types)}")
    if "enum" in schema and value not in schema["enum"]:
        raise ToolError(f"{path} 必须是 {schema['enum']} 之一")
    if isinstance(value, str) and "maxLength" in schema and len(value) > schema["maxLength"]:
        raise ToolError(f"{path} 太长（最多 {schema['maxLength']} 字符）")
    if isinstance(value, dict):
        for k in schema.get("required", []):
            if k not in value:
                raise ToolError(f"缺少参数 {path}.{k}")
        for k, sub in schema.get("properties", {}).items():
            if k in value and value[k] is not None:
                validate(value[k], sub, f"{path}.{k}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise ToolError(f"{path} 至少需要 {schema['minItems']} 项")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ToolError(f"{path} 最多 {schema['maxItems']} 项")
        if "items" in schema:
            for i, x in enumerate(value):
                validate(x, schema["items"], f"{path}[{i}]")
