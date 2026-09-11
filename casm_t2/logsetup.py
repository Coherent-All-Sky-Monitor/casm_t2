"""Logging to journald and a plain file.

The stream handler feeds journalctl under systemd; the file handler writes the
greppable logs under /mnt/nvme5/casm_pipeline/logs/. An unwritable log file
degrades to journald only.
"""

from __future__ import annotations

import logging
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def setup(logfile: str | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if logfile:
        try:
            Path(logfile).parent.mkdir(parents=True, exist_ok=True)
            handlers.append(logging.FileHandler(logfile))
        except OSError:
            pass
    logging.basicConfig(level=logging.INFO, format=FORMAT, handlers=handlers)
