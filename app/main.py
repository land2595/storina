"""Monitor disponibilità OVH Eco + ordine automatico, con web UI.

Uso:
  python -m app.main               ciclo 24/7 + web UI (porta WEB_PORT, default 8765)
  python -m app.main --check       verifica catalogo/disponibilità (e credenziali se presenti), poi esce
  python -m app.main --test-cart [dc]
                                   esegue il flusso carrello + anteprima checkout anche senza stock
                                   (mai il checkout finale), poi esce
"""
from __future__ import annotations

import logging
import os
import random
import signal
import sys
import time
from datetime import datetime

import requests

from . import actions
from . import config as config_mod
from . import logsetup
from .availability import AvailabilityClient, Offer, RateLimited, available_offers
from .catalog import CatalogProvider
from .hub import Hub, HubLogHandler, Runtime
from .notifier import Notifier
from .state import State
from .tracker import FINAL, OrderTracker

log = logging.getLogger("ovh-ks-sniper")


class _Halt(Exception):
    pass


class _Locked(Exception):
    pass


class _Reload(Exception):
    pass


CHECKS_EVERY = 6 * 3600  # s tra due giri di controlli preventivi


def _track_from_lock(tracker: OrderTracker, lock: dict | None) -> None:
    """Riprende a seguire l'ordine indicato nel lock (anche dopo un riavvio)."""
    if not lock:
        return
    oid = lock.get("orderId") or lock.get("probable_orderId")
    if oid and (lock.get("payment") or {}).get("status") not in FINAL:
        tracker.start(oid, lock.get("ordered_at"))


def _session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = "ovh-ks-sniper/1.0"
    return s


# --- riga di comando -------------------------------------------------------------------
def run_check(cfg) -> int:
    catalogs = CatalogProvider(cfg.api_base, cfg.subsidiary, cfg.catalog_refresh, _session())
    avail = AvailabilityClient(cfg.api_base, _session())
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
        res = actions.check_account_safe(cfg)
        print(f"\n{res['message']}")
        print("Account pronto per ordinare" if res["ok"] else "Account NON pronto per ordinare")
    else:
        print("\nCredenziali OVH non impostate: salto la verifica dell'account.")
    return 0


def run_test_cart(cfg, state: State, dc: str | None) -> int:
    if not cfg.has_credentials:
        log.error("--test-cart richiede Application Key, Application Secret e Consumer Key")
        return 2
    catalogs = CatalogProvider(cfg.api_base, cfg.subsidiary, cfg.catalog_refresh, _session())
    _, _, res = actions.test_cart(cfg, catalogs, AvailabilityClient(cfg.api_base, _session()), state, dc)
    return 0 if res.kind == "dry_run" else 1


# --- servizio ----------------------------------------------------------------------------
def main(argv: list[str]) -> int:
    data_dir = config_mod.data_dir()
    hub = Hub(data_dir)
    try:
        cfg = config_mod.load()
        cfg_error = None
    except config_mod.ConfigError as e:
        cfg, cfg_error = None, str(e)
    redact = logsetup.setup(data_dir, cfg.secrets if cfg else [], extra=[HubLogHandler(hub)])
    state = State(data_dir)

    if "--check" in argv or "--test-cart" in argv:
        if cfg is None:
            print(f"Errore di configurazione: {cfg_error}", file=sys.stderr)
            return 2
        if "--check" in argv:
            return run_check(cfg)
        i = argv.index("--test-cart")
        return run_test_cart(cfg, state, argv[i + 1] if len(argv) > i + 1 else None)

    rt = Runtime(hub, state)
    signal.signal(signal.SIGTERM, lambda *_: rt.request_stop())
    signal.signal(signal.SIGINT, lambda *_: rt.request_stop())

    if (os.getenv("WEB_ENABLED") or "true").strip().lower() not in ("0", "false", "no", "off"):
        from . import web

        web.start(rt, int(os.getenv("WEB_PORT") or 8765))

    notifier = Notifier()
    tracker = OrderTracker(rt, notifier)
    rt.tracker = tracker
    stats = {"start": datetime.now(), "polls": 0, "errors": 0, "last_error": "", "restocks": 0}
    while not rt.stop:
        rt.reload_requested = False
        try:
            cfg = config_mod.load()
        except config_mod.ConfigError as e:
            log.error("Configurazione non valida: %s. Correggila dalla web UI o nel .env.", e)
            hub.update(state="config_error", error=str(e))
            _idle(rt, lambda: True)
            continue
        rt.cfg = cfg
        redact.secrets[:] = cfg.secrets
        mode = "DRY-RUN" if cfg.dry_run else "LIVE"
        notifier.configure(cfg.telegram_token, cfg.telegram_chat_id, prefix=f"[OVH {mode}] ")
        hub.update(mode=mode, paused=cfg.paused, error=None, lock=state.lock(), halt=state.halt(),
                   datacenters=cfg.datacenters, plans=cfg.plan_codes, poll_interval=cfg.poll_interval)
        log.info("Configurazione: %s", cfg)

        if state.is_locked():
            info = state.lock() or {}
            log.warning("Lock ordine presente (%s): nessun nuovo ordine.", info)
            hub.update(state="locked", lock=info)
            if cfg.has_credentials:
                _track_from_lock(tracker, info)
            _idle(rt, state.is_locked, f"{state.lock_path} presente")
            hub.update(lock=None)
            continue
        if state.halt():
            hub.update(state="halted", halt=state.halt())
            _idle(rt, lambda: state.halt() is not None, f"{state.halt_path} presente")
            hub.update(halt=None)
            continue
        if cfg.paused:
            log.info("Monitoraggio in pausa (impostazione PAUSED)")
            hub.update(state="paused")
            _idle(rt, lambda: True)
            continue
        if not cfg.dry_run:
            log.warning("MODALITÀ LIVE: al primo restock valido verrà effettuato un ordine REALE con pagamento automatico.")
        try:
            loop(cfg, rt, notifier, stats)
        except _Reload:
            log.info("Configurazione cambiata: ricarico")
        except _Halt as h:
            state.write_halt(str(h))
            notifier.send(f"⛔ Fermo: {h}")
            log.error("Fermo per errore bloccante: %s", h)
            hub.update(state="halted", halt=state.halt(), attempt=None)
            rt.sleep(60)  # nel caso il file halt non sia scrivibile
        except _Locked:
            hub.update(attempt=None)
            if cfg.has_credentials:
                _track_from_lock(tracker, state.lock())
        except Exception:
            log.exception("Errore inatteso nel ciclo principale: riparto tra 60s")
            hub.update(state="error", attempt=None)
            rt.sleep(60)
    notifier.flush()
    return 0


def _idle(rt: Runtime, predicate, what: str = "") -> None:
    """Resta fermo (healthy) finché predicate() è vero o finché arriva un reload/stop."""
    if what:
        log.warning("In pausa: %s. Rimuovilo (anche dalla web UI) per riprendere.", what)
    while not rt.stop and not rt.reload_requested and predicate():
        rt.poll_now = False
        rt.sleep(60)


def loop(cfg, rt: Runtime, notifier: Notifier, stats: dict) -> None:
    """Ciclo di polling. Esce con _Halt (errore bloccante), _Locked (ordine fatto) o _Reload."""
    from .orderer import CartManager, Orderer, make_client

    hub, state = rt.hub, rt.state
    session = _session()
    catalogs = CatalogProvider(cfg.api_base, cfg.subsidiary, cfg.catalog_refresh, session)
    avail = AvailabilityClient(cfg.api_base, session)
    rt.catalogs, rt.avail = catalogs, avail
    hub.update(state="avvio", attempt=None)

    carts = orderer = None
    last_fails: set[str] = set()
    last_logged: set[str] = set()

    def run_checks(first: bool = False) -> None:
        """Controlli preventivi: in LIVE un problema critico ferma tutto prima che si arrivi a ordinare."""
        nonlocal last_fails, last_logged
        r = actions.readiness(cfg, client, offers=hub.snapshot().get("offers"), attempts=hub.attempts_list())
        hub.update(checks=r, account=actions.account_summary(r))
        problems = {f"{c['status']}|{c['label']}|{c['detail']}" for c in r["checks"] if c["status"] in ("fail", "warn")}
        for key in sorted(problems - last_logged):  # nei log solo le novità
            status, label, detail = key.split("|", 2)
            (log.error if status == "fail" else log.warning)("Controllo %s: %s", label, detail)
        if last_logged and not problems:
            log.info("Controlli: tutto a posto")
        last_logged = problems
        fails = {f"{c['label']}: {c['detail']}" for c in r["checks"] if c["status"] == "fail"}
        if fails - last_fails:
            notifier.send("🩺 Controlli non superati:\n• " + "\n• ".join(sorted(fails)))
        elif last_fails and not fails and not first:
            notifier.send("🩺 Tutti i controlli ora sono superati")
        last_fails = fails
        if not cfg.dry_run:
            if r["critical"]:
                raise _Halt("controlli non superati: " + "; ".join(r["critical"]))
            if r["transient"]:
                raise RuntimeError("controlli non completati per un errore temporaneo")

    client = make_client(cfg) if cfg.has_credentials else None
    run_checks(first=True)
    next_checks = time.time()  # ripetuti dopo il primo controllo disponibilità (per i requisiti)
    if client is not None:
        carts = CartManager(cfg, client)
        carts.ensure_ready()
        hub.update(cart=carts.info())

        def on_step(step: str) -> None:
            hub.update(attempt={**(hub.snapshot().get("attempt") or {}), "step": step})

        orderer = Orderer(cfg, client, catalogs, avail, state, carts, on_step=on_step)
    elif not cfg.dry_run:
        raise _Halt("DRY_RUN disattivato ma credenziali OVH mancanti")
    else:
        hub.update(cart=None)
        log.warning("Credenziali OVH assenti: solo monitoraggio (nessun carrello di prova).")

    try:
        catalogs.get()
    except Exception as e:
        log.warning("Catalogo non disponibile all'avvio (%s): riprovo al primo restock", e)

    notifier.send(f"Avviato. Piani {', '.join(cfg.plan_codes)}; DC {', '.join(cfg.datacenters)}; "
                  f"polling {cfg.poll_interval}s.")
    hub.update(state="monitoring")
    backoff = 0
    seen: set[tuple[str, str]] = set()
    rejected_seen: set[tuple[str, str]] = set()  # già loggate come non conformi
    cart_rejected: set[tuple[str, str]] = set()  # rifiutate dai controlli sul carrello
    dry_run_done: dict[tuple[str, str], float] = {}
    prep_failures = 0
    last_heartbeat_day = None

    while True:
        state.beat()
        if rt.stop:
            return
        if rt.reload_requested:
            raise _Reload()
        if state.is_locked():
            raise _Locked()
        rt.poll_now = False

        # 1) Polling disponibilità -------------------------------------------------
        offers: list[Offer] = []
        entries_all: list[dict] = []
        try:
            for plan in cfg.plan_codes:
                entries = avail.fetch(plan)
                entries_all += entries
                offers += available_offers(entries, cfg.datacenters)
            stats["polls"] += 1
            backoff = 0
        except RateLimited as e:
            backoff += 1
            wait = e.retry_after or min(cfg.poll_interval * 2 ** backoff, 900)
            log.warning("HTTP 429 dall'API disponibilità: attendo %.0fs", wait)
            hub.update(state="backoff", next_poll=time.time() + wait)
            rt.sleep(wait + random.uniform(0, 5))
            continue
        except (requests.RequestException, ValueError) as e:
            backoff += 1
            stats["errors"] += 1
            stats["last_error"] = type(e).__name__
            wait = min(cfg.poll_interval * 2 ** backoff, 900)
            log.warning("Errore polling (%s): nuovo tentativo tra %.0fs", type(e).__name__, wait)
            hub.update(state="backoff", next_poll=time.time() + wait, errors=stats["errors"],
                       last_error=stats["last_error"])
            rt.sleep(wait + random.uniform(0, 5))
            continue

        try:
            cat = catalogs.get()
        except Exception as e:
            log.error("Catalogo non disponibile, impossibile verificare i requisiti: %s", e)
            cat = None
        hub.update(state="monitoring", last_poll=time.time(), polls=stats["polls"], errors=stats["errors"],
                   offers=actions.matrix(cfg, cat, entries_all))

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
            hub.update(restocks=stats["restocks"], last_restock=time.time())
            log.warning("RESTOCK: %s", "; ".join(map(str, new)))
            notifier.send("🟢 Restock: " + "; ".join(map(str, new)))

        # 2) Tentativo d'ordine sulle offerte valide ---------------------------------
        if offers and cat is not None:
            evaluated = []
            for o in offers:
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
                hub.update(attempt={"fqn": cand.fqn, "dc": offer.datacenter, "step": "avvio",
                                    "started": time.time(), "dry_run": cfg.dry_run})
                with rt.order_mutex:
                    res = orderer.attempt(offer, cand)
                log.info("Esito: %s — %s", res.kind, res.message)
                hub.update(attempt=None, cart=carts.info() if carts else None)
                hub.add_attempt({"fqn": cand.fqn, "dc": offer.datacenter, "kind": res.kind,
                                 "message": res.message, "details": res.details, "dry_run": cfg.dry_run})

                if res.kind == "ordered":
                    d = res.details
                    notifier.send(f"✅ ORDINE EFFETTUATO #{d.get('orderId')} — {res.message}. "
                                  f"Importo {d.get('amount_ttc')}€. {d.get('url') or ''}")
                    notifier.flush()
                    hub.update(state="locked", lock=state.lock())
                    raise _Locked()
                if res.kind == "uncertain":
                    notifier.send(f"⚠️ {res.message}. Lock scritto per sicurezza.")
                    notifier.flush()
                    hub.update(state="locked", lock=state.lock())
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
        if rt.checks_requested or time.time() >= next_checks:
            rt.checks_requested = False
            next_checks = time.time() + CHECKS_EVERY
            run_checks()
        if carts is not None:
            carts.ensure_ready()
            hub.update(cart=carts.info())
        catalogs.refresh_if_stale()

        now = datetime.now()
        if cfg.heartbeat and now.hour == cfg.heartbeat_hour and last_heartbeat_day != now.date():
            last_heartbeat_day = now.date()
            up = now - stats["start"]
            notifier.send(f"💓 Attivo da {up.days}g {up.seconds // 3600}h. Poll: {stats['polls']}, "
                          f"restock visti: {stats['restocks']}, errori: {stats['errors']}"
                          + (f" (ultimo: {stats['last_error']})" if stats["last_error"] else ""))

        hub.update(next_poll=time.time() + cfg.poll_interval)
        rt.sleep(cfg.poll_interval)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
