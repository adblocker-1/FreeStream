"""Anbindung an Home Assistant über die REST-API.

Verbindung in config.json:
  "homeassistant": {"url": "http://homeassistant.local:8123", "token": "<Long-Lived Access Token>",
                    "verify_ssl": true}
Den Token legt man in Home Assistant unter Profil → Sicherheit → „Langlebige Zugangstoken“ an.

Ein gemeinsamer Client je Verbindung hält einen Zwischenspeicher der Zustände.
Ein Hintergrund-Thread fragt nur die Entitäten ab, die gerade auf Tasten zu sehen
sind (mit Mindestabstand und Backoff bei Fehlern). Dienstaufrufe laufen in einem
eigenen Thread, damit weder die Oberfläche noch das Zeichnen der Tasten blockiert.
Der Token wird nie protokolliert.
"""
import json
import logging
import queue
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .i18n import _

log = logging.getLogger("freestream.hass")

CONFIG_KEY = "homeassistant"
POLL_INTERVAL = 2.0      # Sekunden zwischen zwei Abfragen
MIN_GAP = 0.4            # Mindestabstand zwischen Abfragen (auch nach Tastendruck)
WATCH_TTL = 15.0         # so lange bleibt eine Entität ohne Zugriff in der Abfrage
HOLD = 1.5               # so lange gilt ein vorläufig gesetzter Zustand
MAX_SINGLE = 8           # bis zu so vielen Entitäten einzeln abfragen, sonst /api/states

# Fehlerarten
CONFIG, AUTH, OFFLINE, NOT_FOUND, OTHER = "config", "auth", "offline", "notfound", "other"


class HAError(Exception):
    def __init__(self, message, kind=OTHER):
        super().__init__(message)
        self.kind = kind


def normalize_url(url: str) -> str:
    """„homeassistant.local:8123/“ → „http://homeassistant.local:8123“."""
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    if url.endswith("/api"):
        url = url[:-4]
    return url


def settings(cfg) -> dict:
    c = (cfg.get(CONFIG_KEY) if cfg else None) or {}
    return c if isinstance(c, dict) else {}


def domain_of(entity_id: str) -> str:
    return (entity_id or "").partition(".")[0]


def friendly_name(st, entity_id=""):
    if st:
        name = (st.get("attributes") or {}).get("friendly_name")
        if name:
            return str(name)
    return (entity_id or "").partition(".")[2].replace("_", " ") or entity_id


def format_state(st, attribute="", decimals=None):
    """Zustand (oder Attribut) mit Einheit als Text, z. B. „21.5 °C“."""
    if not st:
        return ""
    attrs = st.get("attributes") or {}
    val = attrs.get(attribute) if attribute else st.get("state")
    unit = "" if attribute else (attrs.get("unit_of_measurement") or "")
    if val is None:
        return "–"
    if not isinstance(val, bool):
        try:
            num = float(val)
            if decimals is not None and decimals >= 0:
                val = f"{num:.{int(decimals)}f}"
            elif isinstance(val, float) or "." in str(val):
                val = f"{num:.1f}".rstrip("0").rstrip(".") if abs(num) < 1000 else f"{num:.0f}"
        except (TypeError, ValueError):
            pass
    text = str(val)
    if text in ("unavailable", "unknown"):
        return {"unavailable": _("unavailable"), "unknown": _("unknown")}[text]
    return f"{text} {unit}".strip() if unit else text


ON_STATES = {"on", "open", "opening", "unlocked", "playing", "home", "active", "heat", "cool", "heat_cool",
             "auto", "dry", "fan_only"}


def is_on(st):
    """True/False für schaltbare Entitäten, None wenn unbekannt."""
    if not st:
        return None
    s = str(st.get("state", "")).lower()
    if s in ("unavailable", "unknown", ""):
        return None
    if s in ON_STATES:
        return True
    return False if s in ("off", "closed", "closing", "locked", "idle", "paused", "standby", "not_home",
                          "stopped") else None


class HAClient:
    def __init__(self, url, token, verify_ssl=True, interval=None, timeout=4.0):
        self.url = normalize_url(url)
        self._token = token or ""
        self.verify_ssl = verify_ssl
        self.interval = interval or POLL_INTERVAL
        self.timeout = timeout
        self.error = ""
        self.error_kind = ""
        self.version = 0                  # steigt bei jeder Änderung (zum Neuzeichnen)
        self._states = {}                 # entity_id -> Zustand (dict) oder None (unbekannt)
        self._watch = {}                  # entity_id -> letzter Zugriff
        self._hold = {}                   # entity_id -> bis wann der vorläufige Zustand gilt
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._running = True
        self._last_poll = 0.0
        self._fails = 0
        self._jobs = queue.Queue()
        self._poller = None
        self._worker = None
        self._entities = (0.0, [])        # Zeit, Liste für die Entitätsauswahl
        self._ctx = None                  # TLS-Kontext (siehe _ssl)
        if not self.url or not self._token:
            self._set_error(_("Home Assistant is not set up yet"), CONFIG)

    # ------------------------------------------------------------- HTTP
    @property
    def configured(self):
        return bool(self.url and self._token)

    def _ssl(self):
        """TLS-Kontext für https, einmal je Client: ein neuer lädt jedes Mal die CA-Zertifikate des Systems
        (rund 20 ms Rechenzeit je Abfrage, bei mehreren Entitäten alle 2 s)."""
        if not self.url.startswith("https"):
            return None
        if self._ctx is None:
            ctx = ssl.create_default_context()
            ctx.set_alpn_protocols(["http/1.1"])  # wie urllib ohne eigenen Kontext
            if not self.verify_ssl:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            self._ctx = ctx
        return self._ctx

    def request(self, method, path, body=None, timeout=None):
        """Eine Anfrage an die REST-API; Fehler als HAError mit verständlicher Meldung."""
        if not self.configured:
            raise HAError(_("Home Assistant is not set up yet"), CONFIG)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("Authorization", "Bearer " + self._token)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout, context=self._ssl()) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise HAError(_("Access denied – check the access token"), AUTH) from None
            if e.code == 404:
                raise HAError(_("Not found: {path}").format(path=path), NOT_FOUND) from None
            msg = ""
            try:
                msg = json.loads(e.read() or b"{}").get("message") or ""
            except (ValueError, AttributeError, OSError):
                pass
            raise HAError(_("Home Assistant reports error {code}: {message}").format(
                code=e.code, message=msg or e.reason), OTHER) from None
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", e)
            raise HAError(_("Home Assistant not reachable: {error}").format(error=reason), OFFLINE) from None
        except (OSError, ValueError) as e:  # Zeitüberschreitung, kaputte Adresse …
            raise HAError(_("Home Assistant not reachable: {error}").format(error=e), OFFLINE) from None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            raise HAError(_("Unexpected response from Home Assistant"), OTHER) from None

    # ------------------------------------------------------- Abfragen
    def info(self):
        """Version und Standortname (für „Verbindung testen“)."""
        self.request("GET", "/api/")
        cfg = self.request("GET", "/api/config") or {}
        self._set_ok()
        return {"version": cfg.get("version", ""), "location_name": cfg.get("location_name", "")}

    def states(self, max_age=0.0):
        """Alle Entitäten (für die Auswahl); kurz zwischengespeichert."""
        at, cached = self._entities
        if max_age and cached and time.time() - at < max_age:
            return cached
        lst = self.request("GET", "/api/states") or []
        with self._lock:
            for st in lst:
                if isinstance(st, dict) and st.get("entity_id") in self._watch:
                    self._store(st["entity_id"], st)
        self._entities = (time.time(), lst)
        self._set_ok()
        return lst

    def fetch(self, entity_id):
        """Zustand einer Entität direkt abfragen (blockiert)."""
        try:
            st = self.request("GET", "/api/states/" + urllib.parse.quote(entity_id))
        except HAError as e:
            if e.kind == NOT_FOUND:
                with self._lock:
                    self._store(entity_id, None)
                raise HAError(_("Unknown entity: {entity}").format(entity=entity_id), NOT_FOUND) from None
            raise
        with self._lock:
            self._store(entity_id, st)
        return st

    # ------------------------------------------------ Zwischenspeicher
    def watch(self, entity_id):
        """Entität in die Hintergrundabfrage aufnehmen (bei jedem Zugriff erneuern)."""
        if not entity_id:
            return
        new = entity_id not in self._watch
        self._watch[entity_id] = time.time()
        if self.configured and self._poller is None:
            self._poller = threading.Thread(target=self._loop, name="homeassistant", daemon=True)
            self._poller.start()
        if new:
            self._wake.set()

    def state(self, entity_id):
        """Zustand aus dem Zwischenspeicher (nie blockierend)."""
        self.watch(entity_id)
        return self._states.get(entity_id)

    def known(self, entity_id):
        """False, wenn Home Assistant die Entität nicht kennt."""
        return not (entity_id in self._states and self._states[entity_id] is None)

    def _store(self, entity_id, st):
        if time.time() < self._hold.get(entity_id, 0):
            return  # gerade selbst gesetzt (Dial/Taste): ältere Antworten nicht zurückspringen lassen
        old = self._states.get(entity_id, "∅")
        if old == "∅" or (old or {}).get("state") != (st or {}).get("state") or \
                (old or {}).get("attributes") != (st or {}).get("attributes") or (old is None) != (st is None):
            self._states[entity_id] = st
            self.version += 1

    def set_local(self, entity_id, state=None, **attrs):
        """Zustand vorläufig selbst setzen (sofortige Anzeige nach Tastendruck)."""
        with self._lock:
            st = dict(self._states.get(entity_id) or {"entity_id": entity_id, "attributes": {}})
            if state is not None:
                st["state"] = state
            if attrs:
                st["attributes"] = dict(st.get("attributes") or {}, **attrs)
            self._states[entity_id] = st
            self._hold[entity_id] = time.time() + HOLD
            self.version += 1

    def _set_error(self, msg, kind):
        if self.error != msg:
            if kind != CONFIG:
                log.info(_("Home Assistant: {error}").format(error=msg))
            self.version += 1
        self.error, self.error_kind = msg, kind

    def _set_ok(self):
        if self.error:
            self.error, self.error_kind = "", ""
            self.version += 1
            log.info(_("Connected to Home Assistant ({url})").format(url=self.url))

    def refresh(self):
        """Sofort neu abfragen (nach einem Dienstaufruf)."""
        self._wake.set()

    def _active(self):
        now = time.time()
        for eid, t in list(self._watch.items()):
            if now - t > WATCH_TTL:
                self._watch.pop(eid, None)
        return list(self._watch)

    def _poll_once(self):
        ids = self._active()
        if not ids:
            return
        if len(ids) > MAX_SINGLE:
            self.states()
            return
        for eid in ids:
            try:
                self.fetch(eid)
            except HAError as e:
                if e.kind != NOT_FOUND:
                    raise
        self._set_ok()

    def _loop(self):
        while self._running:
            gap = time.time() - self._last_poll
            if gap < MIN_GAP:
                time.sleep(MIN_GAP - gap)
            self._last_poll = time.time()
            wait = self.interval
            try:
                self._poll_once()
                self._fails = 0
            except HAError as e:
                self._set_error(str(e), e.kind)
                self._fails += 1
                wait = min(30.0, self.interval * (2 ** min(self._fails, 5)))
            except Exception as e:  # noqa: BLE001 - die Abfrage darf nie sterben
                self._set_error(str(e), OTHER)
                wait = 10.0
            self._wake.wait(wait)
            self._wake.clear()

    def close(self):
        self._running = False
        self._wake.set()
        self._jobs.put(None)

    # --------------------------------------------------- Dienstaufrufe
    def call_service(self, domain, service, data=None):
        """Dienst aufrufen (blockiert); geänderte Zustände landen im Zwischenspeicher."""
        if not domain or not service:
            raise HAError(_("No service specified (e.g. light.turn_on)"), OTHER)
        res = self.request("POST", f"/api/services/{urllib.parse.quote(domain)}/{urllib.parse.quote(service)}",
                           data or {})
        if isinstance(res, list):
            with self._lock:
                for st in res:
                    if isinstance(st, dict) and st.get("entity_id"):
                        self._store(st["entity_id"], st)
        self._set_ok()
        self.refresh()
        return res

    def submit(self, fn, *args, on_error=None, on_done=None, key=None):
        """fn im Arbeits-Thread ausführen. key: gleiche Aufträge in der Schlange zusammenfassen."""
        if self._worker is None:
            self._worker = threading.Thread(target=self._work, name="homeassistant-calls", daemon=True)
            self._worker.start()
        self._jobs.put((fn, args, on_error, on_done, key))

    def _work(self):
        while self._running:
            job = self._jobs.get()
            if job is None:
                return
            fn, args, on_error, on_done, key = job
            try:
                res = fn(*args)
            except HAError as e:
                log.warning(_("Home Assistant: {error}").format(error=e))
                if e.kind in (AUTH, OFFLINE):
                    self._set_error(str(e), e.kind)
                if on_error:
                    on_error(e)
                continue
            except Exception as e:  # noqa: BLE001
                log.warning(_("Home Assistant: {error}").format(error=e))
                if on_error:
                    on_error(e)
                continue
            if on_done:
                on_done(res)

    # ---------------------------------------------------- Bequemlichkeit
    def toggle(self, entity_id):
        """Passenden Dienst zum Umschalten aufrufen (blockiert)."""
        dom = domain_of(entity_id)
        st = self._states.get(entity_id)
        if dom in ("scene",):
            return self.call_service("scene", "turn_on", {"entity_id": entity_id})
        if dom == "script":
            return self.call_service("script", "turn_on", {"entity_id": entity_id})
        if dom in ("button", "input_button"):
            return self.call_service(dom, "press", {"entity_id": entity_id})
        if dom == "lock":
            locked = str((st or {}).get("state")) == "locked"
            return self.call_service("lock", "unlock" if locked else "lock", {"entity_id": entity_id})
        if dom in ("cover", "light", "switch", "fan", "input_boolean", "automation", "media_player", "humidifier",
                   "siren", "climate", "remote", "vacuum", "valve"):
            if dom == "media_player":
                return self.call_service(dom, "media_play_pause", {"entity_id": entity_id})
            if dom == "climate":
                on = is_on(st)
                return self.call_service(dom, "turn_off" if on else "turn_on", {"entity_id": entity_id})
            return self.call_service(dom, "toggle", {"entity_id": entity_id})
        return self.call_service("homeassistant", "toggle", {"entity_id": entity_id})


def parse_service(text):
    """„light.turn_on“ → („light“, „turn_on“)."""
    dom, _sep, svc = (text or "").strip().partition(".")
    if not dom or not svc:
        raise HAError(_("Service must look like domain.service (e.g. light.turn_on)"), OTHER)
    return dom.strip(), svc.strip()


def parse_data(text):
    """Zusätzliche Daten als JSON-Objekt (leer = {})."""
    text = (text or "").strip() if isinstance(text, str) else text
    if not text:
        return {}
    if isinstance(text, dict):
        return dict(text)
    try:
        val = json.loads(text)
    except ValueError as e:
        raise HAError(_("Invalid JSON in the service data: {error}").format(error=e), OTHER) from None
    if not isinstance(val, dict):
        raise HAError(_("A JSON object { … } is expected"), OTHER)
    return val


_clients = {}
_clients_lock = threading.Lock()


def client(cfg) -> HAClient:
    """Gemeinsamer Client je Verbindungseinstellung."""
    c = settings(cfg)
    key = (normalize_url(c.get("url") or ""), c.get("token") or "", c.get("verify_ssl", True) is not False)
    with _clients_lock:
        if key not in _clients:
            for old in _clients.values():
                old.close()
            _clients.clear()
            _clients[key] = HAClient(*key)
        return _clients[key]


def check_connection(url, token, verify_ssl=True, timeout=5.0):
    """Verbindung prüfen, ohne den gemeinsamen Client anzufassen. Ergebnis: dict oder HAError."""
    cl = HAClient(url, token, verify_ssl, timeout=timeout)
    try:
        return cl.info()
    finally:
        cl.close()


def save_settings(cfg, url, token, verify_ssl=True):
    """Verbindung in config.json speichern (Config.save setzt dann die Dateirechte auf 0600)."""
    data = cfg.data if hasattr(cfg, "data") else cfg
    data[CONFIG_KEY] = {"url": normalize_url(url), "token": (token or "").strip(), "verify_ssl": bool(verify_ssl)}
    if hasattr(cfg, "save"):
        cfg.save()
