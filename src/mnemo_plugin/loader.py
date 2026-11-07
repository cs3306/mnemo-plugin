from __future__ import annotations

import importlib
import importlib.util
import logging
import pkgutil
from pathlib import Path

log = logging.getLogger("mnemo_plugin")


def load_builtin() -> None:
    from . import tools

    for m in pkgutil.iter_modules(tools.__path__):
        importlib.import_module(f"{tools.__name__}.{m.name}")


def load_extra(directory: str) -> list[str]:
    """Import every *.py in directory (not recursive); each registers its tools with @tool. Returns error messages."""
    errs: list[str] = []
    d = Path(directory)
    if not d.is_dir():
        return [f"EXTRA_TOOLS_DIR {directory!r} is not a directory"]
    for p in sorted(d.glob("*.py")):
        if p.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"mnemo_extra_{p.stem}", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            log.info("loaded extra tools from %s", p)
        except Exception as e:  # a broken user file must not take the server down
            errs.append(f"{p.name}: {type(e).__name__}: {e}")
            log.exception("cannot load %s", p)
    return errs
