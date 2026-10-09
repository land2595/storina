"""Operazioni condivise tra ciclo principale, riga di comando e web UI."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import ovh
import requests
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
# Richiesto alla creazione ma non preteso dai controlli (le chiavi create prima non lo hanno):
# permette di leggere stato, scadenza e permessi della chiave stessa.
CK_EXTRA_RULES = [("GET", "/auth/currentCredential")]


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


CRITICAL_CHECKS = ("data", "credentials", "consumer_key", "account", "payment")


def _rule_covers(rule: dict, method: str, path: str) -> bool:
    if rule.get("method") != method:
        return False
    rp = rule.get("path") or ""
    return rp == path or rp == "/*" or (rp.endswith("/*") and (path.startswith(rp[:-1]) or path == rp[:-2]))


def _days_left(v) -> float | None:
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return (dt - datetime.now(timezone.utc)).total_seconds() / 86400


def telegram_request(cfg: Config, method: str, payload: dict) -> tuple[bool, str]:
    """Chiamata all'API Telegram che non espone mai il token negli errori."""
    try:
        r = requests.post(f"https://api.telegram.org/bot{cfg.telegram_token}/{method}", json=payload, timeout=10)
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        return False, f"Telegram non raggiungibile ({type(e).__name__})"
    if data.get("ok"):
        return True, ""
    return False, f"Telegram: {data.get('description') or 'HTTP ' + str(r.status_code)}"


def readiness(cfg: Config, client=None, offers: list[dict] | None = None,
              attempts: list[dict] | None = None) -> dict:
    """Controlli preventivi. Non solleva eccezioni: gli errori temporanei sono marcati `transient`."""
    from .orderer import make_client

    checks: list[dict] = []
    transient_err = False

    def add(cid: str, label: str, status: str, detail: str) -> None:
        checks.append({"id": cid, "label": label, "status": status, "detail": detail})

    # 0) cartella dati scrivibile: senza, il lock dopo un ordine non sopravvive a un riavvio
    from .state import State

    err = State(cfg.data_dir).writable_error()
    if err:
        add("data", "Cartella dati", "fail", f"{err}: configurazione e lock non vengono salvati. "
                                              "Su Unraid: chown -R 99:100 /mnt/user/appdata/ovh-ks-sniper")
    else:
        add("data", "Cartella dati", "ok", f"{cfg.data_dir} scrivibile")

    def api_error(cid: str, label: str, e: Exception) -> None:
        nonlocal transient_err
        if transient(e):
            transient_err = True
            add(cid, label, "warn", f"non verificabile ora (errore temporaneo {type(e).__name__})")
        elif isinstance(e, (ovhx.InvalidCredential, ovhx.InvalidKey, ovhx.NotCredential)):
            add(cid, label, "fail", f"credenziali rifiutate da OVH ({type(e).__name__})")
        else:
            add(cid, label, "warn", f"non verificabile ({type(e).__name__})")

    # 1) credenziali presenti
    missing = [n for n, v in (("Application Key", cfg.app_key), ("Application Secret", cfg.app_secret),
                              ("Consumer Key", cfg.consumer_key)) if not v]
    if missing:
        add("credentials", "Chiavi API OVH", "fail", "mancano: " + ", ".join(missing))
    else:
        add("credentials", "Chiavi API OVH", "ok", "presenti")
        client = client or make_client(cfg)

        # 2) consumer key: convalidata, non in scadenza, con i permessi necessari
        try:
            cred = client.get("/auth/currentCredential") or {}
            status, days = cred.get("status"), _days_left(cred.get("expiration"))
            rules = cred.get("rules") or []
            lacking = [f"{m} {p}" for m, p in CK_RULES if not any(_rule_covers(r, m, p) for r in rules)]
            if status != "validated":
                add("consumer_key", "Consumer key", "fail", f"stato '{status}': convalidala dal link OVH")
            elif lacking:
                add("consumer_key", "Consumer key", "fail", "permessi mancanti: " + ", ".join(lacking))
            elif days is not None and days <= 0:
                add("consumer_key", "Consumer key", "fail", "scaduta: generane una nuova")
            elif days is not None and days < 30:
                add("consumer_key", "Consumer key", "warn",
                    f"scade tra {days:.0f} giorni: rigenerala con validità Unlimited")
            else:
                add("consumer_key", "Consumer key", "ok",
                    "convalidata, " + ("validità illimitata" if days is None else f"scade tra {days:.0f} giorni"))
        except Exception as e:  # noqa: BLE001 - un controllo non deve mai far cadere il ciclo
            api_error("consumer_key", "Consumer key", e)

        # 3) profilo account completo e filiale coerente con il listino usato
        try:
            me = client.get("/me") or {}
            sub = me.get("ovhSubsidiary")
            if me.get("state") == "incomplete":
                add("account", "Profilo account OVH", "fail",
                    "profilo incompleto: completalo nel Manager, altrimenti l'ordine fallisce")
            elif sub and sub != cfg.subsidiary:
                add("account", "Profilo account OVH", "fail",
                    f"l'account è della filiale {sub} ma la configurazione usa {cfg.subsidiary}")
            else:
                add("account", "Profilo account OVH", "ok", f"completo, filiale {sub or '?'}")
        except Exception as e:  # noqa: BLE001 - un controllo non deve mai far cadere il ciclo
            api_error("account", "Profilo account OVH", e)

        # 4) metodo di pagamento predefinito valido e non scaduto
        try:
            ids = client.get("/me/payment/method", default=True)
            if not ids:
                add("payment", "Metodo di pagamento", "fail",
                    "nessun metodo PREDEFINITO: impostalo nel Manager (Metodi di pagamento)")
            else:
                pm = client.get(f"/me/payment/method/{ids[0]}") or {}
                desc = " ".join(x for x in (pm.get("paymentType"), str(pm.get("label") or "")[-4:]) if x)
                days = _days_left(pm.get("expirationDate"))
                if pm.get("status") != "VALID":
                    add("payment", "Metodo di pagamento", "fail", f"{desc}: stato {pm.get('status')}")
                elif days is not None and days <= 0:
                    add("payment", "Metodo di pagamento", "fail", f"{desc}: scaduto")
                elif days is not None and days < 45:
                    add("payment", "Metodo di pagamento", "warn", f"{desc}: scade tra {days:.0f} giorni")
                else:
                    add("payment", "Metodo di pagamento", "ok", f"{desc}: valido")
        except Exception as e:  # noqa: BLE001 - un controllo non deve mai far cadere il ciclo
            api_error("payment", "Metodo di pagamento", e)

        # 5) debiti scaduti (OVH può bloccare nuovi ordini)
        try:
            bal = client.get("/me/debtAccount") or {}
            due = (bal.get("dueAmount") or {}).get("value") or 0
            if bal.get("active") and due > 0:
                add("debt", "Debiti sull'account", "warn", f"importo scaduto {due:.2f} €: può bloccare nuovi ordini")
            else:
                add("debt", "Debiti sull'account", "ok", "nessun importo scaduto")
        except Exception as e:  # noqa: BLE001 - un controllo non deve mai far cadere il ciclo
            api_error("debt", "Debiti sull'account", e)

    # 6) Telegram
    if cfg.telegram_enabled:
        ok, msg = telegram_request(cfg, "getChat", {"chat_id": cfg.telegram_chat_id})
        add("telegram", "Notifiche Telegram", "ok" if ok else "fail",
            "bot e chat ID validi" if ok else msg + " (hai premuto Avvia sul bot? Chat ID corretto?)")
    elif cfg.telegram_token or cfg.telegram_chat_id:
        add("telegram", "Notifiche Telegram", "warn", "servono sia il bot token sia il chat ID")
    else:
        add("telegram", "Notifiche Telegram", "skip", "non configurate")

    # 7) esiste almeno una configurazione che rispetta i requisiti?
    if offers:
        good = [o for o in offers if o.get("ok")]
        if good:
            best = min(good, key=lambda o: o.get("monthly") or 1e9)
            add("requirements", "Requisiti raggiungibili", "ok",
                f"{len(good)} configurazione/i idonea/e, la più economica {best.get('monthly')} €/mese")
        else:
            add("requirements", "Requisiti raggiungibili", "fail",
                "nessuna configurazione rispetta storage e prezzi: non ordinerà mai")
    else:
        add("requirements", "Requisiti raggiungibili", "skip", "in attesa del primo controllo disponibilità")

    # 8) test carrello
    tests = [a for a in attempts or [] if a.get("test")]
    if not tests:
        add("test", "Test carrello", "warn", "mai eseguito: usa “Avvia test” prima di passare al reale")
    elif tests[-1].get("kind") == "dry_run":
        add("test", "Test carrello", "ok",
            "ultimo test riuscito il " + datetime.fromtimestamp(tests[-1]["ts"]).strftime("%d/%m %H:%M"))
    else:
        add("test", "Test carrello", "fail", f"ultimo test fallito: {tests[-1].get('message')}")

    fails = [c for c in checks if c["status"] == "fail"]
    critical = [c for c in fails if c["id"] in CRITICAL_CHECKS]
    return {"checks": checks, "checked_at": time.time(), "transient": transient_err,
            "ok": not fails, "ready": not critical and not transient_err,
            "critical": [f"{c['label']}: {c['detail']}" for c in critical]}


def account_summary(r: dict) -> dict:
    """Sintesi per la card 'Account OVH' della dashboard."""
    crit = [c for c in r["checks"] if c["id"] in CRITICAL_CHECKS]
    bad = [c for c in crit if c["status"] == "fail"]
    warn = [c for c in crit if c["status"] == "warn"]
    shown = bad or warn
    msg = ("; ".join(f"{c['label']}: {c['detail']}" for c in shown) if shown
           else "Credenziali, permessi e pagamento a posto")
    return {"ok": not bad and not r.get("transient"), "message": msg, "checked_at": r["checked_at"]}


def find_recent_orders(client, since_ts: float) -> list[int]:
    """Ordini creati dopo since_ts (per ricostruire un checkout dall'esito incerto)."""
    since = datetime.fromtimestamp(since_ts - 120, timezone.utc).isoformat(timespec="seconds")
    return list(client.get("/me/order", **{"date.from": since}) or [])


def request_consumer_key(cfg: Config) -> dict:
    if not (cfg.app_key and cfg.app_secret):
        raise ValueError("Salva prima Application Key e Application Secret")
    client = ovh.Client(endpoint=cfg.endpoint, application_key=cfg.app_key, application_secret=cfg.app_secret)
    req = client.new_consumer_key_request()
    for method, path in CK_RULES + CK_EXTRA_RULES:
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
