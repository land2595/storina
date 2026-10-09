"""Dopo il checkout: segue l'ordine su OVH (pagamento, verifica, consegna) e lo notifica.

Endpoint (GET /me/*, già coperti dalla consumer key):
- /me/order/{id}/status  -> cancelled | cancelling | checking | delivered | delivering |
                            documentsRequested | notPaid | unknown
- /me/order/{id}         -> url (pagina dell'ordine/pagamento), pdfUrl, expirationDate, priceWithTax
"""
from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger("ovh-ks-sniper")

FINAL = {"delivered", "cancelled"}
NOT_PAID_GRACE = 90  # s: subito dopo il checkout l'addebito automatico può non essere ancora registrato
LABELS = {
    "notPaid": "non pagato",
    "checking": "pagato, in verifica da OVH",
    "documentsRequested": "OVH chiede documenti",
    "delivering": "in consegna",
    "delivered": "consegnato",
    "cancelling": "in annullamento",
    "cancelled": "annullato",
    "unknown": "sconosciuto",
}


class OrderTracker:
    def __init__(self, rt, notifier):
        self.rt = rt
        self.notifier = notifier
        self._current: int | None = None
        self._lock = threading.Lock()

    def start(self, order_id, ordered_at: float | None = None) -> None:
        try:
            order_id = int(order_id)
        except (TypeError, ValueError):
            return
        with self._lock:
            if self._current == order_id:
                return
            self._current = order_id
        threading.Thread(target=self._run, args=(order_id, ordered_at or time.time()),
                         name=f"order-{order_id}", daemon=True).start()

    def _run(self, order_id: int, ordered_at: float) -> None:
        from . import config as config_mod
        from .orderer import make_client

        rt, hub = self.rt, self.rt.hub
        try:
            client = make_client(config_mod.load())
        except Exception as e:
            log.error("Monitoraggio ordine #%s impossibile: %s", order_id, e)
            return
        log.info("Seguo lo stato dell'ordine #%s su OVH", order_id)
        last, info, notified_unpaid, errors = None, {}, False, 0
        while not rt.stop and self._current == order_id:
            if not rt.state.lock_path.exists():
                break  # lock rimosso dall'utente: non serve più seguire l'ordine
            try:
                status = client.get(f"/me/order/{order_id}/status")
                if not info or status != last:
                    info = client.get(f"/me/order/{order_id}") or {}
                errors = 0
            except Exception as e:
                errors += 1
                log.warning("Lettura stato ordine #%s fallita (%s)", order_id, type(e).__name__)
                self._sleep(60 if errors < 10 else 600)
                continue

            price = (info.get("priceWithTax") or {}).get("value")
            rec = {"orderId": order_id, "status": status, "label": LABELS.get(status, status),
                   "url": info.get("url"), "pdfUrl": info.get("pdfUrl"),
                   "expirationDate": info.get("expirationDate"), "priceWithTax": price,
                   "checked_at": time.time()}
            hub.update(order_track=rec)
            rt.state.update_lock(payment={k: rec[k] for k in ("status", "label", "url", "pdfUrl", "checked_at")})

            elapsed = time.time() - ordered_at
            if status != last:
                log.warning("Ordine #%s: %s", order_id, rec["label"])
                if status != "notPaid":
                    self.notifier.send(self._message(rec))
            if status == "notPaid" and elapsed >= NOT_PAID_GRACE and not notified_unpaid:
                notified_unpaid = True
                log.error("Ordine #%s NON pagato: pagalo a mano da %s", order_id, rec["url"])
                self.notifier.send(self._message(rec))
            last = status
            if status in FINAL or elapsed > 72 * 3600:
                break
            self._sleep(20 if elapsed < 900 else 300)
        with self._lock:
            if self._current == order_id:
                self._current = None

    @staticmethod
    def _message(rec: dict) -> str:
        oid, st = rec["orderId"], rec["status"]
        if st == "notPaid":
            exp = f" entro {rec['expirationDate']}" if rec.get("expirationDate") else ""
            return (f"⚠️ Ordine #{oid} NON pagato: l'addebito automatico non è andato a buon fine. "
                    f"Pagalo subito{exp}: {rec.get('url') or 'OVH Manager → Ordini'}")
        icons = {"checking": "💳", "documentsRequested": "📄", "delivering": "🚚", "delivered": "📦",
                 "cancelling": "❌", "cancelled": "❌"}
        extra = " Controlla email e Manager OVH." if st == "documentsRequested" else ""
        return f"{icons.get(st, 'ℹ️')} Ordine #{oid}: {rec['label']}.{extra}"

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while not self.rt.stop and time.monotonic() < end:
            time.sleep(max(0.0, min(5.0, end - time.monotonic())))
