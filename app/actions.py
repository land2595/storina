"""Operazioni condivise tra ciclo principale, riga di comando e web UI."""
from __future__ import annotations

import logging
import time

import ovh
import ovh.exceptions as ovhx

from .availability import NOT_AVAILABLE, AvailabilityClient, Offer
from .catalog import Catalog, CatalogProvider
from .config import Config

log = logging.getLogger("ovh-ks-sniper")

# Permessi minimi della consumer key
CK_RULES = [
    ("GET", "/order/cart"),
    ("POST", "/order/cart"),
    ("GET", "/order/cart/*"),
    ("POST", "/order/cart/*"),
    ("DELETE", "/order/cart/*"),
    ("GET", "/order/catalog/*"),
    ("GET", "/me"),
    ("GET", "/me/*"),
]


def transient(e: Exception) -> bool:
    """Errore di rete/5xx: va ritentato, non è un problema di credenziali."""
    if isinstance(e, (ovhx.HTTPError, ovhx.NetworkError, ovhx.InvalidResponse)):
        return True
    status = getattr(getattr(e, "response", None), "status_code", None)
    return status is None or status >= 500


def check_account(client) -> tuple[bool, str]:
    """Verifica credenziali e metodo di pagamento predefinito.

    Ritorna (ok, messaggio). Gli errori transitori (rete assente, 5xx) vengono rilanciati.
    """
    try:
        client.get("/me")
    except ovhx.APIError as e:
        if transient(e):
            raise
        return False, f"Autenticazione API OVH fallita ({type(e).__name__})"
    try:
        methods = client.get("/me/payment/method", default=True)
    except ovhx.APIError as e:
        if transient(e):
            raise
        return False, f"Lettura metodi di pagamento fallita ({type(e).__name__})"
    if not methods:
        return False, "Nessun metodo di pagamento PREDEFINITO sull'account: l'ordine non può essere pagato"
    return True, "Credenziali valide, metodo di pagamento predefinito presente"


def check_account_safe(cfg: Config) -> dict:
    """Versione per la UI: non solleva mai eccezioni."""
    from .orderer import make_client

    if not cfg.has_credentials:
        return {"ok": False, "message": "Credenziali OVH incomplete", "checked_at": time.time()}
    try:
        ok, msg = check_account(make_client(cfg))
    except Exception as e:
        ok, msg = False, f"Errore temporaneo: {type(e).__name__}"
    return {"ok": ok, "message": msg, "checked_at": time.time()}


def request_consumer_key(cfg: Config) -> dict:
    if not (cfg.app_key and cfg.app_secret):
        raise ValueError("Salva prima Application Key e Application Secret")
    client = ovh.Client(endpoint=cfg.endpoint, application_key=cfg.app_key, application_secret=cfg.app_secret)
    req = client.new_consumer_key_request()
    for method, path in CK_RULES:
        req.add_rule(method, path)
    return req.request()  # {"validationUrl", "consumerKey", "state"}


def matrix(cfg: Config, catalog: Catalog | None, entries: list[dict]) -> list[dict]:
    """Righe per la tabella disponibilità della UI: una per combinazione hardware."""
    rows = []
    for e in entries:
        row = {
            "fqn": e.get("fqn"), "planCode": e.get("planCode"), "storage": e.get("storage"),
            "memory": e.get("memory"),
            "dcs": {d.get("datacenter"): d.get("availability") for d in e.get("datacenters") or []},
        }
        if catalog is not None:
            c = catalog.evaluate(e, cfg.min_storage_tb, cfg.max_monthly_price, cfg.max_first_payment)
            row.update(ok=c.ok, reasons=c.reasons, storage_tb=c.storage_tb, monthly=c.monthly_ttc,
                       first=c.est_first_ttc)
        row["available_in"] = [dc for dc, av in row["dcs"].items() if dc in cfg.datacenters and av and av not in NOT_AVAILABLE]
        rows.append(row)
    rows.sort(key=lambda r: (not r.get("ok", False), r.get("monthly") or 1e9))
    return rows


def test_cart(cfg: Config, catalogs: CatalogProvider, avail: AvailabilityClient, state, dc: str | None,
              on_step=None):
    """Flusso carrello + anteprima checkout senza stock e senza mai fare il checkout."""
    from .orderer import CartManager, Orderer, Result, make_client

    if not cfg.has_credentials:
        return None, None, Result("rejected", "Credenziali OVH incomplete")
    dc = (dc or cfg.datacenters[0]).lower()
    client = make_client(cfg)
    cat = catalogs.get()
    for plan in cfg.plan_codes:
        for e in avail.fetch(plan):
            cand = cat.evaluate(e, cfg.min_storage_tb, cfg.max_monthly_price, cfg.max_first_payment)
            if not cand.ok:
                continue
            offer = Offer(entry=e, datacenter=dc, availability="test")
            log.info("Test carrello (senza stock, nessun checkout): %s", offer)
            orderer = Orderer(cfg, client, catalogs, avail, state, CartManager(cfg, client), on_step=on_step)
            res = orderer.attempt(offer, cand, force_dry_run=True)
            log.info("Esito test: %s — %s", res.kind, res.message)
            return offer, cand, res
    return None, None, Result("rejected", "Nessuna combinazione nel catalogo rispetta i requisiti")
