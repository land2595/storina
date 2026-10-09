"""Flusso carrello -> configurazione -> anteprima checkout -> (checkout)."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable
from datetime import datetime, timedelta, timezone

import ovh
import ovh.exceptions as ovhx
import requests

from .availability import AvailabilityClient, Offer, still_available
from .catalog import Candidate, CatalogProvider
from .config import Config
from .state import State

log = logging.getLogger(__name__)

# Errori di autenticazione/permessi: inutile riprovare.
AUTH_ERRORS = (
    ovhx.InvalidKey,
    ovhx.InvalidCredential,
    ovhx.NotGrantedCall,
    ovhx.NotCredential,
    ovhx.Forbidden,
)
# Nessun codice d'errore documentato per "stock esaurito": si ricontrolla la disponibilità
# e, in subordine, si cercano queste parole nel messaggio.
STOCK_HINTS = ("stock", "unavailable", "not available", "no longer available", "indisponible",
               "plus disponible", "non disponibil", "out of")


@dataclass
class Result:
    kind: str  # ordered | dry_run | stock_gone | rejected | prep_error | fatal | uncertain
    message: str
    details: dict = field(default_factory=dict)


def make_client(cfg: Config) -> ovh.Client:
    return ovh.Client(
        endpoint=cfg.endpoint,
        application_key=cfg.app_key,
        application_secret=cfg.app_secret,
        consumer_key=cfg.consumer_key,
        timeout=(5, 30),
    )


def _status(e: Exception) -> int | None:
    resp = getattr(e, "response", None)
    return getattr(resp, "status_code", None)


def _err(e: Exception) -> str:
    # str() di APIError include già l'OVH-Query-ID, utile per il supporto OVH
    msg = " ".join(str(e).split()) or type(e).__name__
    return f"{type(e).__name__}: {msg}"


class CartManager:
    """Tiene pronto un carrello già creato e assegnato, ricreandolo prima della scadenza."""

    MARGIN = timedelta(minutes=30)

    def __init__(self, cfg: Config, client: ovh.Client):
        self.cfg = cfg
        self.client = client
        self.cart_id: str | None = None
        self.expire: datetime | None = None

    def info(self) -> dict | None:
        if not self.cart_id or not self.expire:
            return None
        return {"id": self.cart_id, "expire": self.expire.isoformat()}

    def create(self) -> str:
        expire = datetime.now(timezone.utc) + timedelta(hours=self.cfg.cart_ttl_hours)
        cart = self.client.post(
            "/order/cart",
            ovhSubsidiary=self.cfg.subsidiary,
            description="ovh-ks-sniper",
            expire=expire.isoformat(timespec="seconds"),
        )
        cart_id = cart["cartId"]
        self.client.post(f"/order/cart/{cart_id}/assign")
        log.info("Carrello %s creato e assegnato (scade %s)", cart_id, expire.astimezone().strftime("%d/%m %H:%M"))
        return cart_id

    def ensure_ready(self) -> None:
        now = datetime.now(timezone.utc)
        if self.cart_id and self.expire and self.expire - now > self.MARGIN:
            return
        old = self.cart_id
        self.cart_id, self.expire = None, None
        if old:
            self.delete(old)
        try:
            self.cart_id = self.create()
            self.expire = now + timedelta(hours=self.cfg.cart_ttl_hours)
        except Exception as e:
            log.warning("Preparazione carrello fallita (riproverò): %s", _err(e))

    def take(self) -> str:
        """Restituisce il carrello pronto (o ne crea uno al volo). Il chiamante lo consuma."""
        cart_id, self.cart_id, self.expire = self.cart_id, None, None
        return cart_id or self.create()

    def delete(self, cart_id: str) -> None:
        try:
            self.client.delete(f"/order/cart/{cart_id}")
        except Exception as e:
            log.debug("Eliminazione carrello %s fallita: %s", cart_id, _err(e))


class Orderer:
    def __init__(self, cfg: Config, client: ovh.Client, catalogs: CatalogProvider,
                 avail: AvailabilityClient, state: State, carts: CartManager,
                 on_step: Callable[[str], None] | None = None):
        self.cfg = cfg
        self.client = client
        self.catalogs = catalogs
        self.avail = avail
        self.state = state
        self.carts = carts
        self.on_step = on_step or (lambda step: None)

    # ------------------------------------------------------------------------------
    def attempt(self, offer: Offer, cand: Candidate, force_dry_run: bool = False) -> Result:
        dry_run = self.cfg.dry_run or force_dry_run
        t0 = time.monotonic()
        cart_id: str | None = None
        try:
            self.on_step("carrello")
            cart_id = self.carts.take()
            preview = self._build_cart(cart_id, offer, cand)
        except _Reject as r:
            self._drop(cart_id)
            return Result("rejected", str(r))
        except AUTH_ERRORS as e:
            self._drop(cart_id)
            return Result("fatal", f"Permessi/credenziali API: {_err(e)}")
        except Exception as e:
            self._drop(cart_id)
            if not force_dry_run and still_available(self.avail, offer) is False:
                return Result("stock_gone", f"Stock esaurito durante la preparazione ({_err(e)})")
            return Result("prep_error", f"Errore preparazione carrello: {_err(e)}")

        first = preview["first_payment"]
        elapsed = time.monotonic() - t0
        details = {
            "fqn": cand.fqn, "planCode": cand.plan_code, "datacenter": offer.datacenter,
            "storage_tb": cand.storage_tb, "monthly_ttc": cand.monthly_ttc, "first_payment_ttc": first,
            "cartId": cart_id, "prep_seconds": round(elapsed, 1),
        }
        desc = (f"{cand.fqn} @ {offer.datacenter}: {cand.storage_tb:g} TB, canone {cand.monthly_ttc:.2f}€, "
                f"primo pagamento {first:.2f}€ IVA incl.")

        if self.state.is_locked():
            self._drop(cart_id)
            return Result("rejected", "Lock presente: ordine annullato all'ultimo momento")

        if dry_run:
            log.warning("[DRY-RUN] Avrei ordinato: %s (carrello pronto in %.1fs)", desc, elapsed)
            self._drop(cart_id)
            return Result("dry_run", desc, details)

        # ---------------------------- CHECKOUT REALE -------------------------------
        self.on_step("checkout")
        log.warning("CHECKOUT: %s", desc)
        t_checkout = time.time()
        try:
            order = self.client.post(
                f"/order/cart/{cart_id}/checkout",
                autoPayWithPreferredPaymentMethod=True,
                waiveRetractationPeriod=self.cfg.waive_retractation,
            )
        except (ovhx.HTTPError, ovhx.NetworkError, ovhx.InvalidResponse, requests.RequestException) as e:
            return self._uncertain(details, desc, e, t_checkout)
        except ovhx.APIError as e:
            status = _status(e)
            if status is None or status >= 500:
                return self._uncertain(details, desc, e, t_checkout)
            self._drop(cart_id)
            if isinstance(e, AUTH_ERRORS):
                return Result("fatal", f"Checkout rifiutato (permessi): {_err(e)}", details)
            gone = still_available(self.avail, offer) is False
            if gone or any(h in str(e).lower() for h in STOCK_HINTS):
                return Result("stock_gone", f"Checkout fallito, stock esaurito: {_err(e)}", details)
            return Result("fatal", f"Checkout fallito: {_err(e)}", details)
        except Exception as e:  # qualunque altro imprevisto: l'ordine potrebbe essere partito
            return self._uncertain(details, desc, e, t_checkout)

        # Da qui l'ordine è stato accettato: il lock va scritto qualunque cosa contenga la risposta.
        order = order if isinstance(order, dict) else {}
        try:
            paid = ((order.get("prices") or {}).get("withTax") or {}).get("value")
        except AttributeError:
            paid = None
        details.update(orderId=order.get("orderId"), amount_ttc=paid, url=order.get("url"), ordered_at=t_checkout)
        self.state.write_lock({"status": "ordered", **details})
        return Result("ordered", desc, details)

    # ------------------------------------------------------------------------------
    def _uncertain(self, details: dict, desc: str, e: Exception, t_checkout: float) -> Result:
        # Non sappiamo se l'ordine è stato creato: blocchiamo tutto per non rischiare un doppione.
        self.state.write_lock({"status": "uncertain", "error": _err(e), "ordered_at": t_checkout, **details})
        msg = f"Esito checkout sconosciuto ({_err(e)}) per {desc}."
        # Proviamo a ritrovare l'ordine tra quelli appena creati sull'account.
        from .actions import find_recent_orders

        for attempt in range(3):
            try:
                ids = find_recent_orders(self.client, t_checkout)
            except Exception as err:
                log.warning("Ricerca ordini recenti fallita (%s), tentativo %d/3", type(err).__name__, attempt + 1)
                time.sleep(5)
                continue
            if len(ids) == 1:
                details["probable_orderId"] = ids[0]
                self.state.update_lock(probable_orderId=ids[0])
                msg += f" Trovato un ordine appena creato: #{ids[0]} (probabilmente questo)."
            elif ids:
                self.state.update_lock(recent_orders=ids)
                msg += f" Ordini creati negli ultimi minuti: {', '.join(map(str, ids))}."
            else:
                msg += " Nessun ordine nuovo trovato sull'account: probabilmente non è partito."
            break
        msg += " Verifica su https://www.ovh.com/manager/#/dedicated/billing/orders"
        return Result("uncertain", msg, details)

    def _drop(self, cart_id: str | None) -> None:
        if cart_id:
            self.carts.delete(cart_id)

    def _build_cart(self, cart_id: str, offer: Offer, cand: Candidate) -> dict:
        c, cfg = self.client, self.cfg
        catalog = self.catalogs.get()
        base = f"/order/cart/{cart_id}"

        self.on_step("server")
        item = c.post(f"{base}/eco", planCode=cand.plan_code, duration="P1M", pricingMode="default", quantity=1)
        item_id = item["itemId"]

        # Opzioni: devono esistere nel carrello e coprire tutte le famiglie obbligatorie.
        self.on_step("opzioni")
        options = c.get(f"{base}/eco/options", planCode=cand.plan_code)
        by_plan = {o.get("planCode"): o for o in options}
        mandatory = {o.get("family") for o in options if o.get("mandatory")}
        missing = mandatory - set(cand.addons)
        if missing:
            raise _Reject(f"famiglie obbligatorie non gestite: {sorted(missing)}")
        for family, addon in cand.addons.items():
            opt = by_plan.get(addon)
            if not opt:
                raise _Reject(f"opzione {addon} ({family}) non proposta dal carrello")
            if opt.get("family") and opt["family"] != family:
                raise _Reject(f"opzione {addon}: famiglia {opt['family']} invece di {family}")
            c.post(f"{base}/eco/options", itemId=item_id, planCode=addon, duration="P1M",
                   pricingMode="default", quantity=1)

        # Configurazioni: label verificate con requiredConfiguration + catalogo.
        self.on_step("configurazione")
        required = c.get(f"{base}/item/{item_id}/requiredConfiguration")
        known = {r.get("label") for r in required} | set(catalog.config_values(cand.plan_code))
        wanted = {
            "dedicated_datacenter": offer.datacenter,
            "dedicated_os": cfg.os_template,
            "region": "canada" if offer.datacenter == "bhs" else "europe",
        }
        unhandled = [r.get("label") for r in required if r.get("required") and r.get("label") not in wanted]
        if unhandled:
            raise _Reject(f"configurazioni obbligatorie sconosciute: {unhandled}")
        if "dedicated_datacenter" not in known:
            raise _Reject("label dedicated_datacenter non disponibile: impossibile scegliere il datacenter")
        allowed = catalog.config_values(cand.plan_code)
        for label, value in wanted.items():
            if label not in known:
                continue
            if allowed.get(label) and value not in allowed[label]:
                raise _Reject(f"valore {value} non ammesso per {label} (ammessi: {allowed[label]})")
            c.post(f"{base}/item/{item_id}/configuration", label=label, value=value)

        # Anteprima checkout: verifica prezzi reali (requisito 2) e storage (requisito 1).
        self.on_step("anteprima")
        preview = c.get(f"{base}/checkout")
        with_tax = (preview.get("prices") or {}).get("withTax") or {}
        first, currency = with_tax.get("value"), with_tax.get("currencyCode")
        if not isinstance(first, (int, float)) or currency != "EUR":
            raise _Reject(f"prezzo anteprima non leggibile: {with_tax}")
        if cand.storage_tb is None or cand.storage_tb + 1e-9 < cfg.min_storage_tb:
            raise _Reject(f"storage {cand.storage_tb} TB sotto la soglia")
        if cand.monthly_ttc is None or cand.monthly_ttc > cfg.max_monthly_price + 1e-9:
            raise _Reject(f"canone {cand.monthly_ttc}€ sopra la soglia")
        if first > cfg.max_first_payment + 1e-9:
            raise _Reject(f"primo pagamento {first:.2f}€ > {cfg.max_first_payment:.2f}€")
        est = cand.est_first_ttc
        if est is not None and first > est + 1.0:
            raise _Reject(f"anteprima {first:.2f}€ incoerente con il catalogo ({est:.2f}€): voci inattese nel carrello")
        log.info("Anteprima checkout: %.2f %s IVA incl. (%s)", first, currency,
                 "; ".join(f"{d.get('description')}: {((d.get('totalPrice') or {}).get('text'))}"
                           for d in preview.get("details") or []))
        return {"first_payment": float(first), "raw": preview}


class _Reject(Exception):
    """Requisito non soddisfatto: non si ordina (non è un errore tecnico)."""
