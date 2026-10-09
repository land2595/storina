"""Polling dell'endpoint pubblico /dedicated/server/datacenter/availabilities."""
from __future__ import annotations

from dataclasses import dataclass

import requests

# Valori dell'enum dedicated.AvailabilityEnum che NON indicano disponibilità.
# Lo schema elenca anche "unknown" (visto realmente per alcune combinazioni KS-STOR):
# lo trattiamo come non disponibile per prudenza.
NOT_AVAILABLE = {"unavailable", "comingSoon", "unknown"}


class RateLimited(Exception):
    def __init__(self, retry_after: float | None):
        super().__init__("HTTP 429")
        self.retry_after = retry_after


@dataclass(frozen=True)
class Offer:
    entry: dict  # elemento grezzo dedicated.DatacenterAvailability
    datacenter: str
    availability: str

    @property
    def fqn(self) -> str:
        return self.entry.get("fqn") or self.entry.get("planCode", "?")

    @property
    def key(self) -> tuple[str, str]:
        return self.fqn, self.datacenter

    def __str__(self) -> str:
        return f"{self.fqn} @ {self.datacenter} ({self.availability})"


class AvailabilityClient:
    def __init__(self, api_base: str, session: requests.Session):
        self.url = f"{api_base}/dedicated/server/datacenter/availabilities"
        self.session = session

    def fetch(self, plan_code: str) -> list[dict]:
        r = self.session.get(self.url, params={"planCode": plan_code}, timeout=(5, 15))
        if r.status_code == 429:
            ra = r.headers.get("Retry-After")
            raise RateLimited(float(ra) if ra and ra.isdigit() else None)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            raise ValueError("risposta availabilities inattesa")
        return data


def available_offers(entries: list[dict], datacenters: list[str]) -> list[Offer]:
    """Offerte disponibili, ordinate per preferenza di datacenter."""
    pref = {dc: i for i, dc in enumerate(datacenters)}
    out = []
    for e in entries:
        for dc in e.get("datacenters") or []:
            name, av = dc.get("datacenter"), dc.get("availability")
            if name in pref and av and av not in NOT_AVAILABLE:
                out.append(Offer(entry=e, datacenter=name, availability=av))
    out.sort(key=lambda o: pref[o.datacenter])
    return out


def still_available(client: AvailabilityClient, offer: Offer) -> bool | None:
    """Ricontrolla un'offerta. None se il controllo stesso fallisce."""
    try:
        entries = client.fetch(offer.entry.get("planCode", ""))
    except Exception:
        return None
    return any(o.key == offer.key for o in available_offers(entries, [offer.datacenter]))
