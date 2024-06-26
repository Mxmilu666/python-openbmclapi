from loguru import logger as Logger
from pathlib import Path
import sys
from core.config import Config

basic_logger_format = "<green>[{time:YYYY-MM-DD HH:mm:ss}]</green> <level>[{level}] <yellow>[{name}:{function}:{line}]</yellow>: {message}</level>"
debug_mode = Config.get("advanced.debug")


class LoggingMessage:
    def __init__(self) -> None:
        self.data = []


def _log(*values):
    data = []
    for v in values:
        try:
            data.append(str(v))
        except:
            data.append(repr(v))
    return " ".join(data)


class LoggingLogger:
    def __init__(self, depth: int = 0):
        self.log = Logger.opt(depth=2 + depth)
        self.log.remove()
        self.cur_handler = None
        self.log.add(
            Path("./logs/{time:YYYY-MM-DD}.log"),
            format=basic_logger_format,
            retention="10 days",
            encoding="utf-8",
        )
        self.add_log("DEBUG" if debug_mode else "INFO")

    def add_log(self, level: str):
        if self.cur_handler:
            self.log.remove(self.cur_handler)
        self.cur_handler = self.log.add(
            sys.stderr,
            format=basic_logger_format,
            level=level,
            colorize=True,
        )

    def _log_with_args(self, level, *args, **kwargs):
        message = _log(*args) if args else ""
        self.log.log(level, message, **kwargs)

    def info(self, *args, **kwargs):
        self._log_with_args("INFO", *args, **kwargs)

    def error(self, *args, **kwargs):
        self._log_with_args("ERROR", *args, **kwargs)

    def debug(self, *args, **kwargs):
        self._log_with_args("DEBUG", *args, **kwargs)

    def warn(self, *args, **kwargs):
        self._log_with_args("WARNING", *args, **kwargs)

    def exception(self, *args, **kwargs):
        self._log_with_args("EXCEPTION", *args, **kwargs)

    def success(self, *args, **kwargs):
        self._log_with_args("SUCCESS", *args, **kwargs)

    def depth(self, depth):
        return LoggingLogger(depth)


logger = LoggingLogger()
