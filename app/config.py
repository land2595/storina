"""Configurazione: variabili d'ambiente (.env) sovrascritte da /data/config.json (salvato dalla web UI)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

MIN_POLL_INTERVAL = 20  # secondi: sotto questa soglia si rischia il ban dell'API pubblica

# Endpoint REST per ciascun endpoint della libreria ovh (stessi valori di ovh.client.ENDPOINTS)
API_BASE_URLS = {
    "ovh-eu": "https://eu.api.ovh.com/1.0",
    "ovh-ca": "https://ca.api.ovh.com/1.0",
    "ovh-us": "https://api.us.ovhcloud.com/1.0",
}


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Field:
    env: str
    kind: str  # str | secret | bool | int | float | list | choice
    default: str
    group: str
    label: str
    help: str = ""
    choices: tuple[str, ...] = ()

    @property
    def secret(self) -> bool:
        return self.kind == "secret"


FIELDS: list[Field] = [
    Field("OVH_ENDPOINT", "choice", "ovh-eu", "Account OVH", "Endpoint API", choices=tuple(API_BASE_URLS)),
    Field("OVH_APPLICATION_KEY", "str", "", "Account OVH", "Application Key",
          "Da https://eu.api.ovh.com/createApp/"),
    Field("OVH_APPLICATION_SECRET", "secret", "", "Account OVH", "Application Secret"),
    Field("OVH_CONSUMER_KEY", "secret", "", "Account OVH", "Consumer Key",
          "Generala con il pulsante qui sotto dopo aver salvato Application Key e Secret"),
    Field("OVH_SUBSIDIARY", "str", "IT", "Account OVH", "Filiale (subsidiary)", "Determina listino, valuta e IVA"),
    Field("PLAN_CODES", "list", "24skstor01-v1", "Cosa ordinare", "Plan code", "Separati da virgola"),
    Field("DATACENTERS", "list", "gra,rbx,sbg,fra,lon,waw", "Cosa ordinare", "Datacenter",
          "In ordine di preferenza, separati da virgola"),
    Field("OS_TEMPLATE", "str", "none_64.en", "Cosa ordinare", "Template OS", "none_64.en = nessun sistema operativo"),
    Field("MIN_STORAGE_TB", "float", "16", "Requisiti", "Storage minimo (TB)", "Somma dei dischi dal catalogo OVH"),
    Field("MAX_MONTHLY_PRICE", "float", "30", "Requisiti", "Canone massimo (€/mese IVA incl.)"),
    Field("MAX_FIRST_PAYMENT", "float", "60", "Requisiti", "Primo pagamento massimo (€ IVA incl.)",
          "Canone + setup, letto dall'anteprima del checkout"),
    Field("DRY_RUN", "bool", "true", "Comportamento", "Dry-run",
          "Se attivo fa tutto tranne il checkout finale. Disattivalo solo dopo aver verificato un dry-run."),
    Field("PAUSED", "bool", "false", "Comportamento", "In pausa", "Sospende il monitoraggio"),
    Field("POLL_INTERVAL", "int", "30", "Comportamento", "Intervallo di controllo (s)", "Minimo 20"),
    Field("WAIVE_RETRACTATION", "bool", "true", "Comportamento", "Rinuncia al diritto di recesso",
          "Necessario per la consegna immediata"),
    Field("DRY_RUN_COOLDOWN", "int", "3600", "Comportamento", "Pausa tra due dry-run sulla stessa offerta (s)"),
    Field("MAX_PREP_FAILURES", "int", "3", "Comportamento", "Errori tecnici consecutivi prima di fermarsi"),
    Field("CART_TTL_HOURS", "int", "12", "Comportamento", "Validità del carrello preparato (ore)"),
    Field("CATALOG_REFRESH", "int", "1800", "Comportamento", "Aggiornamento catalogo prezzi (s)"),
    Field("TELEGRAM_BOT_TOKEN", "secret", "", "Notifiche Telegram", "Bot token"),
    Field("TELEGRAM_CHAT_ID", "str", "", "Notifiche Telegram", "Chat ID"),
    Field("HEARTBEAT", "bool", "true", "Notifiche Telegram", "Messaggio giornaliero \"sono vivo\""),
    Field("HEARTBEAT_HOUR", "int", "9", "Notifiche Telegram", "Ora del messaggio giornaliero"),
]
FIELD_BY_ENV = {f.env: f for f in FIELDS}


# --- sorgenti ------------------------------------------------------------------------
def data_dir() -> str:
    return (os.getenv("DATA_DIR") or "/data").strip() or "/data"


def overrides_path() -> Path:
    return Path(data_dir()) / "config.json"


def read_overrides() -> dict[str, str]:
    try:
        data = json.loads(overrides_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: str(v) for k, v in data.items() if k in FIELD_BY_ENV} if isinstance(data, dict) else {}


def write_overrides(values: dict[str, str]) -> None:
    path = overrides_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # contiene segreti
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(values, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def merged(overrides: dict[str, str] | None = None) -> dict[str, tuple[str, str]]:
    """env -> (valore grezzo, sorgente: ui | env | default)."""
    ov = read_overrides() if overrides is None else overrides
    out = {}
    for f in FIELDS:
        env = os.getenv(f.env)
        if f.env in ov:
            out[f.env] = (ov[f.env], "ui")
        elif env is not None and env.strip():
            out[f.env] = (env.strip(), "env")
        else:
            out[f.env] = (f.default, "default")
    return out


# --- parsing -------------------------------------------------------------------------
def _bool(name: str, v: str) -> bool:
    v = v.strip().lower()
    if v in ("1", "true", "yes", "on", "si", "sì"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name}: valore booleano non valido")


def _float(name: str, v: str) -> float:
    try:
        return float(v.strip().replace(",", "."))
    except ValueError as e:
        raise ConfigError(f"{name}: numero non valido") from e


def _int(name: str, v: str) -> int:
    return int(_float(name, v))


def _list(v: str) -> list[str]:
    return [x.strip().lower() for x in v.split(",") if x.strip()]


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
    paused: bool
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
            f"dc={self.datacenters}, min_storage_tb={self.min_storage_tb:g}, max_monthly={self.max_monthly_price:g}, "
            f"max_first={self.max_first_payment:g}, poll={self.poll_interval}s, dry_run={self.dry_run}, "
            f"paused={self.paused}, waive_retractation={self.waive_retractation}, telegram={self.telegram_enabled})"
        )

    __str__ = __repr__


def load(overrides: dict[str, str] | None = None) -> Config:
    v = {k: val for k, (val, _src) in merged(overrides).items()}

    endpoint = v["OVH_ENDPOINT"].strip()
    if endpoint not in API_BASE_URLS:
        raise ConfigError(f"OVH_ENDPOINT non supportato: {endpoint}")

    cfg = Config(
        endpoint=endpoint,
        app_key=v["OVH_APPLICATION_KEY"].strip(),
        app_secret=v["OVH_APPLICATION_SECRET"].strip(),
        consumer_key=v["OVH_CONSUMER_KEY"].strip(),
        subsidiary=v["OVH_SUBSIDIARY"].strip().upper() or "IT",
        plan_codes=_list(v["PLAN_CODES"]),
        datacenters=_list(v["DATACENTERS"]),
        min_storage_tb=_float("MIN_STORAGE_TB", v["MIN_STORAGE_TB"]),
        max_monthly_price=_float("MAX_MONTHLY_PRICE", v["MAX_MONTHLY_PRICE"]),
        max_first_payment=_float("MAX_FIRST_PAYMENT", v["MAX_FIRST_PAYMENT"]),
        poll_interval=max(MIN_POLL_INTERVAL, _int("POLL_INTERVAL", v["POLL_INTERVAL"])),
        dry_run=_bool("DRY_RUN", v["DRY_RUN"]),
        paused=_bool("PAUSED", v["PAUSED"]),
        waive_retractation=_bool("WAIVE_RETRACTATION", v["WAIVE_RETRACTATION"]),
        telegram_token=v["TELEGRAM_BOT_TOKEN"].strip(),
        telegram_chat_id=v["TELEGRAM_CHAT_ID"].strip(),
        heartbeat=_bool("HEARTBEAT", v["HEARTBEAT"]),
        heartbeat_hour=_int("HEARTBEAT_HOUR", v["HEARTBEAT_HOUR"]),
        data_dir=data_dir(),
        os_template=v["OS_TEMPLATE"].strip() or "none_64.en",
        dry_run_cooldown=_int("DRY_RUN_COOLDOWN", v["DRY_RUN_COOLDOWN"]),
        max_prep_failures=max(1, _int("MAX_PREP_FAILURES", v["MAX_PREP_FAILURES"])),
        catalog_refresh=max(60, _int("CATALOG_REFRESH", v["CATALOG_REFRESH"])),
        cart_ttl_hours=max(1, _int("CART_TTL_HOURS", v["CART_TTL_HOURS"])),
    )
    if not cfg.plan_codes:
        raise ConfigError("PLAN_CODES vuoto")
    if not cfg.datacenters:
        raise ConfigError("DATACENTERS vuoto")
    if not 0 <= cfg.heartbeat_hour <= 23:
        raise ConfigError("HEARTBEAT_HOUR deve essere tra 0 e 23")
    for name, val in (("MIN_STORAGE_TB", cfg.min_storage_tb), ("MAX_MONTHLY_PRICE", cfg.max_monthly_price),
                      ("MAX_FIRST_PAYMENT", cfg.max_first_payment)):
        if val <= 0:
            raise ConfigError(f"{name} deve essere maggiore di zero")
    object.__setattr__(
        cfg,
        "secrets",
        [s for s in (cfg.app_key, cfg.app_secret, cfg.consumer_key, cfg.telegram_token) if s and len(s) >= 6],
    )
    return cfg
