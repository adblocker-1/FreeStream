"""Plugin-Host (Platzhalter).

Hier wird später das Elgato-Plugin-SDK (WebSocket-Protokoll) bzw. eigene
Linux-Plugins angebunden. Jede Methode gibt False zurück, wenn kein Plugin
die Aktion übernimmt – dann zeigt die Taste das Warnsymbol.
"""
import logging

log = logging.getLogger("lindeck.plugins")


class PluginHost:
    def __init__(self, daemon):
        self.daemon = daemon
        self.handlers = {}   # Plugin-UUID-Präfix -> Objekt mit key_down/key_up/...

    def register(self, prefix, handler):
        self.handlers[prefix] = handler

    def _handler(self, action):
        for prefix, h in self.handlers.items():
            if action.uuid.startswith(prefix):
                return h
        return None

    def _call(self, name, action, *args):
        h = self._handler(action)
        if h and hasattr(h, name):
            getattr(h, name)(action, *args)
            return True
        if name in ("key_down", "dial_down") or (name == "touch_tap"):
            action.alert()
        return False

    def will_appear(self, ctrl, action):
        if not action.supported:
            return self._call("will_appear", action, ctrl)
        return False

    def will_disappear(self, ctrl, action):
        if not action.supported:
            return self._call("will_disappear", action, ctrl)
        return False

    def key_down(self, action):
        return self._call("key_down", action)

    def key_up(self, action):
        return self._call("key_up", action)

    def dial_rotate(self, action, ticks):
        return self._call("dial_rotate", action, ticks)

    def dial_down(self, action):
        return self._call("dial_down", action)

    def dial_up(self, action):
        return self._call("dial_up", action)

    def touch_tap(self, action, pos, long):
        return self._call("touch_tap", action, pos, long)
