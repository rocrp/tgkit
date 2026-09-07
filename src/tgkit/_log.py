"""Structured-style logging over the stdlib.

Call sites write ``logger.warning("event", key=value)``; the wrapper renders
``event key=value`` through ``logging.getLogger("tgkit")`` so applications
configure tgkit's output with ordinary ``logging`` handlers.
"""

from __future__ import annotations

import logging
from typing import Any


class _Logger:
    __slots__ = ("_logger",)

    def __init__(self, name: str) -> None:
        self._logger = logging.getLogger(name)

    def _log(self, level: int, event: str, kv: dict[str, Any], *, exc_info: bool = False) -> None:
        if not self._logger.isEnabledFor(level):
            return
        message = " ".join([event, *(f"{key}={value}" for key, value in kv.items())])
        self._logger.log(level, message, exc_info=exc_info)

    def debug(self, event: str, **kv: Any) -> None:
        self._log(logging.DEBUG, event, kv)

    def info(self, event: str, **kv: Any) -> None:
        self._log(logging.INFO, event, kv)

    def warning(self, event: str, **kv: Any) -> None:
        self._log(logging.WARNING, event, kv)

    def error(self, event: str, **kv: Any) -> None:
        self._log(logging.ERROR, event, kv)

    def exception(self, event: str, **kv: Any) -> None:
        self._log(logging.ERROR, event, kv, exc_info=True)


logger = _Logger("tgkit")
