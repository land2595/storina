"""Monitor disponibilità OVH Eco + ordine automatico.

Uso:
  python -m app.main               ciclo 24/7
  python -m app.main --check       verifica catalogo/disponibilità (e credenziali se presenti), poi esce
  python -m app.main --test-cart [dc]
                                   esegue il flusso carrello + anteprima checkout anche senza stock
                                   (mai il checkout finale), poi esce
"""
from __future__ import annotations

import logging
import random
import signal
import sys
import threading
import time
from datetime import datetime

import requests

from . import config as config_mod
from . import logsetup
from .availability import AvailabilityClient, Offer, RateLimited, available_offers
from .catalog import CatalogProvider
from .notifier import Notifier
from .state import State

log = logging.getLogger("ovh-ks-sniper")
_stop = False
_stop_event = threading.Event()


def _on_signal(signum, _frame):
    global _stop
    _stop = True
    _stop_event.set()
    log.info("Segnale %s ricevuto, arresto...", signum)


def sleep_with_heartbeat(state: State, seconds: float) -> None:
    end = time.monotonic() + seconds
    while not _stop:
        state.beat()
        left = end - time.monotonic()
        if left <= 0:
            return
        _stop_event.wait(min(15, left))


def idle_while(state: State, predicate, what: str) -> None:
    """Resta fermo (healthy) finché predicate() è vero, es. lock o halt presenti."""
    log.warning("In pausa: %s. Rimuovi il file per riprendere.", what)
    while not _stop and predicate():
        sleep_with_heartbeat(state, 60)
    if not _stop:
        log.warning("%s rimosso: riprendo il monitoraggio.", what)


def _transient(e: Exception) -> bool:
    """Errore di rete/5xx: va ritentato, non è un problema di credenziali."""
    import ovh.exceptions as ovhx

    if isinstance(e, (ovhx.HTTPError, ovhx.NetworkError, ovhx.InvalidResponse)):
        return True
    status = getattr(getattr(e, "response", None), "status_code", None)
    return status is None or status >= 500


def check_account(cfg, client, notifier: Notifier) -> bool:
    """Verifica credenziali e metodo di pagamento predefinito. False = non si può ordinare.

    Gli errori transitori (rete assente all'avvio, 5xx) vengono rilanciati: il chiamante riprova.
    """
    import ovh.exceptions as ovhx

    try:
        client.get("/me")
        log.info("Autenticazione API OVH OK")
    except ovhx.APIError as e:
        if _transient(e):
            raise
        log.error("Autenticazione API OVH fallita: %s", type(e).__name__)
        return False
    try:
        methods = client.get("/me/payment/method", default=True)
    except ovhx.APIError as e:
        if _transient(e):
            raise
        log.error("Lettura metodi di pagamento fallita: %s", type(e).__name__)
        return False
    if not methods:
        log.error("Nessun metodo di pagamento PREDEFINITO sull'account: l'ordine automatico non può essere pagato.")
        return False
    log.info("Metodo di pagamento predefinito presente")
    return True


def run_check(cfg, catalogs: CatalogProvider, avail: AvailabilityClient) -> int:
    cat = catalogs.get()
    for plan in cfg.plan_codes:
        entries = avail.fetch(plan)
        print(f"\n=== {plan}: {len(entries)} combinazioni ===")
        if plan not in cat.plans:
            print(f"  ATTENZIONE: {plan} non è nel catalogo Eco {cfg.subsidiary}")
        for e in entries:
            cand = cat.evaluate(e, cfg.min_storage_tb, cfg.max_monthly_price, cfg.max_first_payment)
            verdict = "OK" if cand.ok else "SCARTATA: " + "; ".join(cand.reasons)
            print(f"  {cand.summary()}\n    -> {verdict}")
            dcs = ", ".join(f"{d['datacenter']}={d['availability']}" for d in e.get("datacenters") or [])
            print(f"    disponibilità: {dcs}")
    if cfg.has_credentials:
        from .orderer import make_client

        print()
        ok = check_account(cfg, make_client(cfg), Notifier("", ""))
        print("Account pronto per ordinare" if ok else "Account NON pronto per ordinare (vedi errori sopra)")
    else:
        print("\nCredenziali OVH non impostate: salto la verifica dell'account.")
    return 0


def run_test_cart(cfg, catalogs, avail, state, dc: str | None) -> int:
    from .orderer import CartManager, Orderer, make_client

    if not cfg.has_credentials:
        log.error("--test-cart richiede OVH_APPLICATION_KEY, OVH_APPLICATION_SECRET e OVH_CONSUMER_KEY")
        return 2
    client = make_client(cfg)
    cat = catalogs.get()
    dc = (dc or cfg.datacenters[0]).lower()
    for plan in cfg.plan_codes:
        for e in avail.fetch(plan):
            cand = cat.evaluate(e, cfg.min_storage_tb, cfg.max_monthly_price, cfg.max_first_payment)
            if not cand.ok:
                continue
            offer = Offer(entry=e, datacenter=dc, availability="test")
            log.info("Test carrello (senza stock, nessun checkout): %s", offer)
            orderer = Orderer(cfg, client, catalogs, avail, state, CartManager(cfg, client))
            res = orderer.attempt(offer, cand, force_dry_run=True)
            log.info("Esito test: %s — %s", res.kind, res.message)
            return 0 if res.kind == "dry_run" else 1
    log.error("Nessuna combinazione rispetta i requisiti nel catalogo: niente da testare")
    return 1


def main(argv: list[str]) -> int:
    try:
        cfg = config_mod.load()
    except config_mod.ConfigError as e:
        print(f"Errore di configurazione: {e}", file=sys.stderr)
        return 2

    logsetup.setup(cfg.data_dir, cfg.secrets)
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    state = State(cfg.data_dir)
    session = requests.Session()
    session.headers["User-Agent"] = "ovh-ks-sniper/1.0"
    catalogs = CatalogProvider(cfg.api_base, cfg.subsidiary, cfg.catalog_refresh, session)
    avail = AvailabilityClient(cfg.api_base, session)

    if "--check" in argv:
        return run_check(cfg, catalogs, avail)
    if "--test-cart" in argv:
        i = argv.index("--test-cart")
        return run_test_cart(cfg, catalogs, avail, state, argv[i + 1] if len(argv) > i + 1 else None)

    mode = "DRY-RUN" if cfg.dry_run else "LIVE"
    notifier = Notifier(cfg.telegram_token, cfg.telegram_chat_id, prefix=f"[OVH {mode}] ")
    log.info("Avvio: %s", cfg)
    if not cfg.dry_run:
        log.warning("MODALITÀ LIVE: al primo restock valido verrà effettuato un ordine REALE con pagamento automatico.")

    stats = {"start": datetime.now(), "polls": 0, "errors": 0, "last_error": "", "restocks": 0}
    while not _stop:
        state.beat()
        if state.is_locked():
            info = state.lock() or {}
            log.warning("Lock ordine presente (%s): nessun nuovo ordine.", info)
            idle_while(state, state.is_locked, str(state.lock_path))
            continue
        if state.halt():
            idle_while(state, lambda: state.halt() is not None, str(state.halt_path))
            continue
        try:
            loop(cfg, state, session, catalogs, avail, notifier, stats)
        except _Halt as h:
            state.write_halt(str(h))
            notifier.send(f"⛔ Fermo: {h}")
            log.error("Fermo per errore bloccante: %s", h)
            sleep_with_heartbeat(state, 60)  # nel caso il file halt non sia scrivibile
        except _Locked:
            pass
        except Exception:
            log.exception("Errore inatteso nel ciclo principale: riparto tra 60s")
            sleep_with_heartbeat(state, 60)
    notifier.flush()
    return 0


class _Halt(Exception):
    pass


class _Locked(Exception):
    pass


def loop(cfg, state: State, session, catalogs: CatalogProvider, avail: AvailabilityClient,
         notifier: Notifier, stats: dict) -> None:
    """Ciclo di polling. Esce con _Halt (errore bloccante) o _Locked (ordine fatto)."""
    from .orderer import CartManager, Orderer, make_client

    client = carts = orderer = None
    if cfg.has_credentials:
        client = make_client(cfg)
        if not check_account(cfg, client, notifier) and not cfg.dry_run:
            raise _Halt("credenziali non valide o nessun metodo di pagamento predefinito")
        carts = CartManager(cfg, client)
        carts.ensure_ready()
        orderer = Orderer(cfg, client, catalogs, avail, state, carts)
    elif not cfg.dry_run:
        raise _Halt("DRY_RUN=false ma credenziali OVH mancanti")
    else:
        log.warning("Credenziali OVH assenti: solo monitoraggio (nessun carrello di prova).")

    try:
        catalogs.get()
    except Exception as e:
        log.warning("Catalogo non disponibile all'avvio (%s): riprovo al primo restock", e)

    notifier.send(f"Avviato. Piani {', '.join(cfg.plan_codes)}; DC {', '.join(cfg.datacenters)}; "
                  f"polling {cfg.poll_interval}s.")
    backoff = 0
    seen: set[tuple[str, str]] = set()
    rejected_seen: set[tuple[str, str]] = set()  # già loggate come non conformi
    cart_rejected: set[tuple[str, str]] = set()  # rifiutate dai controlli sul carrello
    dry_run_done: dict[tuple[str, str], float] = {}
    prep_failures = 0
    last_heartbeat_day = None

    while not _stop:
        state.beat()
        if state.is_locked():
            raise _Locked()

        # 1) Polling disponibilità -------------------------------------------------
        offers: list[Offer] = []
        try:
            for plan in cfg.plan_codes:
                offers += available_offers(avail.fetch(plan), cfg.datacenters)
            stats["polls"] += 1
            backoff = 0
        except RateLimited as e:
            backoff += 1
            wait = e.retry_after or min(cfg.poll_interval * 2 ** backoff, 900)
            log.warning("HTTP 429 dall'API disponibilità: attendo %.0fs", wait)
            sleep_with_heartbeat(state, wait + random.uniform(0, 5))
            continue
        except (requests.RequestException, ValueError) as e:
            backoff += 1
            stats["errors"] += 1
            stats["last_error"] = type(e).__name__
            wait = min(cfg.poll_interval * 2 ** backoff, 900)
            log.warning("Errore polling (%s): nuovo tentativo tra %.0fs", type(e).__name__, wait)
            sleep_with_heartbeat(state, wait + random.uniform(0, 5))
            continue

        current = {o.key for o in offers}
        new = [o for o in offers if o.key not in seen]
        gone = seen - current
        seen = current
        rejected_seen &= current
        cart_rejected &= current
        if not offers:
            prep_failures = 0  # "consecutivi" = all'interno dello stesso episodio di restock
        if gone:
            log.info("Non più disponibile: %s", ", ".join(f"{f} @ {d}" for f, d in gone))
        if new:
            stats["restocks"] += 1
            log.warning("RESTOCK: %s", "; ".join(map(str, new)))
            notifier.send("🟢 Restock: " + "; ".join(map(str, new)))

        # 2) Tentativo d'ordine sulle offerte valide ---------------------------------
        if offers:
            try:
                cat = catalogs.get()
            except Exception as e:
                log.error("Catalogo non disponibile, impossibile verificare i requisiti: %s", e)
                cat = None
            evaluated = []
            for o in offers if cat else []:
                cand = cat.evaluate(o.entry, cfg.min_storage_tb, cfg.max_monthly_price, cfg.max_first_payment)
                if cand.ok:
                    evaluated.append((o, cand))
                elif o.key not in rejected_seen:
                    rejected_seen.add(o.key)
                    log.info("Scartata %s: %s", o, "; ".join(cand.reasons))
            # preferenza DC (già ordinata), a parità il canone più basso
            pref = {dc: i for i, dc in enumerate(cfg.datacenters)}
            evaluated.sort(key=lambda oc: (pref[oc[0].datacenter], oc[1].monthly_ttc))

            for offer, cand in evaluated:
                if offer.key in cart_rejected:
                    continue
                if orderer is None:
                    log.warning("[DRY-RUN] Requisiti OK per %s, ma senza credenziali non posso preparare il carrello",
                                cand.summary())
                    cart_rejected.add(offer.key)
                    break
                if cfg.dry_run and time.time() - dry_run_done.get(offer.key, 0) < cfg.dry_run_cooldown:
                    continue
                log.warning("Tentativo d'ordine: %s @ %s", cand.summary(), offer.datacenter)
                res = orderer.attempt(offer, cand)
                log.info("Esito: %s — %s", res.kind, res.message)

                if res.kind == "ordered":
                    d = res.details
                    notifier.send(f"✅ ORDINE EFFETTUATO #{d.get('orderId')} — {res.message}. "
                                  f"Importo {d.get('amount_ttc')}€. {d.get('url') or ''}")
                    notifier.flush()
                    raise _Locked()
                if res.kind == "uncertain":
                    notifier.send(f"⚠️ {res.message}. Lock scritto per sicurezza.")
                    notifier.flush()
                    raise _Locked()
                if res.kind == "dry_run":
                    dry_run_done[offer.key] = time.time()
                    prep_failures = 0
                    notifier.send(f"🧪 Dry-run completato: avrei ordinato {res.message}")
                    break
                if res.kind == "fatal":
                    raise _Halt(res.message)
                if res.kind == "stock_gone":
                    notifier.send(f"❌ {res.message}")
                    continue
                if res.kind == "rejected":
                    notifier.send(f"🚫 Ordine non effettuato: {res.message}")
                    cart_rejected.add(offer.key)
                    continue
                if res.kind == "prep_error":
                    prep_failures += 1
                    notifier.send(f"⚠️ {res.message} ({prep_failures}/{cfg.max_prep_failures})")
                    if prep_failures >= cfg.max_prep_failures:
                        raise _Halt(f"{prep_failures} errori consecutivi di preparazione: {res.message}")
                    break

        # 3) Manutenzione fuori dal percorso critico ---------------------------------
        if carts is not None:
            carts.ensure_ready()
        catalogs.refresh_if_stale()

        now = datetime.now()
        if cfg.heartbeat and now.hour == cfg.heartbeat_hour and last_heartbeat_day != now.date():
            last_heartbeat_day = now.date()
            up = now - stats["start"]
            notifier.send(f"💓 Attivo da {up.days}g {up.seconds // 3600}h. Poll: {stats['polls']}, "
                          f"restock visti: {stats['restocks']}, errori: {stats['errors']}"
                          + (f" (ultimo: {stats['last_error']})" if stats["last_error"] else ""))

        sleep_with_heartbeat(state, cfg.poll_interval)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
