import logging
import sys


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    if getattr(root, "_asx_configured", False):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "yfinance", "peewee", "apscheduler.executors", "curl_cffi"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    root._asx_configured = True  # type: ignore[attr-defined]
