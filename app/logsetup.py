"""Logging con timestamp in ora locale (TZ) e oscuramento dei segreti."""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


class RedactFilter(logging.Filter):
    def __init__(self, secrets: list[str]):
        super().__init__()
        self.secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.secrets:
            return True
        msg = record.getMessage()
        redacted = msg
        for s in self.secrets:
            redacted = redacted.replace(s, "***")
        if redacted != msg:
            record.msg, record.args = redacted, None
        return True


def setup(data_dir: str, secrets: list[str], extra: list[logging.Handler] | None = None) -> RedactFilter:
    """Configura il logging. Il filtro restituito ha `secrets` modificabile (ricarica della config)."""
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S %Z")
    redact = RedactFilter(secrets)

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout), *(extra or [])]
    try:
        log_dir = Path(data_dir) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(log_dir / "monitor.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"))
    except OSError as e:
        print(f"Log su file disabilitato: {e}", file=sys.stderr)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)
    for h in handlers:
        if h.formatter is None:
            h.setFormatter(fmt)
        h.addFilter(redact)
        root.addHandler(h)
    # urllib3 a livello DEBUG logga gli URL (con il token Telegram): teniamolo zitto.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return redact
