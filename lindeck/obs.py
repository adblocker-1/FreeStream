"""Anbindung an OBS Studio über obs-websocket (Protokoll v5, in OBS ab 28 enthalten).

In OBS: Werkzeuge → WebSocket-Server-Einstellungen → „WebSocket-Server aktivieren“.
Verbindung in config.json:  "obs": {"host": "127.0.0.1", "port": 4455, "password": "…"}

Der Client verbindet sich im Hintergrund, hält den Zustand (aktive Szene,
Aufnahme, Stream …) über OBS-Ereignisse aktuell und baut die Verbindung
automatisch neu auf, wenn OBS neu gestartet wird.
"""
import base64
import hashlib
import json
import logging
import threading
import time
import uuid as uuidlib

from . import ws
from .i18n import _

log = logging.getLogger("lindeck.obs")

EVENTS_ALL = 0x7FF


class OBSError(Exception):
    pass


def auth_string(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()


class OBSClient:
    def __init__(self, host="127.0.0.1", port=4455, password=""):
        self.host, self.port, self.password = host, int(port), password or ""
        self.conn = None
        self.connected = False
        self.error = ""
        self.version = 0           # wird bei jeder Zustandsänderung erhöht (für Neuzeichnen)
        self.state = {"scene": "", "preview": "", "recording": False, "rec_paused": False, "streaming": False,
                      "replay": False, "virtualcam": False, "studio": False}
        self.muted = {}            # Eingang -> stumm
        self.volume_db = {}        # Eingang -> dB
        self.items = {}            # (Szene, Quelle) -> (sceneItemId, sichtbar)
        self._pending = {}
        self._lock = threading.Lock()
        self._running = True
        self._wake = threading.Event()
        threading.Thread(target=self._loop, name="obs", daemon=True).start()

    # ------------------------------------------------------------ Verbindung
    def _loop(self):
        delay = 1
        while self._running:
            try:
                self._connect()
                delay = 1
                self._reader()
            except (OSError, ws.Closed, OBSError, ValueError) as e:
                if self.connected or self.error != str(e):
                    log.info(_("OBS not connected: {error}").format(error=e))
                self.error = str(e)
            self._set_connected(False)
            self._wake.wait(delay)
            self._wake.clear()
            delay = min(10, delay * 2)

    def _connect(self):
        conn = ws.connect(self.host, self.port, timeout=3)
        hello = json.loads(conn.recv())
        if hello.get("op") != 0:
            raise OBSError(_("Unexpected response from OBS"))
        d = hello.get("d") or {}
        ident = {"rpcVersion": 1, "eventSubscriptions": EVENTS_ALL}
        auth = d.get("authentication")
        if auth:
            if not self.password:
                raise OBSError(_("OBS requires a password (config.json → obs.password)"))
            ident["authentication"] = auth_string(self.password, auth["salt"], auth["challenge"])
        conn.send(json.dumps({"op": 1, "d": ident}))
        msg = json.loads(conn.recv())
        if msg.get("op") != 2:
            raise OBSError(_("Login to OBS failed (check the password)"))
        self.conn = conn
        self._set_connected(True)
        log.info(_("Connected to OBS ({host}:{port})").format(host=self.host, port=self.port))
        threading.Thread(target=self._initial_state, daemon=True).start()

    def _set_connected(self, on):
        if self.connected != on:
            self.connected = on
            self.version += 1
        if not on:
            with self._lock:
                for ev, box in self._pending.values():
                    box["error"] = _("Connection to OBS lost")
                    ev.set()
                self._pending.clear()
            self.conn = None

    def _initial_state(self):
        for req, key, field in (("GetCurrentProgramScene", "scene", "currentProgramSceneName"),
                                ("GetRecordStatus", "recording", "outputActive"),
                                ("GetStreamStatus", "streaming", "outputActive"),
                                ("GetVirtualCamStatus", "virtualcam", "outputActive"),
                                ("GetReplayBufferStatus", "replay", "outputActive"),
                                ("GetStudioModeEnabled", "studio", "studioModeEnabled")):
            try:
                self.state[key] = self.request(req).get(field, self.state[key])
            except OBSError:
                pass
        try:
            self.state["rec_paused"] = bool(self.request("GetRecordStatus").get("outputPaused"))
        except OBSError:
            pass
        self.version += 1

    def _reader(self):
        while self._running:
            msg = json.loads(self.conn.recv())
            op, d = msg.get("op"), msg.get("d") or {}
            if op == 7:
                with self._lock:
                    item = self._pending.pop(d.get("requestId"), None)
                if item:
                    ev, box = item
                    st = d.get("requestStatus") or {}
                    if st.get("result"):
                        box["data"] = d.get("responseData") or {}
                    else:
                        box["error"] = st.get("comment") or _("Error {code}").format(code=st.get("code"))
                    ev.set()
            elif op == 5:
                self._on_event(d.get("eventType"), d.get("eventData") or {})

    def _on_event(self, et, data):
        s = self.state
        if et == "CurrentProgramSceneChanged":
            s["scene"] = data.get("sceneName", "")
        elif et == "CurrentPreviewSceneChanged":
            s["preview"] = data.get("sceneName", "")
        elif et == "RecordStateChanged":
            s["recording"] = bool(data.get("outputActive"))
            s["rec_paused"] = data.get("outputState") == "OBS_WEBSOCKET_OUTPUT_PAUSED"
        elif et == "StreamStateChanged":
            s["streaming"] = bool(data.get("outputActive"))
        elif et == "ReplayBufferStateChanged":
            s["replay"] = bool(data.get("outputActive"))
        elif et == "VirtualcamStateChanged":
            s["virtualcam"] = bool(data.get("outputActive"))
        elif et == "StudioModeStateChanged":
            s["studio"] = bool(data.get("studioModeEnabled"))
        elif et == "InputMuteStateChanged":
            self.muted[data.get("inputName")] = bool(data.get("inputMuted"))
        elif et == "InputVolumeChanged":
            self.volume_db[data.get("inputName")] = data.get("inputVolumeDb")
        elif et == "SceneItemEnableStateChanged":
            for key, (iid, __) in list(self.items.items()):
                if key[0] == data.get("sceneName") and iid == data.get("sceneItemId"):
                    self.items[key] = (iid, bool(data.get("sceneItemEnabled")))
        elif et in ("SceneItemCreated", "SceneItemRemoved", "SceneNameChanged", "InputNameChanged"):
            self.items.clear()
        else:
            return
        self.version += 1

    def close(self):
        self._running = False
        if self.conn:
            self.conn.close()
        self._wake.set()

    # --------------------------------------------------------------- Anfragen
    def request(self, rtype, data=None, timeout=3.0) -> dict:
        conn = self.conn
        if not conn or not self.connected:
            self._wake.set()
            raise OBSError(_("OBS is not connected ({error})").format(error=self.error) if self.error else
                           _("OBS is not connected"))
        rid = uuidlib.uuid4().hex
        ev, box = threading.Event(), {}
        with self._lock:
            self._pending[rid] = (ev, box)
        msg = {"op": 6, "d": {"requestType": rtype, "requestId": rid}}
        if data:
            msg["d"]["requestData"] = data
        try:
            conn.send(json.dumps(msg))
        except ws.Closed as e:
            raise OBSError(str(e)) from e
        if not ev.wait(timeout):
            with self._lock:
                self._pending.pop(rid, None)
            raise OBSError(_("No response from OBS to {request}").format(request=rtype))
        if "error" in box:
            raise OBSError(box["error"])
        return box.get("data", {})

    # ------------------------------------------------------ Hilfsfunktionen
    def set_scene(self, name):
        if self.state["studio"]:
            self.request("SetCurrentPreviewScene", {"sceneName": name})
        else:
            self.request("SetCurrentProgramScene", {"sceneName": name})

    def is_muted(self, inp):
        if inp not in self.muted and self.connected:
            try:
                self.muted[inp] = bool(self.request("GetInputMute", {"inputName": inp}).get("inputMuted"))
            except OBSError:
                return None
        return self.muted.get(inp)

    def input_volume_db(self, inp):
        if inp not in self.volume_db and self.connected:
            try:
                self.volume_db[inp] = self.request("GetInputVolume", {"inputName": inp}).get("inputVolumeDb")
            except OBSError:
                return None
        return self.volume_db.get(inp)

    def change_volume_db(self, inp, delta):
        cur = self.input_volume_db(inp)
        cur = -20.0 if cur is None or cur < -100 else cur
        new = max(-100.0, min(26.0, cur + delta))
        self.request("SetInputVolume", {"inputName": inp, "inputVolumeDb": new})
        self.volume_db[inp] = new
        self.version += 1

    def item(self, scene, source):
        scene = scene or self.state["scene"]
        key = (scene, source)
        if key not in self.items:
            iid = self.request("GetSceneItemId", {"sceneName": scene, "sourceName": source})["sceneItemId"]
            on = self.request("GetSceneItemEnabled", {"sceneName": scene, "sceneItemId": iid})["sceneItemEnabled"]
            self.items[key] = (iid, bool(on))
        return scene, self.items[key]

    def toggle_source(self, scene, source):
        scene, (iid, on) = self.item(scene, source)
        self.request("SetSceneItemEnabled", {"sceneName": scene, "sceneItemId": iid, "sceneItemEnabled": not on})
        self.items[(scene, source)] = (iid, not on)
        self.version += 1


_clients = {}
_clients_lock = threading.Lock()


def client(cfg) -> OBSClient:
    """Gemeinsamer OBS-Client je Verbindungseinstellung."""
    c = (cfg.get("obs") if cfg else None) or {}
    key = (c.get("host") or "127.0.0.1", int(c.get("port") or 4455), c.get("password") or "")
    with _clients_lock:
        if key not in _clients:
            for old in _clients.values():
                old.close()
            _clients.clear()
            _clients[key] = OBSClient(*key)
        return _clients[key]


def wait_connected(cl: OBSClient, timeout=2.0) -> bool:
    end = time.time() + timeout
    while not cl.connected and time.time() < end:
        cl._wake.set()
        time.sleep(0.05)
    return cl.connected
