import logging
import os
import sys
import psutil
from pathlib import Path

_PROCESS = psutil.Process(os.getpid())
_last_rss = _PROCESS.memory_info().rss

class RSSDeltaFilter(logging.Filter):
    def filter(self, record):
        global _last_rss
        # Each handler filters the same record, so only the first pass may
        # consume the delta - otherwise later handlers measure the gap since
        # the earlier handler and always report ~0MB.
        if hasattr(record, "rss"):
            return True
        rss = _PROCESS.memory_info().rss
        record.rss = f"{rss / (1024**2):,.1f}MB"
        record.rss_delta = f"{(rss - _last_rss) / (1024**2):+,.1f}MB"
        _last_rss = rss
        return True

# Kept ASCII: the console stream is cp1252 whenever stdout is redirected,
# which cannot encode non-ASCII and would raise on every record.
FORMATTER = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - [RSS %(rss)s | delta %(rss_delta)s] - %(message)s"
)

class FlushFileHandler(logging.FileHandler):
    """FileHandler that flushes on every emit (helps in notebooks)."""
    def emit(self, record):
        super().emit(record)
        self.flush()

def get_console_handler():
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(FORMATTER)
    handler.addFilter(RSSDeltaFilter())
    return handler

def get_file_handler(modelprefix:str):
    logdir = Path(rf"C:\{modelprefix}")
    logdir.mkdir(parents=True, exist_ok=True)

    logfile = logdir / "Model.log"

    handler = FlushFileHandler(logfile, mode="w", encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(FORMATTER)
    handler.addFilter(RSSDeltaFilter())
    return handler

def get_logger(logger_name,level="debug",modelprefix="UnnamedPythonModel"):
    logger = logging.getLogger(logger_name)
    loglevel = logging.DEBUG if level == "debug" else logging.INFO
    logger.setLevel(loglevel)

    # ✅ KEY: If handlers already exist, don’t add more and don’t reopen file
    if logger.handlers:
        return logger

    logger.addHandler(get_console_handler())
    logger.addHandler(get_file_handler(modelprefix))
    logger.propagate = False
    return logger

def setup_logging(modelprefix: str, level: str = "debug") -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    loglevel = logging.DEBUG if level == "debug" else logging.INFO
    root.setLevel(loglevel)
    root.addHandler(get_console_handler())
    root.addHandler(get_file_handler(modelprefix))

    # Suppress noisy third-party loggers
    for noisy in ['markdown_it', 'PIL', 'matplotlib', 'urllib3', 'asyncio']:
        logging.getLogger(noisy).setLevel(logging.WARNING)