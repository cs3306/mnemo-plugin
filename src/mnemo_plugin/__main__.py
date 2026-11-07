from __future__ import annotations

import logging
import signal
import sys
import threading

from .config import Config
from .loader import load_builtin, load_extra
from .scheduler import Scheduler, get_store
from .server import PluginServer


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("mnemo_plugin")
    cfg = Config.from_env()
    errs = cfg.validate()
    if errs:
        for e in errs:
            log.error("config: %s", e)
        return 2
    load_builtin()
    if cfg.extra_tools_dir:
        for e in load_extra(cfg.extra_tools_dir):
            log.error("extra tools: %s", e)
    srv = PluginServer(cfg)
    log.info("tools: %s", ", ".join(sorted(srv.tools)) or "(none)")
    for name, why in srv.disabled.items():
        log.warning("tool %s disabled: %s", name, why)
    sched = None
    if "schedule_add" in srv.tools:
        sched = Scheduler(get_store(cfg), cfg)
        sched.start()
    if not cfg.token:
        log.warning("running WITHOUT authentication (PLUGIN_ALLOW_ANONYMOUS=true)")

    def stop(*_):
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("listening on %s:%d", cfg.host, cfg.port)
    srv.serve_forever()
    srv.server_close()
    if sched:
        sched.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
