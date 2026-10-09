"""Catalogo pubblico Eco (/order/catalog/public/eco): specifiche dischi e prezzi.

Struttura verificata sull'API reale (ottobre 2026):
- plans[].addonFamilies[] = {name: "storage"|"memory"|"bandwidth"|..., mandatory, default, addons: [planCode addon]}
- addons[] = {planCode, product, invoiceName, pricings[]}
- products[] = {name, blobs.technical.storage.disks[] = {number, capacity (GB), ...}}
- pricings[].price / .tax sono interi in 1e-8 della valuta (es. 2399000000 = 23,99), tax = IVA.
- Il campo `storage`/`memory` delle availabilities coincide con addons[].product.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field

import requests

log = logging.getLogger(__name__)

PRICE_UNIT = 100_000_000
# Famiglie i cui addon vengono scelti in base ai campi della risposta availabilities.
AVAILABILITY_FIELDS = {"memory": "memory", "storage": "storage"}


@dataclass
class Candidate:
    plan_code: str
    fqn: str
    addons: dict[str, str]  # famiglia -> planCode addon
    storage_tb: float | None = None
    monthly_ttc: float | None = None
    setup_ttc: float | None = None
    reasons: list[str] = field(default_factory=list)  # motivi di scarto (vuoto = ok)

    @property
    def ok(self) -> bool:
        return not self.reasons

    @property
    def est_first_ttc(self) -> float | None:
        if self.monthly_ttc is None or self.setup_ttc is None:
            return None
        return round(self.monthly_ttc + self.setup_ttc, 2)

    def summary(self) -> str:
        st = f"{self.storage_tb:g} TB" if self.storage_tb is not None else "storage ?"
        mo = f"{self.monthly_ttc:.2f}€/mese" if self.monthly_ttc is not None else "canone ?"
        fi = f"primo pagamento stimato {self.est_first_ttc:.2f}€" if self.est_first_ttc is not None else "primo pagamento ?"
        return f"{self.fqn} [{st}, {mo} IVA incl., {fi}]"


def _pricing(pricings: list[dict], capacity: str, mode: str = "default") -> dict | None:
    for p in pricings or []:
        if p.get("mode") != mode or capacity not in (p.get("capacities") or []):
            continue
        if capacity == "renew" and not (p.get("interval") == 1 and p.get("intervalUnit") == "month"):
            continue
        return p
    return None


def _ttc(p: dict | None) -> float | None:
    if p is None:
        return None
    price, tax = p.get("price"), p.get("tax")
    if not isinstance(price, int) or not isinstance(tax, int):
        return None
    return (price + tax) / PRICE_UNIT


class Catalog:
    def __init__(self, data: dict):
        self.currency = (data.get("locale") or {}).get("currencyCode")
        self.plans = {p["planCode"]: p for p in data.get("plans", [])}
        self.addons = {a["planCode"]: a for a in data.get("addons", [])}
        self.products = {p["name"]: p for p in data.get("products", [])}

    def config_values(self, plan_code: str) -> dict[str, list[str]]:
        """Label di configurazione dichiarate dal catalogo per il piano -> valori ammessi."""
        plan = self.plans.get(plan_code) or {}
        return {c["name"]: list(c.get("values") or []) for c in plan.get("configurations") or []}

    def addon_default(self, plan_code: str, family: str) -> str | None:
        for fam in (self.plans.get(plan_code) or {}).get("addonFamilies") or []:
            if fam.get("name") == family:
                return fam.get("default")
        return None

    # --- storage -------------------------------------------------------------------
    def storage_tb(self, addon_plan_code: str) -> tuple[float | None, str]:
        """Somma dei dischi in TB (decimali) letta dalle specifiche tecniche del catalogo.

        Ritorna (None, motivo) se il valore non è determinabile con certezza. Per sicurezza
        le specifiche strutturate vengono confrontate con la descrizione commerciale
        (invoiceName, es. "4x HDD SATA 4TB ... + 1x SSD NVMe 500GB"): devono concordare.
        """
        addon = self.addons.get(addon_plan_code)
        if not addon:
            return None, f"addon {addon_plan_code} assente dal catalogo"
        product = self.products.get(addon.get("product") or "")
        technical = ((product or {}).get("blobs") or {}).get("technical") or {}
        disks = (technical.get("storage") or {}).get("disks")
        if not disks:
            return None, f"specifiche dischi assenti per {addon_plan_code}"

        invoice = re.sub(r"\s+", "", (addon.get("invoiceName") or "")).lower()
        total_gb = 0.0
        for d in disks:
            n, cap = d.get("number"), d.get("capacity")
            if not isinstance(n, int) or n <= 0 or not isinstance(cap, (int, float)) or cap <= 0:
                return None, f"specifiche dischi non valide per {addon_plan_code}: {d}"
            # capacity è in GB: verifica incrociata con la descrizione commerciale
            labels = {f"{cap:g}gb", f"{cap / 1000:g}tb"}
            if not invoice or not any(f"{n}x" in invoice and lbl in invoice for lbl in labels):
                return None, f"capacità disco {n}x{cap}GB non confermata da '{addon.get('invoiceName')}'"
            total_gb += n * cap
        return total_gb / 1000, ""

    # --- valutazione di una combinazione disponibile ------------------------------
    def evaluate(self, entry: dict, min_storage_tb: float, max_monthly: float, max_first: float) -> Candidate:
        plan_code = entry.get("planCode", "")
        cand = Candidate(plan_code=plan_code, fqn=entry.get("fqn") or plan_code, addons={})
        plan = self.plans.get(plan_code)
        if not plan:
            cand.reasons.append(f"piano {plan_code} non presente nel catalogo {self.currency}")
            return cand
        if self.currency != "EUR":
            cand.reasons.append(f"valuta catalogo {self.currency} != EUR")

        wanted_products = {fam: entry.get(fld) for fam, fld in AVAILABILITY_FIELDS.items() if entry.get(fld)}
        extra_products = {entry.get(k) for k in ("systemStorage", "gpu") if entry.get(k)}

        for fam in plan.get("addonFamilies") or []:
            name, options = fam.get("name"), fam.get("addons") or []
            if not fam.get("mandatory"):
                continue
            chosen = None
            if name in wanted_products:
                chosen = next((a for a in options if (self.addons.get(a) or {}).get("product") == wanted_products[name]), None)
                if not chosen:
                    cand.reasons.append(f"nessun addon '{name}' corrisponde a {wanted_products[name]}")
                    continue
            else:
                chosen = next((a for a in options if (self.addons.get(a) or {}).get("product") in extra_products), None)
                chosen = chosen or fam.get("default") or (options[0] if len(options) == 1 else None)
                if not chosen:
                    cand.reasons.append(f"famiglia obbligatoria '{name}' senza scelta univoca")
                    continue
            cand.addons[name] = chosen

        if "storage" not in cand.addons:
            cand.reasons.append("addon storage non determinato")
        else:
            tb, why = self.storage_tb(cand.addons["storage"])
            cand.storage_tb = tb
            if tb is None:
                cand.reasons.append(why)
            elif tb + 1e-9 < min_storage_tb:
                cand.reasons.append(f"storage {tb:g} TB < {min_storage_tb:g} TB")

        monthly = _ttc(_pricing(plan.get("pricings"), "renew"))
        setup = _ttc(_pricing(plan.get("pricings"), "installation"))
        for a in cand.addons.values():
            pr = (self.addons.get(a) or {}).get("pricings")
            m = _ttc(_pricing(pr, "renew"))
            s = _ttc(_pricing(pr, "installation"))
            monthly = None if (monthly is None or m is None) else monthly + m
            setup = None if setup is None else setup + (s or 0.0)
        cand.monthly_ttc = round(monthly, 2) if monthly is not None else None
        cand.setup_ttc = round(setup, 2) if setup is not None else None
        if cand.monthly_ttc is None:
            cand.reasons.append("canone mensile non determinabile dal catalogo")
        elif cand.monthly_ttc > max_monthly + 1e-9:
            cand.reasons.append(f"canone {cand.monthly_ttc:.2f}€ > {max_monthly:.2f}€")
        if cand.est_first_ttc is not None and cand.est_first_ttc > max_first + 1e-9:
            cand.reasons.append(f"primo pagamento stimato {cand.est_first_ttc:.2f}€ > {max_first:.2f}€")
        return cand


class CatalogProvider:
    """Scarica e tiene in cache il catalogo pubblico (nessuna autenticazione richiesta)."""

    def __init__(self, api_base: str, subsidiary: str, refresh_s: int, session: requests.Session):
        self.url = f"{api_base}/order/catalog/public/eco"
        self.subsidiary = subsidiary
        self.refresh_s = refresh_s
        self.session = session
        self._catalog: Catalog | None = None
        self._loaded_at = 0.0
        self._lock = threading.Lock()

    def get(self) -> Catalog:
        with self._lock:
            if self._catalog is None:
                self._load()
            return self._catalog  # type: ignore[return-value]

    def refresh_if_stale(self) -> None:
        if time.monotonic() - self._loaded_at < self.refresh_s:
            return
        try:
            with self._lock:
                self._load()
        except Exception as e:  # il catalogo vecchio resta valido
            log.warning("Aggiornamento catalogo fallito: %s", e)

    def _load(self) -> None:
        r = self.session.get(self.url, params={"ovhSubsidiary": self.subsidiary}, timeout=30)
        r.raise_for_status()
        self._catalog = Catalog(r.json())
        self._loaded_at = time.monotonic()
        log.info("Catalogo Eco %s caricato (%d piani)", self.subsidiary, len(self._catalog.plans))
