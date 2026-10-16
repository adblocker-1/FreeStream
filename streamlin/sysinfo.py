"""Systemwerte direkt aus /proc und /sys lesen (ohne psutil).

Alle Pfade sind über SysInfo(proc=..., sys=...) austauschbar, damit die
Tests mit nachgebauten Dateien arbeiten können. Raten (CPU-Last, Netzwerk)
brauchen zwei Messungen – dafür gibt es CpuMeter und RateMeter.
"""
import os
import shutil
import time
from pathlib import Path

# Sensoren, die die CPU-Temperatur liefern (in dieser Reihenfolge bevorzugt)
CPU_HWMON = ("coretemp", "k10temp", "zenpower", "cpu_thermal", "cpu-thermal", "soc_thermal", "acpitz",
             "thinkpad")
CPU_ZONES = ("x86_pkg_temp", "cpu-thermal", "cpu_thermal", "soc-thermal", "tcpu", "acpitz")
CPU_LABELS = ("package id 0", "tctl", "tdie", "cpu")
# virtuelle Schnittstellen, die bei "alle" nicht doppelt gezählt werden
VIRTUAL_IF = ("lo", "veth", "docker", "br-", "virbr", "vnet", "vmnet", "podman", "cni", "flannel")


def _read(p, default=""):
    try:
        return Path(p).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return default


def _int(p, default=None):
    try:
        return int(_read(p, ""))
    except ValueError:
        return default


class SysInfo:
    def __init__(self, proc="/proc", sys="/sys", disk_usage=None):
        self.proc = Path(proc)
        self.sys = Path(sys)
        self._disk_usage = disk_usage or shutil.disk_usage

    # ------------------------------------------------------------- CPU
    def cpu_times(self):
        """(Leerlauf, Gesamt) in Jiffies seit dem Start oder None."""
        for line in _read(self.proc / "stat").splitlines():
            if line.startswith("cpu "):
                vals = [int(v) for v in line.split()[1:] if v.isdigit()]
                if len(vals) < 4:
                    return None
                idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
                return idle, sum(vals[:8])
        return None

    # ---------------------------------------------------------- Speicher
    def memory(self):
        """(belegt in %, belegt Bytes, gesamt Bytes) oder None."""
        info = {}
        for line in _read(self.proc / "meminfo").splitlines():
            k, _sep, v = line.partition(":")
            try:
                info[k.strip()] = int(v.split()[0]) * 1024
            except (ValueError, IndexError):
                pass
        total = info.get("MemTotal")
        if not total:
            return None
        avail = info.get("MemAvailable")
        if avail is None:
            avail = info.get("MemFree", 0) + info.get("Buffers", 0) + info.get("Cached", 0)
        used = max(0, total - avail)
        return used * 100.0 / total, used, total

    # -------------------------------------------------------- Temperatur
    def temperatures(self):
        """Alle Sensoren als [(Name, °C)] – hwmon zuerst, dann thermal_zone."""
        out = []
        for d in sorted((self.sys / "class" / "hwmon").glob("hwmon*")):
            name = _read(d / "name") or d.name
            temps = sorted(d.glob("temp*_input"))
            best = None
            for t in temps:
                label = _read(Path(str(t).replace("_input", "_label"))).lower()
                v = _int(t)
                if v is None:
                    continue
                if best is None or label in CPU_LABELS:
                    best = v
                    if label in CPU_LABELS:
                        break
            if best is not None:
                out.append((name, best / 1000.0))
        for z in sorted((self.sys / "class" / "thermal").glob("thermal_zone*")):
            v = _int(z / "temp")
            if v is not None:
                out.append((_read(z / "type") or z.name, v / 1000.0))
        return out

    def temperature(self, sensor=""):
        """CPU-Temperatur in °C (sensor: Name eines bestimmten Sensors) oder None."""
        temps = self.temperatures()
        if not temps:
            return None
        if sensor:
            s = sensor.lower()
            hit = next((v for n, v in temps if n.lower() == s), None)
            return hit if hit is not None else next((v for n, v in temps if s in n.lower()), None)
        for pref in CPU_HWMON + CPU_ZONES:
            hit = next((v for n, v in temps if n.lower() == pref), None)
            if hit is not None:
                return hit
        return temps[0][1]

    # ---------------------------------------------------------- Netzwerk
    def net_counters(self):
        """{Schnittstelle: (empfangen, gesendet)} in Bytes."""
        out = {}
        for line in _read(self.proc / "net" / "dev").splitlines()[2:]:
            name, sep, rest = line.partition(":")
            if not sep:
                continue
            f = rest.split()
            try:
                out[name.strip()] = (int(f[0]), int(f[8]))
            except (ValueError, IndexError):
                pass
        return out

    def interfaces(self):
        return [n for n in self.net_counters() if not n.startswith(VIRTUAL_IF)]

    def net_bytes(self, iface=""):
        """(empfangen, gesendet) einer Schnittstelle oder aller echten zusammen; None, wenn unbekannt."""
        c = self.net_counters()
        if iface:
            return c.get(iface)
        sel = [v for n, v in c.items() if not n.startswith(VIRTUAL_IF)]
        if not sel:
            return None
        return sum(v[0] for v in sel), sum(v[1] for v in sel)

    # ----------------------------------------------------------- Akku
    def battery(self, name=""):
        """dict(percent, status, charging, plugged, hours) oder None ohne Akku."""
        base = self.sys / "class" / "power_supply"
        bats, plugged = [], None
        for d in sorted(base.glob("*")):
            kind = _read(d / "type")
            if kind == "Mains" or kind == "USB" and (d / "online").exists():
                on = _int(d / "online", 0)
                plugged = bool(on) or bool(plugged)
            elif kind == "Battery":
                if _read(d / "scope") == "Device" and not name:
                    continue  # Akku von Maus/Kopfhörer
                bats.append(d)
        if name:
            bats = [d for d in bats if d.name == name]
        if not bats:
            return None
        d = bats[0]
        pct = _int(d / "capacity")
        now = _int(d / "energy_now") or _int(d / "charge_now")
        full = _int(d / "energy_full") or _int(d / "charge_full")
        if pct is None and now and full:
            pct = round(now * 100 / full)
        if pct is None:
            return None
        status = _read(d / "status") or "Unknown"
        rate = abs(_int(d / "power_now") or _int(d / "current_now") or 0)
        hours = None
        if rate and now is not None:
            if status == "Discharging":
                hours = now / rate
            elif status == "Charging" and full:
                hours = max(0, full - now) / rate
        charging = status == "Charging"
        if plugged is None:
            plugged = status in ("Charging", "Full", "Not charging")
        return {"percent": max(0, min(100, pct)), "status": status, "charging": charging,
                "plugged": bool(plugged), "hours": hours}

    # ---------------------------------------------------------- Festplatte
    def disk(self, path="/"):
        """(belegt in %, frei Bytes, gesamt Bytes) oder None."""
        try:
            u = self._disk_usage(os.path.expanduser(path or "/"))
        except OSError:
            return None
        if not u.total:
            return None
        return u.used * 100.0 / u.total, u.free, u.total


class CpuMeter:
    """CPU-Last zwischen zwei Messungen (erste Messung: Mittel seit dem Start)."""

    def __init__(self, info):
        self.info = info
        self._last = (0, 0)

    def percent(self):
        t = self.info.cpu_times()
        if t is None:
            return None
        di, dt = t[0] - self._last[0], t[1] - self._last[1]
        if dt <= 0:
            return getattr(self, "_val", None)
        self._last = t
        self._val = max(0.0, min(100.0, 100.0 * (1 - di / dt)))
        return self._val


class RateMeter:
    """Rate eines Zählers pro Sekunde (z. B. Bytes der Netzwerkkarte)."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._last = None

    def update(self, values):
        """values: Tupel von Zählerständen -> Tupel Raten pro Sekunde (None bei der ersten Messung)."""
        now = self.clock()
        if values is None:
            self._last = None
            return None
        last, self._last = self._last, (now, values)
        if last is None or len(last[1]) != len(values):
            return None
        dt = now - last[0]
        if dt <= 0:
            return getattr(self, "_rate", None)
        rates = tuple(max(0.0, (v - o) / dt) for v, o in zip(values, last[1]))
        self._rate = rates
        return rates


def human_bytes(n, per_second=False):
    """1234567 -> "1.2 MB" (bzw. "1.2 MB/s")."""
    suffix = "/s" if per_second else ""
    if n is None:
        return "–"
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            if unit == "B":
                return f"{n:.0f} {unit}{suffix}"
            return f"{n:.1f} {unit}{suffix}" if abs(n) < 100 else f"{n:.0f} {unit}{suffix}"
        n /= 1000.0
    return f"{n:.0f} TB{suffix}"


DEFAULT = SysInfo()
