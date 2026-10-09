"""Web UI: configurazione, stato in diretta (Server-Sent Events), tentativi, log e azioni.

Solo libreria standard. Protezione:
- password (WEB_PASSWORD dall'ambiente, oppure impostata al primo accesso e salvata come hash scrypt);
- cookie di sessione HttpOnly + SameSite=Strict, le richieste di modifica devono essere JSON;
- i valori segreti salvati non vengono mai rimandati al browser.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

from . import actions
from . import config as config_mod
from .availability import AvailabilityClient
from .catalog import CatalogProvider
from .hub import Runtime
from .state import atomic_write_json

log = logging.getLogger("ovh-ks-sniper.web")

STATIC = Path(__file__).with_name("static")
SESSION_TTL = 7 * 24 * 3600
LIVE_CONFIRM = "ORDINA"


class Auth:
    def __init__(self, data_dir: str):
        self.env_password = os.getenv("WEB_PASSWORD") or ""
        self.path = Path(data_dir) / "web_auth.json"
        self.sessions: dict[str, float] = {}
        self.lock = threading.Lock()

    def setup_needed(self) -> bool:
        return not self.env_password and not self.path.exists()

    def set_password(self, password: str) -> None:
        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1)
        atomic_write_json(self.path, {"salt": salt.hex(), "hash": digest.hex()})
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def check(self, password: str) -> bool:
        if self.env_password:
            return hmac.compare_digest(password.encode(), self.env_password.encode())
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(data["salt"]), n=2 ** 14, r=8, p=1)
            return hmac.compare_digest(digest.hex(), data["hash"])
        except (OSError, ValueError, KeyError):
            return False

    def new_session(self) -> str:
        token = secrets.token_urlsafe(32)
        with self.lock:
            now = time.time()
            self.sessions = {t: exp for t, exp in self.sessions.items() if exp > now}
            self.sessions[token] = now + SESSION_TTL
        return token

    def valid(self, token: str | None) -> bool:
        with self.lock:
            return bool(token) and self.sessions.get(token, 0) > time.time()

    def drop(self, token: str | None) -> None:
        with self.lock:
            self.sessions.pop(token or "", None)


def _config_view() -> dict:
    vals = config_mod.merged()
    fields = []
    for f in config_mod.FIELDS:
        raw, src = vals[f.env]
        item = {"env": f.env, "kind": f.kind, "group": f.group, "label": f.label, "help": f.help,
                "choices": list(f.choices), "source": src, "default": f.default}
        if f.secret:
            item["has_value"] = bool(raw)
            item["value"] = ""
        else:
            item["value"] = raw
        fields.append(item)
    return {"fields": fields}


class Handler(BaseHTTPRequestHandler):
    rt: Runtime
    auth: Auth
    server_version = "ovh-ks-sniper"
    protocol_version = "HTTP/1.1"

    # --- helper ----------------------------------------------------------------------
    def log_message(self, fmt, *args):  # niente log di accesso (rumore)
        pass

    def _token(self) -> str | None:
        c = SimpleCookie(self.headers.get("Cookie") or "")
        return c["sid"].value if "sid" in c else None

    def _json(self, obj, status: int = 200, cookie: str | None = None) -> None:
        body = json.dumps(obj, default=str, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _err(self, msg: str, status: int = 400) -> None:
        self._json({"error": msg}, status)

    def _body(self) -> dict | None:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if not (self.headers.get("Content-Type") or "").startswith("application/json") or not 0 <= n <= 100_000:
            self.close_connection = True  # corpo non letto: la connessione non è riutilizzabile
            return None
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _cookie(token: str, max_age: int) -> str:
        return f"sid={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}"

    # --- GET ---------------------------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._static("index.html", "text/html; charset=utf-8")
        if path == "/api/auth":
            return self._json({"setup_needed": self.auth.setup_needed(), "logged_in": self.auth.valid(self._token())})
        if not path.startswith("/api/"):
            return self._err("non trovato", 404)
        if not self.auth.valid(self._token()):
            return self._err("non autenticato", 401)
        if path == "/api/state":
            return self._json(self._state())
        if path == "/api/config":
            return self._json(_config_view())
        if path == "/api/events":
            return self._events()
        return self._err("non trovato", 404)

    def _static(self, name: str, ctype: str) -> None:
        body = (STATIC / name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                         "script-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _state(self) -> dict:
        hub, st = self.rt.hub, self.rt.state
        snap = hub.snapshot()
        snap["lock"] = st.lock()
        snap["halt"] = st.halt()
        return {"status": snap, "attempts": hub.attempts_list(), "logs": hub.recent_logs(),
                "last_id": hub.last_id, "server_time": time.time()}

    def _events(self) -> None:
        q = parse_qs(urlparse(self.path).query)
        try:
            after = int(self.headers.get("Last-Event-ID") or q.get("after", ["0"])[0])
        except ValueError:
            after = 0
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(b"retry: 3000\n\n")
            self.wfile.flush()
            while not self.rt.stop and self.auth.valid(self._token()):
                events = self.rt.hub.wait(after, 15)
                if not events:
                    self.wfile.write(b": ping\n\n")
                for e in events:
                    after = e["id"]
                    payload = json.dumps(e, default=str, ensure_ascii=False)
                    self.wfile.write(f"id: {e['id']}\ndata: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

    # --- POST --------------------------------------------------------------------------
    def do_POST(self):
        path = urlparse(self.path).path
        data = self._body()
        if data is None:
            return self._err("richiesta non valida (serve JSON)")

        if path == "/api/auth/setup":
            if not self.auth.setup_needed():
                return self._err("password già impostata", 409)
            pw = str(data.get("password") or "")
            if len(pw) < 8:
                return self._err("la password deve avere almeno 8 caratteri")
            self.auth.set_password(pw)
            log.warning("Password della web UI impostata")
            return self._json({"ok": True}, cookie=self._cookie(self.auth.new_session(), SESSION_TTL))
        if path == "/api/auth/login":
            if self.auth.setup_needed():
                return self._err("imposta prima una password", 409)
            if not self.auth.check(str(data.get("password") or "")):
                time.sleep(1.5)  # rallenta i tentativi
                log.warning("Login web UI fallito da %s", self.client_address[0])
                return self._err("password errata", 401)
            return self._json({"ok": True}, cookie=self._cookie(self.auth.new_session(), SESSION_TTL))
        if path == "/api/auth/logout":
            self.auth.drop(self._token())
            return self._json({"ok": True}, cookie=self._cookie("", 0))

        if not self.auth.valid(self._token()):
            return self._err("non autenticato", 401)
        routes = {
            "/api/config": self._save_config,
            "/api/actions/check-account": self._check_account,
            "/api/actions/consumer-key": self._consumer_key,
            "/api/actions/test-cart": self._test_cart,
            "/api/actions/poll-now": self._poll_now,
            "/api/actions/clear-lock": self._clear_lock,
            "/api/actions/clear-halt": self._clear_halt,
            "/api/actions/password": self._change_password,
        }
        fn = routes.get(path)
        if not fn:
            return self._err("non trovato", 404)
        try:
            fn(data)
        except Exception as e:
            log.exception("Errore nell'azione %s", path)
            self._err(f"errore interno: {type(e).__name__}", 500)

    # --- azioni ------------------------------------------------------------------------
    def _save_config(self, data: dict) -> None:
        values = data.get("values") or {}
        reset = data.get("reset") or []
        if not isinstance(values, dict) or not isinstance(reset, list):
            return self._err("formato non valido")
        current = config_mod.read_overrides()
        new = dict(current)
        changed = []
        for env, val in values.items():
            f = config_mod.FIELD_BY_ENV.get(env)
            if not f:
                continue
            if isinstance(val, bool):
                val = "true" if val else "false"
            val = "" if val is None else str(val).strip()
            if f.secret and val == "":
                continue  # campo segreto lasciato vuoto = invariato
            if config_mod.merged(new)[env][0] != val:
                new[env] = val
                changed.append(env)
        for env in reset:
            if env in new and env in config_mod.FIELD_BY_ENV:
                del new[env]
                changed.append(env)
        if not changed:
            return self._json({"ok": True, "changed": []})
        try:
            new_cfg = config_mod.load(new)
        except config_mod.ConfigError as e:
            return self._err(str(e))
        try:
            old_dry = config_mod.load(current).dry_run
        except config_mod.ConfigError:
            old_dry = True
        if old_dry and not new_cfg.dry_run and str(data.get("confirm_live") or "").strip().upper() != LIVE_CONFIRM:
            return self._json({"error": "conferma richiesta", "need_live_confirm": True}, 409)
        config_mod.write_overrides(new)
        log.warning("Configurazione aggiornata dalla web UI: %s", ", ".join(sorted(set(changed))))
        if not new_cfg.dry_run and old_dry:
            log.warning("ATTENZIONE: modalità LIVE attivata dalla web UI")
        self.rt.request_reload()
        self._json({"ok": True, "changed": sorted(set(changed))})

    def _check_account(self, data: dict) -> None:
        try:
            cfg = config_mod.load()
        except config_mod.ConfigError as e:
            return self._err(str(e))
        res = actions.check_account_safe(cfg)
        self.rt.hub.update(account=res)
        (log.info if res["ok"] else log.warning)("Verifica account dalla web UI: %s", res["message"])
        if res["ok"] and data.get("apply"):
            self.rt.request_reload()
        self._json(res)

    def _consumer_key(self, data: dict) -> None:
        try:
            cfg = config_mod.load()
            res = actions.request_consumer_key(cfg)
        except (config_mod.ConfigError, ValueError) as e:
            return self._err(str(e))
        except Exception as e:
            return self._err(f"Richiesta a OVH fallita: {type(e).__name__}: {e}")
        ov = config_mod.read_overrides()
        ov["OVH_CONSUMER_KEY"] = res["consumerKey"]
        config_mod.write_overrides(ov)
        # Nessun reload: la chiave non è ancora valida finché non la convalidi sul sito OVH.
        log.warning("Nuova consumer key richiesta: convalidala dall'URL mostrato nella web UI")
        self._json({"ok": True, "validationUrl": res.get("validationUrl")})

    def _test_cart(self, data: dict) -> None:
        rt = self.rt
        try:
            cfg = config_mod.load()
        except config_mod.ConfigError as e:
            return self._err(str(e))
        if not cfg.has_credentials:
            return self._err("Credenziali OVH incomplete")
        if not rt.order_mutex.acquire(blocking=False):
            return self._err("C'è già un flusso carrello in corso", 409)
        dc = str(data.get("dc") or cfg.datacenters[0]).lower()

        def run():
            try:
                rt.hub.update(attempt={"fqn": "test", "dc": dc, "step": "avvio", "started": time.time(),
                                       "test": True})

                def on_step(step):
                    rt.hub.update(attempt={**(rt.hub.snapshot().get("attempt") or {}), "step": step})

                s = requests.Session()
                catalogs = CatalogProvider(cfg.api_base, cfg.subsidiary, cfg.catalog_refresh, s)
                offer, cand, res = actions.test_cart(cfg, catalogs, AvailabilityClient(cfg.api_base, s),
                                                     rt.state, dc, on_step=on_step)
                rt.hub.add_attempt({"fqn": cand.fqn if cand else "-", "dc": dc, "kind": res.kind,
                                    "message": res.message, "details": res.details, "test": True,
                                    "dry_run": True})
            except Exception as e:
                log.exception("Test carrello fallito")
                rt.hub.add_attempt({"fqn": "-", "dc": dc, "kind": "prep_error",
                                    "message": f"{type(e).__name__}: {e}", "test": True, "dry_run": True})
            finally:
                rt.hub.update(attempt=None)
                rt.order_mutex.release()

        threading.Thread(target=run, name="test-cart", daemon=True).start()
        self._json({"ok": True, "started": True}, 202)

    def _poll_now(self, data: dict) -> None:
        self.rt.request_poll()
        self._json({"ok": True})

    def _clear_lock(self, data: dict) -> None:
        if str(data.get("confirm") or "").strip().upper() != "SBLOCCA":
            return self._err("conferma mancante")
        try:
            self.rt.state.lock_path.unlink(missing_ok=True)
        except OSError as e:
            return self._err(f"impossibile rimuovere il lock: {e}")
        log.warning("Lock ordine rimosso dalla web UI: nuovi ordini di nuovo possibili")
        self.rt.request_reload()
        self._json({"ok": True})

    def _clear_halt(self, data: dict) -> None:
        try:
            self.rt.state.halt_path.unlink(missing_ok=True)
        except OSError as e:
            return self._err(f"impossibile rimuovere il blocco: {e}")
        log.warning("Blocco per errore rimosso dalla web UI: riprendo")
        self.rt.request_reload()
        self._json({"ok": True})

    def _change_password(self, data: dict) -> None:
        if self.auth.env_password:
            return self._err("password impostata da WEB_PASSWORD: cambiala nel .env")
        if not self.auth.check(str(data.get("old") or "")):
            time.sleep(1.5)
            return self._err("password attuale errata", 401)
        new = str(data.get("new") or "")
        if len(new) < 8:
            return self._err("la nuova password deve avere almeno 8 caratteri")
        self.auth.set_password(new)
        with self.auth.lock:
            self.auth.sessions.clear()
        self._json({"ok": True}, cookie=self._cookie(self.auth.new_session(), SESSION_TTL))


def start(rt: Runtime, port: int) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"rt": rt, "auth": Auth(config_mod.data_dir())})
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
    log.info("Web UI in ascolto sulla porta %d", port)
    if handler.auth.setup_needed():
        log.warning("Web UI: nessuna password impostata, sceglila al primo accesso (o imposta WEB_PASSWORD)")
    return server
