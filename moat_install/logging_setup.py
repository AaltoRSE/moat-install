"""Central logging configuration — the project's logging standard.

All debugging information in moat-install is emitted through Python's
:mod:`logging` module, never via bare ``print()`` to stderr. The rules every
module must follow:

* Each module creates its logger once at import time:
  ``logger = get_logger(__name__)``.
* Adapters additionally get a per-harness child logger via
  :class:`moat_install.adapters._base.Adapter` (``self.log``), so log records
  identify the harness.
* ``cli.run()`` calls :func:`setup_logging` once per run, right after argument
  parsing: with ``--debug`` the ``moat_install`` package logger runs at DEBUG
  level (the verbose decision trace — path resolution, merge steps, HTTP
  calls, file creation, backups, rc-file edits); without it, the level is
  WARNING and debug records are dropped.
* Debug output goes to **stderr**; stdout carries only the user-facing
  summaries. DEBUG records keep the historical ``[debug] `` prefix; other
  levels carry their level name (``[warning]``, ``[error]``).
* Secrets: log messages must never contain API key material. Masking is the
  caller's responsibility — ``moat_install.home.mask_key`` is the single
  masking helper — and is guarded end to end by
  ``tests/test_cli.py::test_debug_masks_secrets``.
"""

from __future__ import annotations

import logging
import sys
from typing import Optional, TextIO

#: Root name of the package logger; every module logger lives below it.
LOGGER_NAME = "moat_install"


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """Return a logger inside the package namespace.

    ``get_logger()`` returns the package logger itself; ``get_logger(__name__)``
    returns the caller's module logger (already under ``moat_install``), and any
    other name is namespaced under ``moat_install`` so that the configuration
    installed by :func:`setup_logging` always applies.
    """
    if not name:
        return logging.getLogger(LOGGER_NAME)
    if not name.startswith(LOGGER_NAME + ".") and name != LOGGER_NAME:
        name = f"{LOGGER_NAME}.{name}"
    return logging.getLogger(name)


class _PrefixFormatter(logging.Formatter):
    """Formatter that prefixes each record with a bracketed level tag.

    DEBUG records keep the historical ``[debug] `` prefix so existing output
    comparisons (and human muscle memory) keep working; other levels render as
    ``[warning]``, ``[error]``, and so on.
    """

    def format(self, record: logging.LogRecord) -> str:
        return f"[{record.levelname.lower()}] {super().format(record)}"


def setup_logging(debug: bool = False, stream: Optional[TextIO] = None) -> None:
    """(Re)configure the package logger; safe to call repeatedly.

    Installs exactly one stderr handler on the ``moat_install`` logger —
    replacing any previous handler, so repeated calls never accumulate
    handlers — and sets the level: DEBUG when ``debug`` is true (``--debug``),
    WARNING otherwise. ``stream`` defaults to the current ``sys.stderr`` and
    exists for tests.
    """
    logger = get_logger()
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(_PrefixFormatter("%(message)s"))
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if debug else logging.WARNING)
    logger.propagate = False
