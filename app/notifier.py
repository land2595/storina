"""Notifiche Telegram asincrone: non rallentano mai il flusso d'ordine."""
from __future__ import annotations

import logging
import queue
import threading

import requests

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, token: str, chat_id: str, prefix: str = ""):
        self.enabled = bool(token and chat_id)
        self._url = f"https://api.telegram.org/bot{token}/sendMessage" if self.enabled else ""
        self._chat_id = chat_id
        self._prefix = prefix
        self._q: queue.Queue[str] = queue.Queue()
        self._session = requests.Session()
        if self.enabled:
            threading.Thread(target=self._worker, name="telegram", daemon=True).start()

    def send(self, text: str) -> None:
        if self.enabled:
            self._q.put(f"{self._prefix}{text}")

    def flush(self, timeout: float = 15) -> None:
        """Attende (al massimo `timeout` s) che la coda si svuoti, es. prima di uno stop."""
        if not self.enabled:
            return
        done = threading.Event()

        def _wait():
            self._q.join()
            done.set()

        threading.Thread(target=_wait, daemon=True).start()
        done.wait(timeout)

    def _worker(self) -> None:
        while True:
            text = self._q.get()
            try:
                for attempt in range(3):
                    try:
                        r = self._session.post(
                            self._url,
                            json={"chat_id": self._chat_id, "text": text[:4000], "disable_web_page_preview": True},
                            timeout=10,
                        )
                        if r.status_code == 200:
                            break
                        log.warning("Telegram ha risposto HTTP %s", r.status_code)
                    except requests.RequestException as e:
                        # Non loggare str(e): contiene l'URL con il token.
                        log.warning("Invio Telegram fallito (%s), tentativo %d/3", type(e).__name__, attempt + 1)
            finally:
                self._q.task_done()
