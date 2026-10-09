"""Stato condiviso in memoria tra il ciclo di monitoraggio e la web UI, con stream di eventi."""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from pathlib import Path

log = logging.getLogger(__name__)


class Hub:
    def __init__(self, data_dir: str):
        self._cond = threading.Condition()
        self._events: deque[dict] = deque(maxlen=1500)
        self._seq = 0
        self.status: dict = {"state": "avvio", "started_at": time.time()}
        self.attempts_path = Path(data_dir) / "attempts.jsonl"
        self.attempts: deque[dict] = deque(self._load_attempts(), maxlen=200)

    # --- eventi --------------------------------------------------------------------
    def emit(self, kind: str, data) -> None:
        with self._cond:
            self._seq += 1
            self._events.append({"id": self._seq, "ts": time.time(), "type": kind, "data": data})
            self._cond.notify_all()

    def wait(self, after_id: int, timeout: float) -> list[dict]:
        with self._cond:
            if self._seq <= after_id:
                self._cond.wait(timeout)
            if after_id > self._seq:  # il server è ripartito: il client riceve tutto
                after_id = 0
            return [e for e in self._events if e["id"] > after_id]

    @property
    def last_id(self) -> int:
        with self._cond:
            return self._seq

    def recent_logs(self, n: int = 400) -> list[dict]:
        with self._cond:
            return [e for e in self._events if e["type"] == "log"][-n:]

    # --- stato ---------------------------------------------------------------------
    def update(self, **kw) -> None:
        kw = json.loads(json.dumps(kw, default=str))
        with self._cond:
            self.status.update(kw)
        self.emit("status", kw)

    def snapshot(self) -> dict:
        with self._cond:
            return json.loads(json.dumps(self.status, default=str))

    # --- storico tentativi (persistente) -------------------------------------------
    def add_attempt(self, rec: dict) -> None:
        rec = json.loads(json.dumps({"ts": time.time(), **rec}, default=str))
        with self._cond:
            self.attempts.append(rec)
        try:
            with open(self.attempts_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError as e:
            log.debug("Scrittura storico tentativi fallita: %s", e)
        self.emit("attempt", rec)

    def attempts_list(self) -> list[dict]:
        with self._cond:
            return list(self.attempts)

    def _load_attempts(self) -> list[dict]:
        try:
            lines = self.attempts_path.read_text(encoding="utf-8").splitlines()[-200:]
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out


class HubLogHandler(logging.Handler):
    def __init__(self, hub: Hub):
        super().__init__()
        self.hub = hub
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.hub.emit("log", {"level": record.levelname, "msg": self.format(record), "ts": record.created})
        except Exception:
            pass


class Runtime:
    """Controllo del ciclo principale condiviso con la web UI."""

    def __init__(self, hub: Hub, state):
        self.hub = hub
        self.state = state
        self.cfg = None
        self.order_mutex = threading.Lock()  # un solo flusso carrello alla volta
        self.wake = threading.Event()
        self.stop = False
        self.reload_requested = False
        self.poll_now = False

    def request_reload(self) -> None:
        self.reload_requested = True
        self.wake.set()

    def request_poll(self) -> None:
        self.poll_now = True
        self.wake.set()

    def request_stop(self) -> None:
        self.stop = True
        self.wake.set()

    def interrupted(self) -> bool:
        return self.stop or self.reload_requested or self.poll_now

    def sleep(self, seconds: float) -> None:
        """Dorme aggiornando l'heartbeat; si sveglia subito su stop/reload/poll immediato."""
        end = time.monotonic() + seconds
        while True:
            self.state.beat()
            left = end - time.monotonic()
            if left <= 0 or self.interrupted():
                return
            self.wake.wait(min(15, left))
            self.wake.clear()
