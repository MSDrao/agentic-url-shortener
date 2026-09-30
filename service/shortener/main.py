"""ASGI entrypoint: `uvicorn shortener.main:app`."""

import json
import logging

from .api import create_app

_STD = set(vars(logging.makeLogRecord({})))


class JsonFormatter(logging.Formatter):
    """One JSON object per line, including structured `extra=` fields (request_id, status, ...)."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {"ts": self.formatTime(record), "level": record.levelname, "logger": record.name,
                   "msg": record.getMessage()}
        payload.update({k: v for k, v in vars(record).items() if k not in _STD and k != "message"})
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


_handler = logging.StreamHandler()
_handler.setFormatter(JsonFormatter())
logging.basicConfig(level=logging.INFO, handlers=[_handler])

app = create_app()
