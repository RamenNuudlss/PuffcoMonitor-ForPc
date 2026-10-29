"""
Puffco Monitor - a read-only desktop dashboard and OBS overlay for the Puffco Peak Pro / Proxy.

Talks to the device over Bluetooth LE using Puffco's "Lorax" protocol (community
reverse-engineered; see PuffcoBLE and puffco-py). Only READS values - it never
sends heat, lighting, or settings commands to the device.

Runs in the system tray; the window is app.html shown in a native Edge WebView2 window.
Close the Puffco app on your phone first - the rig only talks to one device at a time.

Charge and session history is saved to puffco_data.json next to the app.
The OBS overlay is served at http://127.0.0.1:8765/overlay while the app runs.

Run from source:  pip install bleak pywebview pystray pillow numpy
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import queue
import shutil
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path

VERSION = "2.0"
FROZEN = getattr(sys, "frozen", False)
RES = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))          # files bundled with the app
HERE = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent   # your data
sys.path.insert(0, str(RES))

log = logging.getLogger("puffco")
try:
    _h = RotatingFileHandler(HERE / "puffco_monitor.log", maxBytes=300_000, backupCount=1, encoding="utf-8")
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
except OSError:
    pass

try:
    from bleak import BleakClient, BleakScanner
except ImportError:  # shown in the UI instead of crashing
    BleakClient = BleakScanner = None

# ---------------------------------------------------------------- protocol ---
LORAX_CMD = "60133d5c-5727-4f2c-9697-d842c5292a3c"
LORAX_REPLY = "8dc5ec05-8f7d-45ad-99db-3fbde65dbd9c"
LORAX_VERSION = "05434bca-cc7f-4ef6-bbb3-b1c520b9800c"
LORAX_KEY = bytes.fromhex("64c6456256f26f5b1ca12737a5dd71fb")
OP_GET_SEED, OP_UNLOCK, OP_READ_SHORT, OP_WRITE_SHORT = 0x00, 0x01, 0x10, 0x11
NAME_HINTS = ("puffco", "peak", "proxy")

STATES = {
    0: "Starting up", 1: "Starting up", 2: "Starting up", 3: "Off", 4: "Asleep",
    5: "Idle", 6: "Choosing temp", 7: "Preheating", 8: "Session active",
    9: "Cooling down", 10: "Showing version", 11: "Showing battery",
    12: "Factory test", 13: "Pairing",
}
HEATING = {7, 8, 9}
CHARGE = {0: "Charging", 1: "Topping off", 2: "Full (on charger)",
          3: "Paused (temperature)", 4: "Not charging"}
CHARGE_SRC = {0: "USB", 1: "Qi wireless", 2: "Power Dock", 3: "None"}
CHAMBERS = {0: "None", 1: "Classic", 2: "3DXL", 3: "3D", 4: "3DXL", 5: "3D"}
MODELS = {0: "Peak Pro (OG)", 1: "Peak Pro Opal", 2: "Peak Pro Indiglow",
          4: "Peak Pro Guardian", 12: "Peak Pro Pearl", 13: "Peak Pro Onyx",
          15: "Peak Pro Desert", 17: "Peak Pro Flourish", 19: "Peak Pro Storm",
          23: "Peak Pro Daybreak"}

# Heat profiles: the rig always has exactly 4 slots, each under /u/app/hc/<slot>/.
# Temperatures are stored in Celsius except the boost, which is a Fahrenheit step.
PROFILE_SLOTS = 4
PROFILE_LIMITS = {"temp_f": (400, 620), "time": (5, 180), "boost_temp": (0, 36), "boost_time": (0, 60)}
VAPOR = {0.0: "Smooth", 0.5: "Bold", 1.0: "Intense", 1.5: "Extreme"}
PROFILE_NAME_MAX = 20   # bytes of UTF-8

# Paths that might hold battery health / cycle data. Not documented anywhere -
# the "Find battery data" button tries all of them and shows which ones answer.
PROBE_PATHS = [
    "/p/bat/soc", "/p/bat/cap", "/p/bat/volt", "/p/bat/vlt", "/p/bat/v", "/p/bat/temp",
    "/p/bat/tmp", "/p/bat/curr", "/p/bat/cur", "/p/bat/i", "/p/bat/cyc", "/p/bat/cycl",
    "/p/bat/ncyc", "/p/bat/cnt", "/p/bat/hlth", "/p/bat/soh", "/p/bat/fcc", "/p/bat/dcap",
    "/p/bat/rcap", "/p/bat/full", "/p/bat/stat", "/p/bat/info", "/p/bat/chg/stat",
    "/p/bat/chg/src", "/p/bat/chg/eta", "/p/bat/chg/elap", "/p/bat/chg/cur",
    "/p/bat/chg/volt", "/p/bat/chg/cnt", "/p/bat/chg/tot", "/p/bat/chg/cyc",
    "/p/app/odom/0/nc", "/p/app/odom/0/tm", "/p/app/odom/1/nc", "/p/app/odom/1/tm",
    "/p/app/info/dpd", "/p/app/info/drem", "/p/app/info/dtot", "/p/sys/uptm",
    "/p/sys/hw/mdcd", "/p/sys/hw/ser", "/p/sys/fw/ver", "/p/htr/chmt",
    "/p/app/stat/id", "/p/app/htr/temp", "/p/app/htr/ttag", "/p/app/thc/temp", "/p/app/hcs",
    # second round of guesses, following the naming the firmware actually uses (odom/N/nc = count, tm = time)
    "/p/bat/odom/nc", "/p/bat/odom/tm", "/p/bat/odom/0/nc", "/p/bat/odom/0/tm", "/p/bat/chg/odom/nc",
    "/p/bat/chg/nc", "/p/bat/chg/tm", "/p/bat/nc", "/p/bat/cc", "/p/bat/ccnt", "/p/bat/cycles", "/p/bat/fcap",
    "/p/bat/dsgn", "/p/bat/health", "/p/bat/hlt", "/p/bat/res", "/p/bat/ir", "/p/bat/vmin", "/p/bat/vmax",
    "/p/bat/tmin", "/p/bat/tmax", "/p/bat/err", "/p/bat/flt", "/p/bat/chg/v", "/p/bat/chg/i", "/p/bat/chg/ttf",
    "/p/bat/chg/tte", "/p/bat/chg/rem", "/p/app/odom/2/nc", "/p/app/odom/2/tm", "/p/app/odom/3/nc",
    "/p/app/thc/name", "/p/app/thc/time", "/p/sys/fw/bver", "/u/sys/name",
]


def f32(b):
    return struct.unpack("<f", b[:4])[0] if len(b) >= 4 else None


def num(b):
    """Decode a number whether the firmware stored it as float32, uint32, uint16 or uint8."""
    if not b:
        return None
    if len(b) >= 4:
        fv = f32(b)
        if fv == fv and (fv == 0 or 1e-3 < abs(fv) < 1e7):  # plausible float (not NaN/tiny)
            return fv
        return u32(b)
    if len(b) >= 2:
        return struct.unpack("<H", b[:2])[0]
    return b[0]


def u32(b):
    return struct.unpack("<I", b[:4])[0] if len(b) >= 4 else None


def text(b):
    return b.split(b"\x00")[0].decode(errors="ignore").strip()


def c_to_f(c):
    return c * 9 / 5 + 32


def fw_string(n):
    letters = "ABCDEFGHJKMNPRTUVWXYZ"
    if n == 0:
        return "X*"
    out, n = "", n - 1
    while n >= 0:
        out, n = letters[n % len(letters)] + out, n // len(letters) - 1
    return out


def describe_raw(b):
    """Show a raw value every plausible way, so unknown fields can be identified."""
    if not b:
        return "(empty)"
    parts = [f"hex {b.hex(' ')}"]
    if len(b) >= 4:
        fv = f32(b)
        if fv is not None and abs(fv) < 1e7:
            parts.append(f"float {fv:.3f}")
        parts.append(f"uint32 {u32(b)}")
    if len(b) >= 2:
        parts.append(f"uint16 {struct.unpack('<H', b[:2])[0]}")
    parts.append(f"uint8 {b[0]}")
    t = text(b)
    if t and all(32 <= ord(c) < 127 for c in t):
        parts.append(f'text "{t}"')
    return "  |  ".join(parts)


class Lorax:
    """Minimal Lorax client. Only the heat profile editor ever writes."""

    def __init__(self, address, on_disconnect):
        # timeout in the constructor (not connect()) works with both old bleak and bleak 3
        self.client = BleakClient(address, disconnected_callback=lambda _: on_disconnect(), timeout=15.0)
        self.seq = 0
        self.pending = {}
        self.lock = asyncio.Lock()

    async def connect(self):
        await self.client.connect()
        try:
            await self.client.read_gatt_char(LORAX_VERSION)  # triggers bonding
        except Exception:
            pass
        await self.client.start_notify(LORAX_REPLY, self._on_reply)
        status, seed = await self._cmd(OP_GET_SEED)
        if status != 0 or len(seed) < 16:
            raise RuntimeError("Device refused the handshake")
        token = hashlib.sha256(LORAX_KEY + bytes(seed[:16])).digest()[:16]
        status, _ = await self._cmd(OP_UNLOCK, token)
        if status != 0:
            raise RuntimeError("Unlock failed - is the Puffco app still connected on your phone?")

    async def disconnect(self):
        try:
            await self.client.disconnect()
        except Exception:
            pass

    def _on_reply(self, _, data):
        if len(data) < 3:
            return
        seq = data[0] | (data[1] << 8)
        fut = self.pending.get(seq)
        if fut and not fut.done():
            fut.set_result((data[2], bytes(data[3:])))

    async def _cmd(self, op, body=b"", timeout=2.0):
        async with self.lock:
            self.seq = (self.seq + 1) & 0xFFFF or 1
            seq = self.seq
            fut = asyncio.get_running_loop().create_future()
            self.pending[seq] = fut
            try:
                await self.client.write_gatt_char(
                    LORAX_CMD, struct.pack("<HB", seq, op) + body, response=False)
                return await asyncio.wait_for(fut, timeout)
            finally:
                self.pending.pop(seq, None)

    async def read(self, path, size=125):
        """Returns the raw bytes, or None if the path doesn't exist / errors."""
        try:
            status, data = await self._cmd(OP_READ_SHORT, struct.pack("<HH", 0, size) + path.encode())
        except Exception:
            return None
        return data if status == 0 else None

    async def write(self, path, data):
        """Writes raw bytes to a path. Raises if the rig refuses."""
        status, _ = await self._cmd(OP_WRITE_SHORT, struct.pack("<HB", 0, 0) + path.encode() + b"\x00" + data)
        if status != 0:
            raise RuntimeError(f"the rig refused {path} (code {status})")


# ------------------------------------------------------------- storage ---
DATA_FILE = HERE / "puffco_data.json"
DEFAULT_DATA = {"device": None, "charges": [], "sessions": [], "profile_library": [],
                "settings": {"auto_reconnect": True, "notify": True, "on_top": False}}


def load_data():
    try:
        d = json.loads(DATA_FILE.read_text())
        for k, v in DEFAULT_DATA.items():
            d.setdefault(k, v if not isinstance(v, dict) else dict(v))
        for k, v in DEFAULT_DATA["settings"].items():
            d["settings"].setdefault(k, v)
        return d
    except Exception:
        return json.loads(json.dumps(DEFAULT_DATA))


def save_data(d):
    d["charges"] = d["charges"][-200:]
    d["sessions"] = d["sessions"][-3000:]
    tmp = DATA_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(d, indent=1))
        os.replace(tmp, DATA_FILE)
    except Exception:
        pass


def clean_profile(p):
    """Checks a heat profile from the UI. Returns the cleaned profile, or an error message."""
    name = str(p.get("name") or "").strip()
    if not name:
        return "Give the profile a name."
    name = name.encode()[:PROFILE_NAME_MAX].decode(errors="ignore")
    out = {"name": name}
    for key, label in (("temp_f", "Temperature"), ("time", "Time"), ("boost_temp", "Boost temperature"),
                       ("boost_time", "Boost time")):
        v = p.get(key)
        if v is None and key.startswith("boost"):
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            return f"{label} needs a number."
        lo, hi = PROFILE_LIMITS[key]
        if not lo <= v <= hi:
            return f"{label} has to be between {lo} and {hi}" + (" seconds." if "time" in key else "°F.")
        out[key] = round(v, 1)
    if p.get("vapor") is not None:
        try:
            v = float(p["vapor"])
        except (TypeError, ValueError):
            v = None
        if v not in VAPOR:
            return "Pick a vapor level."
        out["vapor"] = v
    return out


def fmt_dur(sec):
    sec = int(max(0, sec))
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"


def fmt_clock(ts):
    return datetime.fromtimestamp(ts).strftime("%I:%M %p").lstrip("0")


def notify(title, body):
    """Windows toast with no extra dependencies (falls back to silence elsewhere)."""
    if not sys.platform.startswith("win"):
        return
    esc = lambda s: s.replace("'", "''")
    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null;"
        "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$x=$t.GetElementsByTagName('text');"
        f"$x.Item(0).AppendChild($t.CreateTextNode('{esc(title)}')) > $null;"
        f"$x.Item(1).AppendChild($t.CreateTextNode('{esc(body)}')) > $null;"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
        "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe')"
        ".Show([Windows.UI.Notifications.ToastNotification]::new($t))"
    )
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-Command", ps],
                         creationflags=0x08000000)  # CREATE_NO_WINDOW
    except Exception:
        pass


# -------------------------------------------------------------- trackers ---
CHARGING_CODES = {0, 1, 3}   # bulk, top-up, paused for temperature
FULL_CODE, UNPLUGGED_CODE = 2, 4


class ChargeTracker:
    """Times each charge, logs it, and predicts when the current one will finish."""

    def __init__(self, data):
        self.data = data
        self.cur = None          # charge in progress
        self.prev_code = None
        self.last_sample = 0

    def update(self, code, pct, src):
        now = time.time()
        events = []
        if code is None or pct is None:
            return events
        charging = code in CHARGING_CODES
        if charging and self.cur is None:
            self.cur = {"start": now, "start_pct": round(pct, 1), "source": CHARGE_SRC.get(src, "?"),
                        "joined_late": self.prev_code is None, "samples": [[0, round(pct, 1)]]}
            self.last_sample = now
            if not self.cur["joined_late"]:
                events.append("started")
        elif self.cur is not None:
            if charging or code == FULL_CODE:
                last_pct = self.cur["samples"][-1][1]
                if round(pct, 1) != last_pct or now - self.last_sample >= 60:
                    self.cur["samples"].append([round(now - self.cur["start"]), round(pct, 1)])
                    self.last_sample = now
            if code == FULL_CODE:
                self._finish(now, pct, complete=True)
                events.append("full")
            elif code == UNPLUGGED_CODE:
                self._finish(now, pct, complete=False)
                events.append("unplugged")
        if src is not None and self.cur is not None:
            self.cur["source"] = CHARGE_SRC.get(src, self.cur["source"])
        self.prev_code = code
        return events

    def _finish(self, now, pct, complete):
        c = self.cur
        c.update(end=now, end_pct=round(pct, 1), duration=round(now - c["start"]), complete=complete)
        if c["duration"] > 60:  # ignore blips
            self.data["charges"].append(c)
            save_data(self.data)
        self.cur = None

    def elapsed(self):
        return time.time() - self.cur["start"] if self.cur else None

    def eta_seconds(self, pct):
        """Seconds until full. Uses how long past charges took from this % to full
        (accounts for the slow top-off at the end); falls back to the current rate."""
        if self.cur is None or pct is None:
            return None, ""
        if pct >= 99.5:
            return 0, ""
        times = []
        for c in reversed(self.data["charges"]):
            if not c.get("complete") or c["start_pct"] > pct + 1:
                continue
            at, prev = None, None
            for t, p in c["samples"]:                 # when this charge passed today's %, interpolated
                if p >= pct:
                    at = prev[0] + (pct - prev[1]) / (p - prev[1]) * (t - prev[0]) if prev and p > prev[1] else t
                    break
                prev = (t, p)
            if at is not None:
                times.append(c["duration"] - at)
            if len(times) >= 10:
                break
        if times:
            times.sort()
            return times[len(times) // 2], "from your past charges"
        el = self.elapsed()
        gained = pct - self.cur["start_pct"]
        if el and el > 180 and gained >= 2:
            rate = gained / el                        # % per second
            return (100 - pct) / rate * 1.15, "rough - learning"  # +15% for the slow top-off
        return None, "learning..."

    def avg_full_charge(self):
        """Average time for a 0-100% charge, scaled from complete charges that covered 30%+."""
        ds = [c["duration"] * 100 / (c["end_pct"] - c["start_pct"])
              for c in self.data["charges"]
              if c.get("complete") and not c.get("joined_late") and c["end_pct"] - c["start_pct"] >= 30]
        return (sum(ds) / len(ds), len(ds)) if ds else (None, 0)


class SessionTracker:
    """Detects heat cycles and logs duration, peak temp and battery used."""

    HOT_F = 300   # works even if the firmware's state codes differ

    def __init__(self, data):
        self.data = data
        self.cur = None
        self.last_cool = None

    def update(self, state, temp, pct):
        now = time.time()
        hot = (state in HEATING) or (temp is not None and temp >= self.HOT_F)
        ev = None
        if hot and self.cur is None:
            self.cur = {"start": now, "peak": temp or 0,
                        "bat_start": pct if pct is not None else None}
        elif hot and self.cur is not None:
            if temp:
                self.cur["peak"] = max(self.cur["peak"], temp)
        elif not hot and self.cur is not None:
            c = self.cur
            self.cur = None
            dur = now - c["start"]
            if dur >= 15:
                used = (c["bat_start"] - pct) if (c["bat_start"] is not None and pct is not None) else None
                if used is not None and used < 0:   # was charging during the session
                    used = None
                self.data["sessions"].append({"time": c["start"], "duration": round(dur),
                                              "peak_f": round(c["peak"]),
                                              "battery_used": round(used, 1) if used is not None else None})
                save_data(self.data)
                ev = "session"
        return ev

    def today(self):
        midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        return sum(1 for s in self.data["sessions"] if s["time"] >= midnight)

    def avg_battery_per_dab(self):
        u = [s["battery_used"] for s in self.data["sessions"][-50:]
             if s.get("battery_used") is not None and 0 < s["battery_used"] < 30]
        return sum(u) / len(u) if u else None


# --------------------------------------------------------------- overlay ---
RIGS_DIR = HERE / "rigs"
OVERLAY_DEFAULTS = {
    # rig
    "edition": "", "rig_scale": 1.0, "offline_mode": "dim", "reflection": True, "shadow": True, "pad": True,
    "knob_anim": True, "knob_amount": 1.0, "bubbles": True,
    # vapor
    "smoke": "session", "inhale": 10.0, "exhale": 4.0, "wait": 1.0, "first_delay": 0.5,
    "smoke_amount": 1.0, "smoke_size": 1.0, "smoke_speed": 1.0, "smoke_swirl": 1.0, "smoke_linger": 1.0,
    "quality": "high", "smoke_tint": True, "cooldown_wisps": True, "detect": False,
    # pop-up & motion
    "visibility": "popup", "linger": 8.0, "stay_heating": True, "stay_charging": False,
    "pop_heat": True, "pop_session": True, "pop_cool": True, "pop_charge": True, "pop_full": True, "pop_connect": True,
    "entrance": "left", "anim_speed": 1.0, "bounce": 0.4, "lean": True, "motion_blur": True,
    # info bar
    "label": True, "bar_mode": "always", "bar_scale": 1.0, "bar_gap": 0.0, "bar_opacity": 0.82,
    "show_dot": True, "show_temp": True, "battery": True, "show_progress": True,
    "txt_standby": "Standby", "txt_heating": "Heating", "txt_active": "Boofin", "txt_cool": "Cooling down",
    "txt_charging": "Charging", "txt_charged": "Charged", "txt_off": "Offline",
    # lighting
    "light": "edition", "light_intensity": 1.0, "glass_glow": 1.0, "rainbow_speed": 1.0,
    "standby_breathe": True, "heat_fill": True, "heater_glow": True, "backlight": True, "floor_glow": True,
}
LIGHT_PRESETS = {"Match edition": "edition", "Pink": "#ff5ca8", "Purple": "#9b7bff", "Blue": "#4aa8ff",
                 "Teal": "#36e2d0", "Green": "#5cf08a", "Orange": "#ff8a3c", "Red": "#ff4a4a",
                 "White": "#f4f1ff", "Rainbow": "rainbow"}
SMOKE_MODES = {"Rhythm: smoke, pause, wait, repeat": "session", "Nonstop cloud": "constant", "No vapor": "off"}
RHYTHM = ("smoke", "session")
POPUP = ("visibility", "popup")

OV_PAGES = [
    ("Rig", [
        {"k": "edition", "t": "edition", "label": "Edition"},
        {"k": "rig_scale", "t": "pct", "label": "Size", "lo": 50, "hi": 150},
        {"k": "offline_mode", "t": "choice", "label": "When disconnected",
         "opts": {"Dim it": "dim", "Hide it": "hide", "Show it like normal": "normal"}},
        {"t": "head", "label": "Details"},
        {"k": "reflection", "t": "bool", "label": "Reflection on the floor"},
        {"k": "shadow", "t": "bool", "label": "Drop shadow"},
        {"k": "pad", "t": "bool", "label": "Charging pad slides in while it's charging"},
        {"k": "bubbles", "t": "bool", "label": "Bubbles in the glass while it's smoking"},
        {"k": "knob_anim", "t": "bool", "label": "Carb cap rocks back and forth during a session"},
        {"k": "knob_amount", "t": "pct", "label": "Carb cap rocking", "lo": 25, "hi": 250, "when": ("knob_anim", True)},
    ]),
    ("Vapor", [
        {"k": "smoke", "t": "choice", "label": "Vapor", "opts": SMOKE_MODES},
        {"t": "head", "label": "Rhythm"},
        {"k": "inhale", "t": "secs", "label": "Smoke (inhale)", "lo": 1, "hi": 30, "step": 0.5, "when": RHYTHM},
        {"k": "exhale", "t": "secs", "label": "Pause (exhale)", "lo": 0, "hi": 20, "step": 0.5, "when": RHYTHM},
        {"k": "wait", "t": "secs", "label": "Wait", "lo": 0, "hi": 10, "step": 0.5, "when": RHYTHM},
        {"k": "first_delay", "t": "secs", "label": "First smoke after ready", "lo": 0, "hi": 10, "step": 0.5,
         "when": ("smoke", ("session", "constant"))},
        {"t": "head", "label": "The cloud"},
        {"k": "smoke_amount", "t": "pct", "label": "Thickness", "lo": 25, "hi": 200},
        {"k": "smoke_size", "t": "pct", "label": "Size", "lo": 50, "hi": 200},
        {"k": "smoke_speed", "t": "pct", "label": "Speed", "lo": 25, "hi": 200},
        {"k": "smoke_swirl", "t": "pct", "label": "Swirl", "lo": 0, "hi": 250},
        {"k": "smoke_linger", "t": "pct", "label": "Hangs around", "lo": 25, "hi": 300},
        {"k": "quality", "t": "choice", "label": "Quality",
         "opts": {"High (sharpest)": "high", "Medium": "medium", "Low (easiest on your PC)": "low"}},
        {"k": "smoke_tint", "t": "bool", "label": "Tint the vapor with the light color as it leaves the glass"},
        {"k": "cooldown_wisps", "t": "bool", "label": "Last few wisps after the session ends"},
        {"k": "detect", "t": "bool", "label": "Also start the smoke when a real draw is detected (beta)"},
    ]),
    ("Pop-up & motion", [
        {"k": "visibility", "t": "choice", "label": "Show the rig",
         "opts": {"Pop up when something changes, then go away": "popup",
                  "Only while it's heating and boofin": "session",
                  "Always": "always", "Never (info bar only)": "never"}},
        {"k": "linger", "t": "secs", "label": "Stays up for", "lo": 2, "hi": 120, "step": 1,
         "when": ("visibility", ("popup", "session"))},
        {"k": "stay_heating", "t": "bool", "label": "Stay up the whole heat-up and session", "when": POPUP},
        {"k": "stay_charging", "t": "bool", "label": "Stay up the whole time it's charging", "when": POPUP},
        {"t": "head", "label": "Pop up when"},
        {"k": "pop_heat", "t": "bool", "label": "It starts heating", "when": POPUP},
        {"k": "pop_session", "t": "bool", "label": "It's ready (boofin)", "when": POPUP},
        {"k": "pop_cool", "t": "bool", "label": "The session ends", "when": POPUP},
        {"k": "pop_charge", "t": "bool", "label": "It goes on or comes off the charger", "when": POPUP},
        {"k": "pop_full", "t": "bool", "label": "It's fully charged", "when": POPUP},
        {"k": "pop_connect", "t": "bool", "label": "It connects or disconnects", "when": POPUP},
        {"t": "head", "label": "Animation"},
        {"k": "entrance", "t": "choice", "label": "Comes in by",
         "opts": {"Swooping in from the left": "left", "Swooping in from the right": "right",
                  "Rising up from below": "up", "Dropping in from above": "down",
                  "Zooming in": "zoom", "Fading in": "fade", "Just appearing": "none"}},
        {"k": "anim_speed", "t": "pct", "label": "Speed", "lo": 25, "hi": 300},
        {"k": "bounce", "t": "pct", "label": "Bounce", "lo": 0, "hi": 100},
        {"k": "lean", "t": "bool", "label": "Leans into the motion when it swoops"},
        {"k": "motion_blur", "t": "bool", "label": "Motion blur"},
    ]),
    ("Info bar", [
        {"k": "label", "t": "bool", "label": "Show the info bar"},
        {"k": "bar_mode", "t": "choice", "label": "Info bar is",
         "opts": {"Always on screen": "always", "Comes and goes with the rig": "with_rig"}, "when": ("label", True)},
        {"k": "bar_scale", "t": "pct", "label": "Size", "lo": 50, "hi": 250, "when": ("label", True)},
        {"k": "bar_gap", "t": "pct", "label": "Gap below the rig", "lo": -10, "hi": 25, "step": 1, "when": ("label", True)},
        {"k": "bar_opacity", "t": "pct", "label": "Background", "lo": 0, "hi": 100, "step": 1, "when": ("label", True)},
        {"t": "head", "label": "Show"},
        {"k": "show_dot", "t": "bool", "label": "Status dot", "when": ("label", True)},
        {"k": "show_temp", "t": "bool", "label": "Temperature", "when": ("label", True)},
        {"k": "battery", "t": "bool", "label": "Battery %", "when": ("label", True)},
        {"k": "show_progress", "t": "bool", "label": "Heat-up progress bar", "when": ("label", True)},
        {"t": "head", "label": "Wording"},
        {"k": "txt_standby", "t": "text", "label": "Standby"},
        {"k": "txt_heating", "t": "text", "label": "Heating up"},
        {"k": "txt_active", "t": "text", "label": "Ready / session"},
        {"k": "txt_cool", "t": "text", "label": "Cooling down"},
        {"k": "txt_charging", "t": "text", "label": "Charging"},
        {"k": "txt_charged", "t": "text", "label": "Fully charged"},
        {"k": "txt_off", "t": "text", "label": "Disconnected"},
    ]),
    ("Lighting", [
        {"k": "light", "t": "light", "label": "Light color"},
        {"k": "light_intensity", "t": "pct", "label": "Base light", "lo": 0, "hi": 150},
        {"k": "glass_glow", "t": "pct", "label": "Glass glow", "lo": 0, "hi": 150},
        {"k": "rainbow_speed", "t": "pct", "label": "Rainbow speed", "lo": 10, "hi": 300, "when": ("light", "rainbow")},
        {"t": "head", "label": "Effects"},
        {"k": "standby_breathe", "t": "bool", "label": "Light slowly breathes in standby"},
        {"k": "heat_fill", "t": "bool", "label": "Light fills up from the front as it heats"},
        {"k": "heater_glow", "t": "bool", "label": "Warm glow at the bottom of the glass"},
        {"k": "backlight", "t": "bool", "label": "Soft halo behind the rig"},
        {"k": "floor_glow", "t": "bool", "label": "Light spilling onto the floor"},
    ]),
]

APP_DEFAULTS = {"auto_reconnect": True, "notify": True, "notify_ready": True, "notify_full": True,
                "notify_low": True, "low_pct": 15, "units": "F", "on_top": False,
                "startup": False, "start_min": True, "close_to_tray": True}
NOTIFY = ("notify", True)
APP_FIELDS = [
    {"t": "head", "label": "Connection"},
    {"k": "auto_reconnect", "t": "bool", "label": "Connect automatically and stay connected"},
    {"t": "head", "label": "Notifications"},
    {"k": "notify", "t": "bool", "label": "Windows notifications"},
    {"k": "notify_ready", "t": "bool", "label": "When it's done heating", "when": NOTIFY},
    {"k": "notify_full", "t": "bool", "label": "When it's fully charged", "when": NOTIFY},
    {"k": "notify_low", "t": "bool", "label": "When the battery is getting low", "when": NOTIFY},
    {"k": "low_pct", "t": "num", "label": "Low battery at", "lo": 5, "hi": 50, "step": 1, "fmt": "{:.0f}%",
     "when": [NOTIFY, ("notify_low", True)]},
    {"t": "head", "label": "Display"},
    {"k": "units", "t": "choice", "label": "Temperature", "opts": {"Fahrenheit (°F)": "F", "Celsius (°C)": "C"}},
    {"k": "on_top", "t": "bool", "label": "Keep this window on top"},
    {"t": "head", "label": "Running in the background"},
    {"k": "close_to_tray", "t": "choice", "label": "Closing the window",
     "opts": {"Keeps it running in the tray": True, "Quits Puffco Monitor": False}},
    {"k": "startup", "t": "bool", "label": "Start Puffco Monitor when Windows starts"},
    {"k": "start_min", "t": "bool", "label": "Start it quietly in the tray", "when": ("startup", True)},
    {"t": "head", "label": "Data"},
    {"t": "buttons", "items": [("Clear charge history", "clear_charges"), ("Clear session history", "clear_sessions"),
                               ("Reset all overlay settings", "reset_overlay"), ("Open the Puffco folder", "open_folder")]},
    {"t": "buttons", "items": [("Quit Puffco Monitor", "quit")]},
]
KNOWN_IDLE = {0, 1, 2, 3, 4, 5, 6, 10, 11, 12, 13}


def ensure_default_rigs():
    src = RES / "rigs"
    try:
        if src.is_dir() and src.resolve() != RIGS_DIR.resolve():
            for ed in src.iterdir():
                if ed.is_dir() and not (RIGS_DIR / ed.name).exists():
                    shutil.copytree(ed, RIGS_DIR / ed.name)
    except OSError:
        log.exception("copying default editions")


def list_editions():
    if not RIGS_DIR.is_dir():
        return []
    return sorted(p.name for p in RIGS_DIR.iterdir() if (p / "rig.json").is_file() and (p / "rig.png").is_file())


class PhaseDetector:
    """Works out standby / heating / session / cooldown for the overlay, plus a hit guess.
    Uses the device's state code when it's one of the documented heat-cycle codes and
    the chamber temperature trend otherwise, so it still works if a firmware numbers
    its states differently."""

    HIT_DROP_F = 3.5       # a draw pulls cool air over the bowl - the temp dips a few degrees

    def __init__(self):
        self.hist = deque(maxlen=80)
        self.phase = "off"
        self.hit_until = 0.0
        self.hit_id = 0

    def reset(self):
        self.phase = "off"
        self.hist.clear()

    def _window(self, now, secs):
        return [t for ts, t in self.hist if now - ts <= secs]

    def update(self, state, temp):
        now = time.time()
        if temp is not None:
            self.hist.append((now, temp))
        if not self.hist:
            self.phase = "standby"
            return
        t = self.hist[-1][1]
        w2 = [(ts, x) for ts, x in self.hist if now - ts <= 2.5]
        slope = (w2[-1][1] - w2[0][1]) / max(0.5, w2[-1][0] - w2[0][0]) if len(w2) >= 2 else 0.0
        drop10 = max(self._window(now, 10) or [t]) - t
        prev = self.phase
        if state == 7:
            ph = "heating"
        elif state == 8:
            ph = "active" if t >= 250 else "heating"
        elif state == 9:
            ph = "cooldown"
        elif t < 150:
            ph = "standby"
        elif slope > 2.5 and prev != "active":
            ph = "heating"
        elif prev == "active":
            ph = "cooldown" if drop10 >= 40 or (state in KNOWN_IDLE and slope < -0.5 and drop10 >= 15) else "active"
        elif prev == "heating":
            ph = "cooldown" if slope < -1 else ("active" if t >= 380 and abs(slope) < 2.5 else "heating")
        elif prev == "cooldown":
            ph = "cooldown"
        else:
            ph = "standby" if state in KNOWN_IDLE else ("active" if t >= 380 else "standby")
        if ph == "active" and temp is not None:
            recent = self._window(now, 2.5)
            if recent and max(recent) - temp >= self.HIT_DROP_F:
                if now >= self.hit_until:
                    self.hit_id += 1
                self.hit_until = now + 2.5
        self.phase = ph


class OverlayServer:
    """Local web server: the OBS overlay (/overlay, /state, /rigs/...) and the app window (/app, /api/...).
    Only listens on 127.0.0.1. Changes need a custom header, which other websites can't send."""

    def __init__(self, app, port=8765):
        self.app, self.port, self.httpd = app, None, None
        for p in range(port, port + 6):
            try:
                self.httpd = ThreadingHTTPServer(("127.0.0.1", p), self._handler())
                self.port = p
                break
            except OSError:
                continue
        if self.httpd:
            threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/overlay" if self.port else ""

    def stop(self):
        if self.httpd:
            threading.Thread(target=self.httpd.shutdown, daemon=True).start()

    def _handler(self):
        app = self.app

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, code, body, ctype):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def send_json(self, obj, code=200):
                body = json.dumps(obj, default=str).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                path = urllib.parse.urlsplit(self.path).path
                try:
                    if self.headers.get("X-Puffco") != "1":
                        return self.send(403, b"forbidden", "text/plain")
                    n = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                    if path == "/api/do":
                        return self.send_json(app.do(body.get("a"), body))
                    if path == "/api/ov":
                        app.set_ov(body)
                        return self.send_json({"ok": True})
                    if path == "/api/cfg":
                        app.set_cfg(body)
                        return self.send_json({"ok": True})
                    if path == "/api/edition":
                        return self.send_json(app.add_edition(body.get("name", ""), body.get("image", "")))
                    return self.send(404, b"not found", "text/plain")
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception as e:
                    log.exception("POST %s", path)
                    try:
                        self.send_json({"ok": False, "error": str(e)}, 500)
                    except Exception:
                        pass

            def do_GET(self):
                path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
                try:
                    if path in ("/", "/overlay", "/overlay.html"):
                        f = RES / "overlay.html"
                        if not f.is_file():
                            return self.send(404, b"overlay.html is missing", "text/plain")
                        return self.send(200, f.read_bytes(), "text/html; charset=utf-8")
                    if path in ("/app", "/app.html"):
                        return self.send(200, (RES / "app.html").read_bytes(), "text/html; charset=utf-8")
                    if path in ("/icon.png", "/favicon.ico"):
                        f = RES / ("icon.png" if path == "/icon.png" else "icon.ico")
                        return self.send(200, f.read_bytes(), "image/png" if path == "/icon.png" else "image/x-icon")
                    if path == "/api/ping":
                        return self.send_json({"app": "puffco-monitor", "version": VERSION})
                    if path == "/api/state":
                        return self.send_json(app.snapshot())
                    if path == "/api/schema":
                        return self.send_json({"ov_pages": OV_PAGES, "app_fields": APP_FIELDS,
                                               "ov_defaults": OVERLAY_DEFAULTS, "app_defaults": APP_DEFAULTS,
                                               "light_presets": LIGHT_PRESETS})
                    if path == "/state":
                        return self.send(200, json.dumps(app.overlay_state()).encode(), "application/json")
                    if path in ("/rigs", "/rigs/"):
                        return self.send(200, json.dumps(list_editions()).encode(), "application/json")
                    if path.startswith("/rigs/"):
                        f = (RIGS_DIR / path[6:]).resolve()
                        if RIGS_DIR.resolve() in f.parents and f.is_file() and f.suffix in (".png", ".json"):
                            return self.send(200, f.read_bytes(), "image/png" if f.suffix == ".png" else "application/json")
                    return self.send(404, b"not found", "text/plain")
                except (BrokenPipeError, ConnectionResetError):
                    pass

        return Handler


# ------------------------------------------------------------- BLE worker ---
class Worker:
    """Runs all Bluetooth work on its own asyncio thread; talks to the UI via a queue."""

    def __init__(self, out_q):
        self.q = out_q
        self.loop = asyncio.new_event_loop()
        self.dev = None
        self.poll_task = None
        self.link_task = None
        self.want = None          # address we're trying to stay connected to
        self.auto = True
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    def submit(self, coro):
        asyncio.run_coroutine_threadsafe(coro, self.loop)

    def emit(self, kind, payload=None):
        self.q.put((kind, payload))

    async def scan(self):
        self.emit("status", "Scanning for Puffco devices...")
        try:
            found = await BleakScanner.discover(timeout=5.0)
        except Exception as e:
            self.emit("status", f"Bluetooth error: {e}")
            return
        hits = [(d.name or "Unknown", d.address) for d in found
                if any(h in (d.name or "").lower() for h in NAME_HINTS)]
        self.emit("devices", hits)
        self.emit("status", f"Found {len(hits)} device(s)." if hits else
                  "No Puffco found. Wake the rig (press the button) and close the phone app.")

    async def start_link(self, address):
        """Connect and keep reconnecting whenever the rig drops (sleep, out of range)."""
        await self.stop_link(quiet=True)
        self.want = address
        self.link_task = asyncio.ensure_future(self._link(address))

    async def _link(self, address):
        first = True
        while self.want == address:
            if self.dev is None:
                if not first and not self.auto:
                    self.want = None
                    self.emit("link", "off")
                    self.emit("status", "Connection lost. Auto-reconnect is off.")
                    return
                self.emit("link", "connecting" if first else "waiting")
                self.emit("status", "Connecting..." if first else
                          "Waiting for the rig to wake up - will reconnect automatically.")
                ok = await self._connect_once(address)
                first = False
                if not ok:
                    await asyncio.sleep(4)
                    continue
            elif not self.dev.client.is_connected or (self.poll_task and self.poll_task.done()):
                await self._teardown()
                continue
            await asyncio.sleep(1)

    async def _connect_once(self, address):
        try:
            device = await BleakScanner.find_device_by_address(address, timeout=6.0)
        except Exception as e:
            self.emit("status", f"Bluetooth error: {e}")
            return False
        if device is None:
            return False
        dev = Lorax(device, lambda: None)
        try:
            await dev.connect()
        except Exception as e:
            await dev.disconnect()
            self.emit("status", f"Couldn't connect: {e}")
            return False
        self.dev = dev
        self.emit("link", "connected")
        self.emit("status", "Connected.")
        await self.read_static()
        await self.read_profiles()
        self.poll_task = asyncio.ensure_future(self.poll())
        return True

    async def _teardown(self):
        if self.poll_task:
            self.poll_task.cancel()
            self.poll_task = None
        if self.dev:
            await self.dev.disconnect()
            self.dev = None

    async def stop_link(self, quiet=False):
        self.want = None
        if self.link_task:
            self.link_task.cancel()
            self.link_task = None
        await self._teardown()
        if not quiet:
            self.emit("link", "off")
            self.emit("status", "Disconnected.")

    async def read_static(self):
        d, info = self.dev, {}
        if (b := await d.read("/u/sys/name")):
            info["name"] = text(b)
        if (b := await d.read("/p/sys/hw/ser")):
            info["serial"] = text(b)
        if (b := await d.read("/p/sys/fw/ver")):
            info["firmware"] = fw_string(b[0])
        if (b := await d.read("/p/sys/hw/mdcd", 4)) and u32(b) is not None:
            info["model"] = MODELS.get(u32(b), f"Puffco (model {u32(b)})")
        self.emit("static", info)

    async def poll(self):
        tick = 0
        fails = 0
        while self.dev and self.dev.client.is_connected:
            d, v = self.dev, {}
            try:
                if (b := await d.read("/p/app/stat/id", 1)):
                    v["state"] = b[0]
                if (b := await d.read("/p/app/htr/temp", 4)) and (t := f32(b)) and 0 < t < 500:
                    v["temp"] = c_to_f(t)
                if (b := await d.read("/p/app/htr/ttag", 4)) and (t := f32(b)) and 0 < t < 500:
                    v["target"] = c_to_f(t)
                pct = num(await d.read("/p/bat/soc", 4))
                if not pct or pct > 100:  # some firmware keeps the % here instead
                    alt = num(await d.read("/p/bat/cap", 4))
                    if alt is not None and 0 <= alt <= 100:
                        pct = alt
                if pct is not None:
                    v["battery"] = pct
                if (b := await d.read("/p/bat/chg/stat", 1)):
                    v["charge"] = b[0]
                if (b := await d.read("/p/bat/chg/src", 1)):
                    v["charge_src"] = b[0]

                if tick % 10 == 0:  # slower-changing stuff
                    if (b := await d.read("/p/htr/chmt", 1)):
                        v["chamber"] = b[0]
                    dabs = num(await d.read("/p/app/odom/0/nc", 4))
                    if dabs is None:
                        dabs = num(await d.read("/p/app/info/dtot", 4))
                    if dabs is not None:
                        v["total_dabs"] = int(round(dabs))
                    if (x := num(await d.read("/p/app/info/dpd", 4))) is not None:
                        v["dpd"] = x
                    if (x := num(await d.read("/p/app/info/drem", 4))) is not None:
                        v["drem"] = x
                    if "target" not in v and (x := num(await d.read("/p/app/thc/temp", 4))) and 0 < x < 500:
                        v["target"] = c_to_f(x)            # active heat profile's temperature
                    if (b := await d.read("/p/app/thc/name")) and text(b):
                        v["profile"] = text(b)
                    if (b := await d.read("/p/app/hcs", 1)):
                        v["profile_slot"] = b[0]
                    for key, path in (("volt", "/p/bat/volt"), ("bat_temp", "/p/bat/temp"), ("current", "/p/bat/curr"),
                                      ("cap", "/p/bat/cap"), ("heat_time", "/p/app/odom/0/tm"),
                                      ("dabs2", "/p/app/odom/1/nc"), ("heat_time2", "/p/app/odom/1/tm"),
                                      ("uptime", "/p/sys/uptm"), ("chg_elap", "/p/bat/chg/elap")):
                        if (x := num(await d.read(path, 4))) is not None:
                            v[key] = x
                fails = fails + 1 if not v else 0
                if fails >= 5:  # link is dead even if Windows hasn't noticed yet
                    return
                if v:
                    self.emit("live", v)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.emit("status", f"Read error: {e}")
            tick += 1
            hot = v.get("state") in HEATING or (v.get("temp") or 0) >= 150
            await asyncio.sleep(0.3 if hot else 1.5)

    async def read_profiles(self):
        d, out = self.dev, []
        if not d:
            return

        async def f(path):
            b = await d.read(path, 4)
            return f32(b) if b else None

        for i in range(PROFILE_SLOTS):
            base = f"/u/app/hc/{i}/"
            b = await d.read(base + "name")
            temp = await f(base + "temp")
            p = {"slot": i, "name": text(b) if b else f"Profile {i + 1}",
                 "temp_f": round(c_to_f(temp)) if temp else None, "time": await f(base + "time")}
            for key, leaf in (("vapor", "intn"), ("boost_temp", "btmp"), ("boost_time", "btim")):
                if (x := await f(base + leaf)) is not None:   # older firmware doesn't have these
                    p[key] = round(x, 1)
            out.append(p)
        self.emit("profiles", out)

    async def write_profile(self, slot, p):
        """Saves one heat profile slot. `p` has already been checked by Core.save_profile."""
        d = self.dev
        if not d:
            self.emit("status", "Connect to your rig first.")
            return
        base = f"/u/app/hc/{slot}/"
        try:
            await d.write(base + "name", p["name"].encode() + b"\x00")
            await d.write(base + "temp", struct.pack("<f", (p["temp_f"] - 32) * 5 / 9))
            await d.write(base + "time", struct.pack("<f", p["time"]))
            for key, leaf in (("vapor", "intn"), ("boost_temp", "btmp"), ("boost_time", "btim")):
                if key in p:
                    await d.write(base + leaf, struct.pack("<f", p[key]))
            # the rig keeps a working copy of the selected profile: reselect it so the change applies now
            if (b := await d.read("/p/app/hcs", 1)) and b[0] == slot:
                await d.write("/p/app/hcs", bytes([slot]))
            self.emit("status", f"Saved “{p['name']}” to slot {slot + 1}.")
        except Exception as e:
            self.emit("status", f"Couldn't save slot {slot + 1}: {e}")
        await self.read_profiles()

    async def read_path(self, path):
        if not self.dev:
            self.emit("log", "Not connected.")
            return
        b = await self.dev.read(path)
        self.emit("log", f"{path}\n    {describe_raw(b) if b is not None else 'no such path'}")

    async def probe(self):
        if not self.dev:
            self.emit("log", "Not connected.")
            return
        self.emit("log", f"Trying {len(PROBE_PATHS)} paths...\n")
        hits = 0
        for p in PROBE_PATHS:
            b = await self.dev.read(p)
            if b is not None:
                hits += 1
                self.emit("log", f"{p}\n    {describe_raw(b)}")
        self.emit("log", f"\nDone - {hits} of {len(PROBE_PATHS)} paths answered. "
                         "Run it again after charging or a session and compare to see what changes.\n")


# ------------------------------------------------------------------- core ---
class Core:
    """Everything except the window: Bluetooth, trackers, estimates, settings and the local server.
    The window (or a browser) just shows /app and talks to this through the small JSON API."""

    def __init__(self):
        self.lock = threading.RLock()
        self.q = queue.Queue()
        self.data = load_data()
        self.charge = ChargeTracker(self.data)
        self.sessions = SessionTracker(self.data)
        self.devices = []
        self.link = "off"
        self.last = {}                 # latest value of every live field
        self.static = {}               # name / model / serial / firmware
        self.profiles = []             # the rig's 4 heat profile slots, as last read
        self.status, self.status_t = "", 0.0
        self.log, self.log_seq = deque(maxlen=600), 0
        self.low_warned = self.was_ready = False
        st = self.data["settings"]
        self.cfg = {k: st.get(k, v) for k, v in APP_DEFAULTS.items()}
        saved_ov = dict(st.get("overlay", {}))
        if "swoop" in saved_ov and "entrance" not in saved_ov:       # older settings
            saved_ov["entrance"] = saved_ov["swoop"]
        self.ov = dict(OVERLAY_DEFAULTS, **{k: v for k, v in saved_ov.items() if k in OVERLAY_DEFAULTS})
        if self.ov.get("smoke") not in SMOKE_MODES.values():
            self.ov["smoke"] = "session"
        self.ov_custom = st.get("overlay_custom", "#ff5ca8")
        self.phase = PhaseDetector()
        self.preview, self.preview_hit_until, self.preview_hit_id = None, 0.0, 0
        self.fcc_samples, self.dpc_samples = deque(maxlen=40), deque(maxlen=40)
        self.elap_samples, self.elap_unit = deque(maxlen=200), None
        self.health = {}
        self.ui = None                 # the window host, once there is one
        self.quitting = False
        self._save_timer = None
        self._accent_cache = (None, None)
        ensure_default_rigs()
        self.server = OverlayServer(self)
        self.worker = Worker(self.q) if BleakClient is not None else None
        threading.Thread(target=self._pump, daemon=True).start()
        if not self.server.port:
            self.set_status("Couldn't start the overlay server - ports 8765-8770 are all in use.")
        if self.cfg["startup"]:
            self.update_startup()          # keep the Windows startup entry pointing at this copy (e.g. the new exe)
        if self.worker is None:
            self.set_status("Bluetooth support is missing - install it with:  pip install bleak")
        else:
            self.worker.auto = self.cfg["auto_reconnect"]
            saved = self.data.get("device")
            if saved:
                self.devices = [(saved["name"], saved["address"])]
                if self.cfg["auto_reconnect"]:
                    self.worker.submit(self.worker.start_link(saved["address"]))
            else:
                self.worker.submit(self.worker.scan())

    # -- events from the Bluetooth thread ----------------------------------------
    def _pump(self):
        while True:
            kind, payload = self.q.get()
            try:
                with self.lock:
                    getattr(self, f"on_{kind}")(payload)
            except Exception:
                log.exception("event %s failed", kind)

    def set_status(self, text):
        self.status, self.status_t = text, time.time()

    def on_status(self, text):
        self.set_status(text)

    def on_log(self, text):
        for line in str(text).split("\n"):
            self.log_seq += 1
            self.log.append([self.log_seq, line])

    def on_devices(self, hits):
        self.devices = hits

    def on_link(self, state):
        prev, self.link = self.link, state
        if state == "waiting" and prev == "connected":
            self.charge.prev_code = None      # we can't see what happens while disconnected
        if state != "connected":
            self.phase.reset()
            self.profiles = []

    def on_profiles(self, profiles):
        self.profiles = profiles

    def on_static(self, info):
        self.static.update({k: v for k, v in info.items() if v})

    def on_live(self, v):
        self.last.update(v)
        L = self.last
        for ev in self.charge.update(v.get("charge"), L.get("battery"), L.get("charge_src")):
            if ev == "full":
                self.alert("Puffco fully charged", f"Battery at {L.get('battery', 100):.0f}%.", "full")
            if ev in ("started", "full", "unplugged"):
                self.low_warned = False
        self.phase.update(L.get("state"), v.get("temp"))
        self.sessions.update(L.get("state"), v.get("temp"), L.get("battery"))
        ready = L.get("state") == 8 and (L.get("temp") or 0) >= 300
        if ready and not self.was_ready:
            self.alert("Puffco is ready", "Heat-up finished - time to dab.", "ready")
        self.was_ready = ready
        bat = L.get("battery")
        if bat is not None and bat <= self.cfg["low_pct"] and not self.charge.cur and not self.low_warned:
            self.low_warned = True
            self.alert("Puffco battery low", f"{bat:.0f}% left - time to charge.", "low")
        if "chg_elap" in v:
            self.learn_elap_unit(v["chg_elap"])
        if any(k in v for k in ("volt", "bat_temp", "current", "cap", "drem", "battery")):
            self.update_health()

    def on_edition_added(self, info):
        name, view, err = info
        if err:
            self.set_status(f"Couldn't add “{name}”: {err}")
            return
        self.ov["edition"] = name
        self.save_settings()
        self.set_status(f"Added “{name}” ({view} view).")

    def learn_elap_unit(self, val):
        """The charger's elapsed timer has no documented unit - work it out from how fast it counts."""
        now = time.time()
        if self.last.get("charge") in CHARGING_CODES:
            if self.elap_samples and val < self.elap_samples[-1][1]:
                self.elap_samples.clear()
            self.elap_samples.append((now, val))
            t0, v0 = self.elap_samples[0]
            if now - t0 >= 90 and self.elap_unit is None:
                rate = (val - v0) / (now - t0)
                self.elap_unit = 1 if 0.6 < rate < 1.4 else 60 if 0.01 < rate < 0.025 else None
        else:
            self.elap_samples.clear()

    def update_health(self):
        L, h = self.last, self.health
        soc, cap, drem = L.get("battery"), L.get("cap"), L.get("drem")
        bh = self.data.setdefault("battery", {})
        if soc and cap and 15 <= soc <= 95 and cap > 0:
            self.fcc_samples.append(cap / (soc / 100))
        if soc and drem and soc >= 10:
            self.dpc_samples.append(drem / (soc / 100))
        med = lambda xs: sorted(xs)[len(xs) // 2] if xs else None
        fcc, dpc = med(self.fcc_samples), med(self.dpc_samples)
        if fcc:
            flog = bh.setdefault("fcc_log", [])
            if len(self.fcc_samples) >= 5:
                day = datetime.now().strftime("%Y-%m-%d")
                if not flog or flog[-1][0] != day:
                    flog.append([day, round(fcc)])
                    save_data(self.data)
                else:
                    flog[-1][1] = round(fcc)
            best = max([x for _, x in flog] + [fcc])
            h["pct"], h["wh"] = 100 * fcc / best, fcc / 1000
        if dpc:
            h["dpc"] = dpc
            if L.get("total_dabs"):
                h["cycles"] = L["total_dabs"] / dpc
        if L.get("volt"):
            h["volt"], h["cells"] = L["volt"], max(1, min(4, round(L["volt"] / 3.7)))
        h["tracked"] = sum(max(0, c["end_pct"] - c["start_pct"]) for c in self.data["charges"]) / 100

    # -- the overlay ----------------------------------------------------------------
    def overlay_state(self):
        now = time.time()
        with self.lock:
            L = dict(self.last)
            connected = self.link == "connected"
            phase = self.phase.phase if connected else "off"
            hit = connected and phase == "active" and now < self.phase.hit_until
            code = L.get("charge")
            temp, battery = L.get("temp"), L.get("battery")
            on_charger, full, charging = code in (0, 1, 2, 3), code == 2, code in CHARGING_CODES
            if self.preview:
                p = self.preview
                connected, phase = True, ("standby" if p == "charging" else p)
                on_charger = charging = p == "charging"
                full, hit = False, False
                temp = {"standby": 78, "charging": 78, "active": 522, "cooldown": 430}.get(p, 78 + (now * 45) % 440)
                battery = battery if battery is not None else 72
            if now < self.preview_hit_until:
                connected, phase, hit = True, "active", True
                temp = temp if temp and temp > 300 else 506
            return {"connected": connected, "phase": phase, "temp": temp, "target": L.get("target"),
                    "battery": battery, "charging": charging, "on_charger": on_charger, "charge_full": full,
                    "hit": hit, "hit_id": self.phase.hit_id, "puff_id": self.preview_hit_id,
                    "settings": dict(self.ov, units=self.cfg["units"])}

    def edition_accent(self):
        name = self.ov.get("edition")
        if self._accent_cache[0] != name:
            try:
                acc = json.loads((RIGS_DIR / name / "rig.json").read_text()).get("accent")
            except Exception:
                acc = None
            self._accent_cache = (name, acc)
        return self._accent_cache[1] or "#ff8a9a"

    # -- what the app window shows -----------------------------------------------
    def snapshot(self):
        now = time.time()
        with self.lock:
            L = dict(self.last)
            ch = None
            el = self.charge.elapsed()
            if el is not None:
                note = "since the app opened" if self.charge.cur.get("joined_late") else ""
                if note and self.elap_unit and L.get("chg_elap") is not None:
                    el, note = L["chg_elap"] * self.elap_unit, ""
                eta, how = self.charge.eta_seconds(L.get("battery"))
                ch = {"elapsed": el, "note": note, "eta": eta, "how": how,
                      "clock": fmt_clock(now + eta) if eta else None}
            avg, n = self.charge.avg_full_charge()
            sess = self.data["sessions"]
            recent = sess[-20:]
            day0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            per_day = []
            for i in range(13, -1, -1):
                a, b = day0 - i * 86400, day0 - (i - 1) * 86400
                per_day.append([datetime.fromtimestamp(a).strftime("%a %d"), sum(1 for s in sess if a <= s["time"] < b)])
            return {
                "now": now, "link": self.link, "status": self.status, "status_t": self.status_t,
                "devices": self.devices, "device": self.data.get("device"), "static": self.static,
                "live": L, "profiles": self.profiles, "library": list(self.data["profile_library"]),
                "phase": self.phase.phase if self.link == "connected" else "off",
                "labels": {"state": STATES.get(L.get("state"), "Unknown") if L.get("state") is not None else None,
                           "chamber": CHAMBERS.get(L.get("chamber")) if L.get("chamber") is not None else None,
                           "charge": CHARGE.get(L.get("charge")) if L.get("charge") is not None else None,
                           "source": CHARGE_SRC.get(L.get("charge_src")) if L.get("charge_src") is not None else None},
                "charging": ch, "avg_full": avg, "avg_full_n": n,
                "heating_for": now - self.sessions.cur["start"] if self.sessions.cur else None,
                "today": self.sessions.today(),
                "avg_session": sum(s["duration"] for s in recent) / len(recent) if recent else None,
                "bpd": self.sessions.avg_battery_per_dab(),
                "health": dict(self.health), "per_day": per_day,
                "charges": list(reversed(self.data["charges"][-100:])),
                "sessions": list(reversed(sess[-200:])),
                "ov": dict(self.ov), "cfg": dict(self.cfg), "ov_custom": self.ov_custom,
                "editions": list_editions(), "accent": self.edition_accent(),
                "url": self.server.url, "preview": self.preview,
                "log": list(self.log)[-400:], "bleak": self.worker is not None,
                "folder": str(HERE), "version": VERSION, "frozen": FROZEN,
                "window": bool(self.ui and self.ui.window),
            }

    # -- actions from the app window / tray ------------------------------------
    def do(self, a, args=None):
        args = args or {}
        with self.lock:
            w = self.worker
            if a == "scan" and w and self.link == "off":
                w.submit(w.scan())
            elif a == "connect" and w:
                i = int(args.get("index", 0))
                if 0 <= i < len(self.devices):
                    name, addr = self.devices[i]
                    self.data["device"] = {"name": name, "address": addr}
                    save_data(self.data)
                    w.submit(w.start_link(addr))
                else:
                    self.set_status("Pick a device first (hit Scan).")
            elif a == "disconnect" and w:
                w.submit(w.stop_link())
            elif a == "toggle_connect" and w:
                if self.link != "off":
                    w.submit(w.stop_link())
                elif self.devices:
                    self.do("connect", {"index": 0})
                else:
                    w.submit(w.scan())
            elif a == "preview":
                self.preview = args.get("key") or None
            elif a == "puff":
                self.preview_hit_id += 1
                self.preview_hit_until = time.time() + 15.5
            elif a == "probe" and w:
                w.submit(w.probe())
            elif a == "read" and w and args.get("path"):
                w.submit(w.read_path(str(args["path"]).strip()))
            elif a == "save_profile":
                return self.save_profile(args.get("slot"), args.get("profile") or {})
            elif a == "refresh_profiles" and w and self.link == "connected":
                w.submit(w.read_profiles())
            elif a == "lib_save":
                p = clean_profile(args.get("profile") or {})
                if isinstance(p, str):
                    return {"ok": False, "error": p}
                lib, i = self.data["profile_library"], args.get("index")
                if isinstance(i, int) and 0 <= i < len(lib):
                    lib[i] = p
                else:
                    lib.append(p)
                save_data(self.data)
            elif a == "lib_remove":
                lib, i = self.data["profile_library"], args.get("index")
                if isinstance(i, int) and 0 <= i < len(lib):
                    lib.pop(i)
                    save_data(self.data)
            elif a == "clear_log":
                self.log.clear()
            elif a == "clear_charges":
                self.data["charges"] = []
                save_data(self.data)
                self.update_health()
            elif a == "clear_sessions":
                self.data["sessions"] = []
                save_data(self.data)
            elif a == "reset_overlay":
                ed = self.ov.get("edition")
                self.ov.clear()
                self.ov.update(OVERLAY_DEFAULTS, edition=ed)
                self.save_settings()
            elif a == "reset_keys":
                for k in args.get("keys", []):
                    if k in OVERLAY_DEFAULTS and k != "edition":
                        self.ov[k] = OVERLAY_DEFAULTS[k]
                    elif k in APP_DEFAULTS:
                        self.cfg[k] = APP_DEFAULTS[k]
                self.apply_cfg(None)
                self.save_settings()
            elif a == "open_folder":
                open_path(HERE)
            elif a == "open_overlay":
                webbrowser.open(self.server.url + "?demo=1")
            elif a in ("show", "hide", "quit") and self.ui:
                getattr(self.ui, a)()
        return {"ok": True}

    def save_profile(self, slot, p):
        if not (isinstance(slot, int) and 0 <= slot < PROFILE_SLOTS):
            return {"ok": False, "error": "Pick a slot from 1 to 4."}
        if not self.worker or self.link != "connected":
            return {"ok": False, "error": "Connect to your rig first."}
        if self.last.get("state") in HEATING:
            return {"ok": False, "error": "Wait until the rig finishes heating, then save."}
        p = clean_profile(p)
        if isinstance(p, str):
            return {"ok": False, "error": p}
        have = next((x for x in self.profiles if x["slot"] == slot), {})
        for key in ("vapor", "boost_temp", "boost_time"):   # only write what this firmware has
            if key not in have:
                p.pop(key, None)
        self.worker.submit(self.worker.write_profile(slot, p))
        return {"ok": True}

    def set_ov(self, changes):
        with self.lock:
            for k, v in changes.items():
                if k in OVERLAY_DEFAULTS:
                    self.ov[k] = v
                elif k == "ov_custom":
                    self.ov_custom = str(v)
            self.save_soon()

    def set_cfg(self, changes):
        with self.lock:
            for k, v in changes.items():
                if k in APP_DEFAULTS:
                    self.cfg[k] = v
                    self.apply_cfg(k)
            self.save_soon()

    def apply_cfg(self, key):
        if key in (None, "auto_reconnect") and self.worker:
            self.worker.auto = self.cfg["auto_reconnect"]
        if key in (None, "startup", "start_min"):
            self.update_startup()
        if key in (None, "on_top") and self.ui:
            self.ui.on_top(bool(self.cfg["on_top"]))

    def add_edition(self, name, image_b64):
        name = "".join(ch for ch in str(name) if ch not in '\\/:*?"<>|').strip() or "My rig"
        try:
            raw = base64.b64decode(image_b64.split(",", 1)[-1])
        except Exception:
            return {"ok": False, "error": "That file couldn't be read."}
        RIGS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = RIGS_DIR / f"_upload_{int(time.time())}.img"
        tmp.write_bytes(raw)
        self.set_status(f"Processing “{name}”…")

        def work():
            try:
                import add_rig
                add_rig.RIGS = RIGS_DIR   # next to the exe, not the temp unpack folder
                _, view = add_rig.process(str(tmp), name)
                self.q.put(("edition_added", (name, view, None)))
            except ImportError:
                self.q.put(("edition_added", (name, None, "adding editions needs:  pip install pillow numpy")))
            except Exception as e:
                self.q.put(("edition_added", (name, None, str(e))))
            finally:
                try:
                    tmp.unlink()
                except OSError:
                    pass
        threading.Thread(target=work, daemon=True).start()
        return {"ok": True}

    # -- saving, alerts, startup ------------------------------------------------
    def save_settings(self):
        with self.lock:
            self._save_timer = None
            self.data["settings"] = dict(self.cfg, overlay=dict(self.ov), overlay_custom=self.ov_custom)
            save_data(self.data)

    def save_soon(self):
        if self._save_timer:
            self._save_timer.cancel()
        self._save_timer = threading.Timer(0.5, self.save_settings)
        self._save_timer.daemon = True
        self._save_timer.start()

    def alert(self, title, body, kind):
        if self.cfg["notify"] and self.cfg.get(f"notify_{kind}", True):
            notify(title, body)

    def update_startup(self):
        if not sys.platform.startswith("win"):
            return
        f = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Puffco Monitor.vbs"
        try:
            if self.cfg["startup"]:
                if FROZEN:
                    cmd = f'"""{sys.executable}"" --startup"'
                else:
                    exe = Path(sys.executable)
                    pyw = exe.with_name("pythonw.exe")
                    pyw = pyw if pyw.exists() else exe
                    cmd = f'"""{pyw}"" ""{Path(__file__).resolve()}"" --startup"'
                f.write_text('Set s = CreateObject("WScript.Shell")\r\n'
                             f's.CurrentDirectory = "{HERE}"\r\n'
                             f's.Run {cmd}, 0, False\r\n')
            elif f.exists():
                f.unlink()
        except OSError as e:
            self.set_status(f"Couldn't change the startup setting: {e}")

    def shutdown(self):
        self.quitting = True
        if self._save_timer:
            self._save_timer.cancel()
        self.save_settings()
        if self.worker and self.link != "off":
            self.worker.submit(self.worker.stop_link(quiet=True))
            time.sleep(0.4)
        self.server.stop()


# ------------------------------------------------------------- window + tray ---
def open_path(p):
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(p))
        else:
            webbrowser.open(Path(p).resolve().as_uri())
    except Exception:
        log.exception("open_path")


def copy_text(s):
    try:
        if sys.platform.startswith("win"):
            subprocess.run(["clip"], input=s.encode("utf-16-le"), creationflags=0x08000000, check=False)
    except Exception:
        log.exception("copy_text")


class Host:
    """The native window (Edge WebView2 via pywebview) plus the tray icon. Closing the window
    just hides it - the rig stays connected and the OBS overlay keeps running."""

    def __init__(self, core, hidden):
        self.core, self.hidden = core, hidden
        core.ui = self
        self.url = f"http://127.0.0.1:{core.server.port}/app"
        self.window = self.icon = None
        self.done = threading.Event()

    def run(self):
        self.start_tray()
        try:
            import webview
            self.window = webview.create_window(
                "Puffco Monitor", self.url, width=1240, height=830, min_size=(980, 660),
                background_color="#0b0b0f", hidden=self.hidden, on_top=bool(self.core.cfg["on_top"]))
            self.window.events.closing += self._closing
            self.window.events.shown += self._dark_titlebar
            webview.start(gui="edgechromium" if sys.platform.startswith("win") else None, icon=str(RES / "icon.ico"))
        except Exception:
            log.exception("native window unavailable - using a browser window instead")
            self.window = None
            if not self.hidden:
                self.open_browser()
            if self.icon is None:
                self.done.wait()
            else:
                while not self.done.wait(0.5):
                    pass
        finally:
            if not self.core.quitting:
                self.core.shutdown()
            if self.icon:
                try:
                    self.icon.stop()
                except Exception:
                    pass

    def _closing(self):
        if self.core.quitting or not self.core.cfg.get("close_to_tray", True) or self.icon is None:
            self.core.quitting = True
            self.done.set()
            return True
        self.window.hide()
        return False

    def _dark_titlebar(self):
        """Dark Windows title bar to match the app (Windows 10 2004+ / 11)."""
        if not sys.platform.startswith("win"):
            return
        try:
            import ctypes
            hwnd = ctypes.windll.user32.FindWindowW(None, "Puffco Monitor")
            if hwnd:
                on = ctypes.c_int(1)
                for attr in (20, 19):
                    if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(on), 4) == 0:
                        break
                cap = ctypes.c_int(0x000f0b0b)        # caption colour 0x00BBGGRR (Windows 11)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(cap), 4)
        except Exception:
            pass

    def open_browser(self):
        """No WebView2: open the app in an Edge/Chrome app window (no tabs or address bar)."""
        if sys.platform.startswith("win"):
            for exe in (r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
                        r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
                        r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
                        r"%LocalAppData%\Google\Chrome\Application\chrome.exe"):
                p = os.path.expandvars(exe)
                if os.path.isfile(p):
                    subprocess.Popen([p, f"--app={self.url}", "--window-size=1240,830"], creationflags=0x08000000)
                    return
        webbrowser.open(self.url)

    def show(self):
        if self.window is not None:
            self.window.show()
            try:
                self.window.restore()
            except Exception:
                pass
        else:
            self.open_browser()

    def hide(self):
        if self.window is not None:
            self.window.hide()

    def on_top(self, on):
        if self.window is not None:
            try:
                self.window.on_top = on
            except Exception:
                pass

    def quit(self):
        self.core.quitting = True
        self.core.shutdown()
        self.done.set()
        if self.icon:
            try:
                self.icon.stop()
            except Exception:
                pass
        if self.window is not None:
            try:
                self.window.destroy()
            except Exception:
                os._exit(0)

    # -- tray --------------------------------------------------------------------
    def tray_status(self):
        c = self.core
        if c.link == "connected":
            bat = c.last.get("battery")
            return f"Connected{'  ·  ' + format(bat, '.0f') + '%' if bat is not None else ''}"
        return {"connecting": "Connecting…", "waiting": "Waiting for the rig…"}.get(c.link, "Not connected")

    def start_tray(self):
        try:
            import pystray
            from PIL import Image
        except ImportError:
            log.info("pystray/pillow missing - no tray icon")
            return
        c = self.core
        menu = pystray.Menu(
            pystray.MenuItem("Open Puffco Monitor", lambda: self.show(), default=True),
            pystray.MenuItem(lambda item: self.tray_status(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(lambda item: "Disconnect" if c.link != "off" else "Connect",
                             lambda: c.do("toggle_connect")),
            pystray.MenuItem("Copy overlay URL", lambda: copy_text(c.server.url)),
            pystray.MenuItem("Start with Windows", lambda: c.set_cfg({"startup": not c.cfg["startup"]}),
                             checked=lambda item: bool(c.cfg["startup"])),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit Puffco Monitor", lambda: self.quit()),
        )
        try:
            self.icon = pystray.Icon("PuffcoMonitor", Image.open(RES / "icon.png"), "Puffco Monitor", menu)
            threading.Thread(target=self.icon.run, daemon=True).start()
            threading.Thread(target=self._tray_loop, daemon=True).start()
        except Exception:
            log.exception("tray icon failed")
            self.icon = None

    def _tray_loop(self):
        last = None
        while not self.done.wait(2):
            s = self.tray_status()
            if s != last and self.icon:
                last = s
                try:
                    self.icon.title = f"Puffco Monitor - {s}"
                    self.icon.update_menu()
                except Exception:
                    pass


def find_running():
    """If Puffco Monitor is already open, return its port."""
    import urllib.request
    for p in range(8765, 8771):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{p}/api/ping", timeout=0.4) as r:
                if json.loads(r.read()).get("app") == "puffco-monitor":
                    return p
        except Exception:
            continue
    return None


def poke_running(port):
    import urllib.request
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/do", data=json.dumps({"a": "show"}).encode(),
                                 headers={"Content-Type": "application/json", "X-Puffco": "1"})
    try:
        urllib.request.urlopen(req, timeout=2).read()
    except Exception:
        pass


def selftest():
    """Used when building: checks every bundled piece imports, then exits."""
    out = []
    for mod in ("bleak", "bleak.backends.winrt.client", "bleak.backends.winrt.scanner", "winrt.windows.devices.bluetooth",
                "webview", "clr_loader", "pystray", "PIL.Image", "numpy", "add_rig"):
        try:
            __import__(mod)
            out.append(f"ok   {mod}")
        except Exception as e:
            out.append(f"FAIL {mod}: {e!r}")
    for f in ("app.html", "overlay.html", "icon.png", "icon.ico"):
        out.append(f"{'ok  ' if (RES / f).is_file() else 'MISS'} {f}")
    (HERE / "selftest.txt").write_text("\n".join(out))


def main():
    args = sys.argv[1:]
    if "--selftest" in args:
        return selftest()
    port = find_running()
    if port:
        poke_running(port)
        return
    core = Core()
    hidden = "--minimized" in args or ("--startup" in args and core.cfg.get("start_min"))
    Host(core, hidden).run()


if __name__ == "__main__":
    main()
