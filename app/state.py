"""Stato persistente su /data: lock dell'ordine, blocco per errore, heartbeat."""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)


def atomic_write_json(path: Path, obj: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        # Un file illeggibile vale comunque come presente: meglio non ordinare.
        return {"error": "file illeggibile"}


class State:
    def __init__(self, data_dir: str):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.dir / "order.lock"
        self.halt_path = self.dir / "halt.json"
        self.heartbeat_path = self.dir / "heartbeat"
        self._locked_in_memory = False

    # --- lock ordine (requisito 3) -------------------------------------------------
    def lock(self) -> dict | None:
        data = _read_json(self.lock_path)
        if data is None and self._locked_in_memory:
            return {"error": "lock solo in memoria (scrittura su disco fallita)"}
        return data

    def is_locked(self) -> bool:
        return self._locked_in_memory or self.lock_path.exists()

    def write_lock(self, info: dict) -> None:
        # Prima la memoria: anche se il disco fallisce, questo processo non ordina più.
        # Se la scrittura riesce, il flag si azzera così che cancellare il file basti per riprendere.
        self._locked_in_memory = True
        info = {**info, "timestamp": datetime.now().astimezone().isoformat(timespec="seconds")}
        try:
            atomic_write_json(self.lock_path, info)
            self._locked_in_memory = False
            log.warning("Lock scritto in %s", self.lock_path)
        except (OSError, TypeError, ValueError) as e:
            log.critical("IMPOSSIBILE scrivere il lock %s: %s. Nessun altro ordine in questa sessione.", self.lock_path, e)

    def update_lock(self, **fields) -> None:
        """Aggiunge informazioni (es. stato del pagamento) a un lock esistente."""
        data = _read_json(self.lock_path)
        if data is None or "error" in data and len(data) == 1:
            return
        try:
            atomic_write_json(self.lock_path, {**data, **fields})
        except (OSError, TypeError, ValueError) as e:
            log.warning("Aggiornamento lock fallito: %s", e)

    # --- blocco dopo errore non recuperabile ---------------------------------------
    def halt(self) -> dict | None:
        return _read_json(self.halt_path)

    def write_halt(self, reason: str) -> None:
        try:
            atomic_write_json(
                self.halt_path,
                {"reason": reason, "timestamp": datetime.now().astimezone().isoformat(timespec="seconds")},
            )
        except OSError as e:
            log.error("Impossibile scrivere %s: %s", self.halt_path, e)

    # --- heartbeat per l'healthcheck Docker ----------------------------------------
    def beat(self) -> None:
        try:
            self.heartbeat_path.write_text(str(int(time.time())), encoding="utf-8")
        except OSError:
            pass
