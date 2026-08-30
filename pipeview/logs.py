"""Shared logging setup for the CLIs: -v/-vv to stderr, --log-file
always at debug level (a file can afford full detail)."""

from __future__ import annotations

import logging
import os
import sys


def configure(verbose: int, log_file: str | None, *,
              stderr: bool = True) -> None:
    """Configure the pipeview logger tree. `stderr=False` keeps the
    stream handler off even with -v (the curses browser owns the
    terminal and logs to its file instead)."""
    logger = logging.getLogger("pipeview")
    logger.handlers.clear()
    if not verbose and not log_file:
        return
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    if verbose and stderr:
        handler = logging.StreamHandler(sys.stderr)
        handler.setLevel(logging.INFO if verbose == 1 else logging.DEBUG)
        handler.setFormatter(fmt)
        logger.addHandler(handler)
    if log_file:
        os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
