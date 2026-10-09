"""Configurazione letta esclusivamente da variabili d'ambiente."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

MIN_POLL_INTERVAL = 20  # secondi: sotto questa soglia si rischia il ban dell'API pubblica

# Endpoint REST per ciascun endpoint della libreria ovh (stessi valori di ovh.client.ENDPOINTS)
API_BASE_URLS = {
    "ovh-eu": "https://eu.api.ovh.com/1.0",
    "ovh-ca": "https://ca.api.ovh.com/1.0",
    "ovh-us": "https://api.us.ovhcloud.com/1.0",
}


class ConfigError(Exception):
    pass


def _str(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return default if v is None else v.strip()


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or not v.strip():
        return default
    v = v.strip().lower()
    if v in ("1", "true", "yes", "on", "si", "sì"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name}: valore booleano non valido")


def _float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None or not v.strip():
        return default
    try:
        return float(v.strip().replace(",", "."))
    except ValueError as e:
        raise ConfigError(f"{name}: numero non valido") from e


def _int(name: str, default: int) -> int:
    return int(_float(name, default))


def _list(name: str, default: str) -> list[str]:
    raw = _str(name, default) or default
    return [x.strip().lower() for x in raw.split(",") if x.strip()]


@dataclass(frozen=True)
class Config:
    endpoint: str
    app_key: str
    app_secret: str
    consumer_key: str
    subsidiary: str
    plan_codes: list[str]
    datacenters: list[str]
    min_storage_tb: float
    max_monthly_price: float
    max_first_payment: float
    poll_interval: int
    dry_run: bool
    waive_retractation: bool
    telegram_token: str
    telegram_chat_id: str
    heartbeat: bool
    heartbeat_hour: int
    data_dir: str
    os_template: str
    dry_run_cooldown: int
    max_prep_failures: int
    catalog_refresh: int
    cart_ttl_hours: int
    secrets: list[str] = field(default_factory=list, repr=False)

    @property
    def api_base(self) -> str:
        return API_BASE_URLS[self.endpoint]

    @property
    def has_credentials(self) -> bool:
        return bool(self.app_key and self.app_secret and self.consumer_key)

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id)

    def __repr__(self) -> str:  # evita che chiavi/token finiscano nei log per errore
        return (
            f"Config(endpoint={self.endpoint}, subsidiary={self.subsidiary}, plans={self.plan_codes}, "
            f"dc={self.datacenters}, min_storage_tb={self.min_storage_tb}, max_monthly={self.max_monthly_price}, "
            f"max_first={self.max_first_payment}, poll={self.poll_interval}s, dry_run={self.dry_run}, "
            f"waive_retractation={self.waive_retractation}, telegram={self.telegram_enabled})"
        )

    __str__ = __repr__


def load() -> Config:
    endpoint = _str("OVH_ENDPOINT", "ovh-eu")
    if endpoint not in API_BASE_URLS:
        raise ConfigError(f"OVH_ENDPOINT non supportato: {endpoint}")

    poll = _int("POLL_INTERVAL", 30)
    if poll < MIN_POLL_INTERVAL:
        poll = MIN_POLL_INTERVAL

    cfg = Config(
        endpoint=endpoint,
        app_key=_str("OVH_APPLICATION_KEY"),
        app_secret=_str("OVH_APPLICATION_SECRET"),
        consumer_key=_str("OVH_CONSUMER_KEY"),
        subsidiary=_str("OVH_SUBSIDIARY", "IT").upper() or "IT",
        plan_codes=_list("PLAN_CODES", "24skstor01-v1"),
        datacenters=_list("DATACENTERS", "gra,rbx,sbg,fra,lon,waw"),
        min_storage_tb=_float("MIN_STORAGE_TB", 16),
        max_monthly_price=_float("MAX_MONTHLY_PRICE", 30),
        max_first_payment=_float("MAX_FIRST_PAYMENT", 60),
        poll_interval=poll,
        dry_run=_bool("DRY_RUN", True),
        waive_retractation=_bool("WAIVE_RETRACTATION", True),
        telegram_token=_str("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_str("TELEGRAM_CHAT_ID"),
        heartbeat=_bool("HEARTBEAT", True),
        heartbeat_hour=_int("HEARTBEAT_HOUR", 9),
        data_dir=_str("DATA_DIR", "/data") or "/data",
        os_template=_str("OS_TEMPLATE", "none_64.en") or "none_64.en",
        dry_run_cooldown=_int("DRY_RUN_COOLDOWN", 3600),
        max_prep_failures=_int("MAX_PREP_FAILURES", 3),
        catalog_refresh=_int("CATALOG_REFRESH", 1800),
        cart_ttl_hours=max(1, _int("CART_TTL_HOURS", 12)),
    )
    if not cfg.plan_codes:
        raise ConfigError("PLAN_CODES vuoto")
    if not cfg.datacenters:
        raise ConfigError("DATACENTERS vuoto")
    object.__setattr__(
        cfg,
        "secrets",
        [s for s in (cfg.app_key, cfg.app_secret, cfg.consumer_key, cfg.telegram_token) if s and len(s) >= 6],
    )
    return cfg
