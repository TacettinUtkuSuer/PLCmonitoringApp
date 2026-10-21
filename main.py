# -*- coding: utf-8 -*-
"""
==========================================================================================
 INDUSTRIAL TEST BENCH MONITOR  --  Single File Service (FastAPI + InfluxQL + uPlot)
==========================================================================================
 * InfluxDB v1 /query REST endpoint (InfluxQL, SQL-Style). NO Flux.
 * Dynamic downsampling: GROUP BY time(<interval>) auto-selected by time range.
 * ISO 8601 -> float Unix epoch seconds conversion done in Python.
 * Stateless backend: no time-series data is ever kept in server RAM.
 * Reactive sensor pool with WebSocket broadcast (zero F5 refresh required).
 * Port: 8989 (BENCHMONITOR_PORT ortam degiskeniyle degistirilebilir)
==========================================================================================
"""

import os
import re
import json
import math
import time
import random
import asyncio
import threading
import socket
import sys
import logging
import logging.handlers
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ==========================================================================================
# 1) CONFIGURATION (persisted to config.json -> survives container restart via volume/rebuild)
# ==========================================================================================

# PyInstaller ile paketlenmis (sys.frozen) calisirken __file__ gecici _MEIPASS klasorunu
# gosterir; config/preset orada kaybolur.  Bu yuzden veri klasoru ProgramData'dir.
FROZEN = bool(getattr(sys, "frozen", False))

if FROZEN:
    BASE_DIR = os.environ.get("BENCHMONITOR_DATA") or os.path.join(
        os.environ.get("PROGRAMDATA") or r"C:\ProgramData", "BenchMonitor")
    os.makedirs(BASE_DIR, exist_ok=True)
    # vendor/ (uPlot) icin ilk bulunan: veri klasoru > exe yani > paket ici (_MEIPASS)
    _VENDOR_CANDIDATES = [
        os.path.join(BASE_DIR, "vendor"),
        os.path.join(os.path.dirname(sys.executable), "vendor"),
        os.path.join(getattr(sys, "_MEIPASS", ""), "vendor"),
    ]
    VENDOR_DIR = next((d for d in _VENDOR_CANDIDATES if os.path.isdir(d)), _VENDOR_CANDIDATES[0])
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    VENDOR_DIR = os.path.join(BASE_DIR, "vendor")

CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DEFAULT_PORT = 8989


def get_port() -> int:
    """Dinlenecek port: BENCHMONITOR_PORT ortam degiskeni (kurulumda sorulur), yoksa 8989."""
    try:
        port = int(os.environ.get("BENCHMONITOR_PORT") or DEFAULT_PORT)
        return port if 1 <= port <= 65535 else DEFAULT_PORT
    except ValueError:
        return DEFAULT_PORT


LOG_DIR = os.path.join(BASE_DIR, "logs")

DEFAULT_CONFIG: Dict[str, Any] = {
    "influx_url": "http://10.10.2.6:8086",
    "db": "lc_100",
    "username": "",
    "password": "",
    "measurement": "telemetry",
    "refresh_ms": 1000,
}

_CFG_LOCK = threading.Lock()


def load_config() -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                disk = json.load(fh)
            if isinstance(disk, dict):
                cfg.update({k: v for k, v in disk.items() if k in DEFAULT_CONFIG})
    except Exception as exc:  # corrupted config must never kill the service
        print("[CONFIG] read error, using defaults:", exc)
    return cfg


def save_config(new_cfg: Dict[str, Any]) -> Dict[str, Any]:
    with _CFG_LOCK:
        cfg = load_config()
        for key in DEFAULT_CONFIG:
            if key in new_cfg and new_cfg[key] is not None:
                cfg[key] = new_cfg[key]
        cfg["influx_url"] = str(cfg["influx_url"]).rstrip("/")
        try:
            cfg["refresh_ms"] = max(250, int(cfg.get("refresh_ms") or 1000))
        except Exception:
            cfg["refresh_ms"] = 1000
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2, ensure_ascii=False)
        return cfg


CONFIG: Dict[str, Any] = load_config()

# ==========================================================================================
# 2) IN-MEMORY REACTIVE STATE  (sensor pool ONLY -- never time series data)
# ==========================================================================================

state: Dict[str, Any] = {
    # [{"sensor_id": "<influxdb adi>", "name": "<arayuz adi>", "unit": "...", "ts": 1699999999.0}]
    "pool_sensors": []
}

SENSOR_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")


def valid_sensor(sensor_id: str) -> bool:
    return bool(sensor_id) and bool(SENSOR_RE.match(sensor_id))


class _Keep(object):
    """'Bu alana dokunma' anlamina gelen tekil isaretci (None = 'temizle' demektir)."""
    __slots__ = ()

    def __repr__(self) -> str:
        return "KEEP"


KEEP = _Keep()


def parse_limit(raw: Any) -> Any:
    """Esik degeri ayristirici.
       verilmemis            -> KEEP  (mevcut deger korunur)
       "" / "null" / "none"  -> None  (esik kaldirilir)
       sayi                  -> float"""
    if raw is KEEP or raw is None:
        return KEEP
    text = str(raw).strip()
    if text == "" or text.lower() in ("null", "none", "-"):
        return None
    try:
        val = float(text.replace(",", "."))
    except (TypeError, ValueError):
        return KEEP
    return val if math.isfinite(val) else None


EXPR_MAX = 512


def parse_expr(raw: Any) -> Any:
    """HESAP sensoru formulu ayristirici (formul METIN olarak saklanir; sunucu
       onu YORUMLAMAZ -- degerlendirme tarayicida yapilir, boylece preset
       havuzundaki gecici sensorler de ayni motoru kullanir).
       verilmemis  -> KEEP  (mevcut formul korunur)
       ""          -> None  (formul silinir; kart duz sensore doner)
       "=a*2"/"a*2"-> "a*2" (bastaki '=' arayuz sozdizimidir, saklanmaz)"""
    if raw is KEEP or raw is None:
        return KEEP
    text = str(raw).strip()
    if text == "":
        return None
    if text[0] == "=":
        text = text[1:].strip()
    if text == "":
        return None
    return text[:EXPR_MAX]


SOURCE_MAX = 256


def parse_source(raw: Any, is_url: bool = False) -> Any:
    """SENSOR BAZLI KAYNAK alani ayristirici (db / measurement / influx_url).

       Bu uc alanin her biri BIRBIRINDEN BAGIMSIZDIR: webhook'ta hangisi
       gonderildiyse yalnizca o, o sensor icin genel ayarin YERINE gecer.
       Gonderilmeyen alan genel ayardan (config.json) okunmaya devam eder.

       verilmemis -> KEEP  (mevcut deger korunur)
       ""         -> None  (gecersiz kilma KALKAR, genel ayara donulur)
       metin      -> metin (o sensor icin gecersiz kilma)"""
    if raw is KEEP or raw is None:
        return KEEP
    text = str(raw).strip()
    if text == "" or text.lower() in ("null", "none", "-"):
        return None
    text = text[:SOURCE_MAX]
    if is_url:
        text = text.rstrip("/")
    return text or None


# ==========================================================================================
# 3) DYNAMIC DOWNSAMPLING TABLE  (target: 1000-2000 points per query -> 60 FPS)
# ==========================================================================================

RANGE_TABLE: List[Dict[str, Any]] = [
    {"key": "5m",  "label": "5 Dk",   "seconds": 300},
    {"key": "10m", "label": "10 Dk",  "seconds": 600},
    {"key": "15m", "label": "15 Dk",  "seconds": 900},
    {"key": "30m", "label": "30 Dk",  "seconds": 1800},
    {"key": "1h",  "label": "1 Saat", "seconds": 3600},
    {"key": "2h",  "label": "2 Saat", "seconds": 7200},
    {"key": "3h",  "label": "3 Saat", "seconds": 10800},
    {"key": "4h",  "label": "4 Saat", "seconds": 14400},
    {"key": "5h",  "label": "5 Saat", "seconds": 18000},
    {"key": "10h", "label": "10 Saat", "seconds": 36000},
]
RANGE_MAP = {r["key"]: r for r in RANGE_TABLE}


def auto_interval_ms(seconds: int) -> int:
    """VARSAYILAN cozunurluk (milisaniye):
       5-10 Dk -> 1s | 15-30 Dk -> 2s | 1-2 Saat -> 5s | 3-5 Saat -> 15s | 10 Saat -> 30s"""
    if seconds <= 600:
        return 1000
    if seconds <= 1800:
        return 2000
    if seconds <= 7200:
        return 5000
    if seconds <= 18000:
        return 15000
    return 30000


# Varsayilan degerin UZERINE binen kademeli cozunurluk artisi.
#   fine=0 -> varsayilan      (10 Saat icin 30s)
#   fine=1 -> M  (yarisi)     (10 Saat icin 15s)
#   fine=2 -> F  (ceyregi)    (10 Saat icin 7500ms)
#   fine=3 -> tam ekran, M/F uzerine 1 kademe daha
FINE_MAX = 3
MIN_INTERVAL_MS = 100


def fine_interval_ms(base_ms: int, fine: Any) -> int:
    try:
        f = int(fine)
    except (TypeError, ValueError):
        f = 0
    f = max(0, min(FINE_MAX, f))
    return max(MIN_INTERVAL_MS, int(round(base_ms / float(2 ** f))))


def fmt_interval(ms: int) -> str:
    """InfluxQL GROUP BY time() birimi: tam saniyeyse '15s', degilse '7500ms'."""
    return "{0}s".format(ms // 1000) if ms % 1000 == 0 else "{0}ms".format(ms)


def auto_interval(seconds: int) -> str:
    return fmt_interval(auto_interval_ms(seconds))


# ==========================================================================================
# 4) ISO 8601  ->  FLOAT UNIX EPOCH SECONDS
# ==========================================================================================

def iso_to_epoch(value: Any) -> Optional[float]:
    """'2026-09-14T15:12:00Z' / '...T15:12:00.123456789Z' -> 1789...0.0 (float seconds)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e17:      # nanoseconds
            return v / 1e9
        if v > 1e14:      # microseconds
            return v / 1e6
        if v > 1e11:      # milliseconds
            return v / 1e3
        return v
    txt = str(value).strip()
    if not txt:
        return None
    if txt.endswith("Z"):
        txt = txt[:-1] + "+00:00"
    # Python's fromisoformat accepts at most 6 fractional digits -> clamp nanoseconds
    if "." in txt:
        head, rest = txt.split(".", 1)
        frac, tail = "", ""
        for i, ch in enumerate(rest):
            if ch.isdigit():
                frac += ch
            else:
                tail = rest[i:]
                break
        frac = (frac + "000000")[:6]
        txt = head + "." + frac + tail
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


# ==========================================================================================
# 5) INFLUXQL CLIENT  (v1 /query REST endpoint)
# ==========================================================================================

def build_influxql(sensors: List[str], range_key: str, interval: str, measurement: str) -> str:
    selects = ", ".join('last("{0}") AS "{0}"'.format(s) for s in sensors)
    return (
        'SELECT {sel} FROM "{meas}" '
        'WHERE time > now() - {rng} '
        'GROUP BY time({ivl}) fill(none)'
    ).format(sel=selects, meas=measurement, rng=range_key, ivl=interval)


_HTTP: Dict[str, Any] = {"client": None, "loop": None}


def http_client() -> Any:
    """TEK, paylasilan HTTP istemcisi (keep-alive). Her sorguda yeni istemci acmak
       ~50 ms surer (sertifika deposu yuklenir; Windows'ta daha da uzun) ve bu sure
       boyunca sunucu baska hicbir istege bakamaz; ustelik her sorgu InfluxDB'ye
       yeni bir TCP baglantisi kurar. Grafik + alarm yoklamasi saniyede birkac
       sorgu yaptigi icin bu, arayuzun belirgin sekilde yavaslamasina yol aciyordu."""
    loop = asyncio.get_running_loop()
    client = _HTTP["client"]
    if client is None or _HTTP["loop"] is not loop or getattr(client, "is_closed", False):
        client = httpx.AsyncClient(timeout=20.0)
        _HTTP.update(client=client, loop=loop)
    return client


async def influx_query(query: str, base_url: Optional[str] = None,
                       db: Optional[str] = None) -> Dict[str, Any]:
    """base_url / db verilmezse genel ayar kullanilir (sensor bazli gecersiz
       kilma bu iki parametre ile gelir)."""
    cfg = CONFIG
    root = str(base_url if base_url else cfg["influx_url"]).rstrip("/")
    url = root + "/query"
    params = {"db": str(db if db else cfg["db"]), "q": query}
    auth = None
    if cfg.get("username"):
        auth = (cfg.get("username") or "", cfg.get("password") or "")
    resp = await http_client().get(url, params=params, auth=auth)
    resp.raise_for_status()
    return resp.json()


def sensor_source(sid: str) -> Tuple[str, str, str]:
    """Bir sensorun (influx_url, db, measurement) kaynagi.

       Karttaki deger doluysa ONU, bos ise genel ayari kullanir. Uc alan
       BIRBIRINDEN BAGIMSIZ cozulur -- yalnizca 'db' gonderilmis bir sensor,
       url ve measurement'i genel ayardan almaya devam eder."""
    rec = next((s for s in state["pool_sensors"] if s["sensor_id"] == sid), None) or {}
    url = rec.get("url") or CONFIG.get("influx_url") or ""
    db = rec.get("db") or CONFIG.get("db") or ""
    meas = rec.get("meas") or CONFIG.get("measurement") or "telemetry"
    return (str(url).rstrip("/"), str(db), str(meas))


def group_by_source(sids: List[str]) -> "OrderedDict[Tuple[str, str, str], List[str]]":
    """Ayni (url, db, measurement) ucluIsunu paylasan sensorler TEK sorguda
       toplanir; farkli kaynaklar ayri sorgulara boluner."""
    groups: "OrderedDict[Tuple[str, str, str], List[str]]" = OrderedDict()
    for sid in sids:
        groups.setdefault(sensor_source(sid), []).append(sid)
    return groups


def mock_series(sensor_id: str, seconds: int, interval_sec: float) -> Dict[str, List[Any]]:
    """Synthetic waveform for MOCK_* sensors so the UI is testable without a live DB.
    Computed on the fly, never stored."""
    now = time.time()
    start = now - seconds
    seed = sum(ord(c) for c in sensor_id)
    rnd = random.Random(seed)
    amp = 10 + (seed % 90)
    base = (seed % 40)
    freq = 1.0 / (60.0 + (seed % 240))
    ts: List[float] = []
    vals: List[Optional[float]] = []
    t = start
    while t <= now:
        v = base + amp * math.sin(2 * math.pi * freq * t) + rnd.uniform(-amp * 0.03, amp * 0.03)
        ts.append(round(t, 3))
        vals.append(round(v, 4))
        t += interval_sec
    return {"t": ts, "v": vals}


# ==========================================================================================
# 5b) GLOBAL PRESET DEPOSU  (presets.json -> TUM PC / TUM TARAYICI ayni listeyi gorur)
# ==========================================================================================

PRESET_PATH = os.path.join(BASE_DIR, "presets.json")
_PRESET_LOCK = threading.Lock()
PRESET_NAME_RE = re.compile(r"^[^\x00-\x1f/\\]{1,64}$")


def valid_preset_name(name: str) -> bool:
    return bool(name) and bool(PRESET_NAME_RE.match(name))


def load_presets() -> Dict[str, Any]:
    try:
        if os.path.exists(PRESET_PATH):
            with open(PRESET_PATH, "r", encoding="utf-8") as fh:
                disk = json.load(fh)
            if isinstance(disk, dict):
                return disk
    except Exception as exc:  # bozuk dosya servisi asla oldurmesin
        print("[PRESET] read error:", exc)
    return {}


def write_presets(data: Dict[str, Any]) -> None:
    tmp = PRESET_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, PRESET_PATH)


def preset_names() -> List[str]:
    return sorted(load_presets().keys(), key=lambda s: s.lower())


# ==========================================================================================
# 5c) MALZEME TABLOLARI  (materials.json -> TUM PC'ler ayni listeyi gorur)
#     CSV: temp [degC], dens [kg/m3], cp [J/kg·K], dvis [Pa·s], cond [W/m·K]
#     Formulde:  SU_dens(T1)  SU_cp(25)  SU_dvis(x)  SU_cond(x)  (tarayicida ara degerlenir)
# ==========================================================================================

MATERIAL_PATH = os.path.join(BASE_DIR, "materials.json")
_MAT_LOCK = threading.Lock()
MAT_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
# (sutun, ZORUNLU birim) -- birimler sabittir, donusum yapilmaz
MAT_COLS: List[Tuple[str, str]] = [
    ("temp", "degC"), ("dens", "kg/m3"), ("cp", "J/kg·K"), ("dvis", "Pa·s"), ("cond", "W/m·K"),
]
MAT_MAX_ROWS = 5000


def _unit_key(unit: str) -> str:
    """Birim karsilastirmasi: buyuk/kucuk harf, bosluk ve carpim isareti (· . * ya da
       Excel'in bozdugu 'Â·') onemsiz. 'J/kg·K' -> 'j/kgk', 'Pa·s' -> 'pas'."""
    return re.sub(r"[^a-z0-9/]", "", (unit or "").lower())


def parse_material_csv(text: str) -> List[List[float]]:
    """Malzeme CSV'sini dogrular, [[temp, dens, cp, dvis, cond], ...] (temp'e gore sirali) doner.
       Hata durumunda ValueError (mesaj kullaniciya gosterilir)."""
    lines = [ln.strip() for ln in (text or "").lstrip("\ufeff").splitlines() if ln.strip()]
    if len(lines) < 3:
        raise ValueError("En az bir baslik satiri ve iki veri satiri gerekir.")
    head = lines[0]
    delim = max([";", "\t", ","], key=head.count)
    cells = [c.strip().strip('"') for c in head.split(delim)]
    pos: Dict[str, int] = {}
    for idx, cell in enumerate(cells):
        m = re.match(r"^([A-Za-z]+)\s*\[(.*)\]$", cell)
        if not m:
            raise ValueError("Baslik hatali: '{0}' (bicim: ad [birim])".format(cell))
        col, unit = m.group(1).lower(), m.group(2).strip()
        want = dict(MAT_COLS).get(col)
        if want is None:
            raise ValueError("Bilinmeyen sutun: '{0}'".format(cell))
        if _unit_key(unit) != _unit_key(want):
            raise ValueError("'{0}' sutununun birimi [{1}] olmali, CSV'de [{2}]".format(col, want, unit))
        if col in pos:
            raise ValueError("Sutun iki kez var: '{0}'".format(col))
        pos[col] = idx
    missing = [c for c, _ in MAT_COLS if c not in pos]
    if missing:
        raise ValueError("Eksik sutun: " + ", ".join(missing))
    rows: List[List[float]] = []
    for no, line in enumerate(lines[1:], start=2):
        parts = [c.strip().strip('"') for c in line.split(delim)]
        if len(parts) != len(cells):
            raise ValueError("{0}. satirda {1} deger var, {2} olmali".format(no, len(parts), len(cells)))
        row = []
        for col, _ in MAT_COLS:
            raw = parts[pos[col]]
            if delim != ",":
                raw = raw.replace(",", ".")          # 998,73 (Turkce Excel)
            try:
                val = float(raw)
            except ValueError:
                raise ValueError("{0}. satir, {1}: sayi degil ('{2}')".format(no, col, parts[pos[col]]))
            if not math.isfinite(val):
                raise ValueError("{0}. satir, {1}: gecersiz sayi".format(no, col))
            row.append(val)
        rows.append(row)
    if len(rows) > MAT_MAX_ROWS:
        raise ValueError("En fazla {0} satir.".format(MAT_MAX_ROWS))
    rows.sort(key=lambda r: r[0])
    for a, b in zip(rows, rows[1:]):
        if a[0] == b[0]:
            raise ValueError("Ayni sicaklik iki kez var: {0} degC".format(a[0]))
    return rows


def load_materials() -> Dict[str, Any]:
    try:
        if os.path.exists(MATERIAL_PATH):
            with open(MATERIAL_PATH, "r", encoding="utf-8") as fh:
                disk = json.load(fh)
            if isinstance(disk, dict):
                return disk
    except Exception as exc:  # bozuk dosya servisi asla oldurmesin
        print("[MATERIAL] read error:", exc)
    return {}


def write_materials(data: Dict[str, Any]) -> None:
    tmp = MATERIAL_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False)
    os.replace(tmp, MATERIAL_PATH)


# ==========================================================================================
# 6) WEBSOCKET HUB  (reactive pool -> all connected screens, no F5)
# ==========================================================================================

class Hub:
    def __init__(self) -> None:
        self.clients: List[WebSocket] = []
        self.lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self.lock:
            self.clients.append(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self.lock:
            if ws in self.clients:
                self.clients.remove(ws)

    async def broadcast(self, payload: Dict[str, Any]) -> None:
        dead: List[WebSocket] = []
        async with self.lock:
            targets = list(self.clients)
        for ws in targets:
            try:
                await ws.send_text(json.dumps(payload))
            except Exception:
                dead.append(ws)
        if dead:
            async with self.lock:
                for ws in dead:
                    if ws in self.clients:
                        self.clients.remove(ws)


hub = Hub()


def pool_payload() -> Dict[str, Any]:
    return {"type": "pool", "sensors": state["pool_sensors"]}


def preset_payload() -> Dict[str, Any]:
    return {"type": "presets", "names": preset_names()}


def material_payload() -> Dict[str, Any]:
    return {"type": "materials", "materials": load_materials()}


# ==========================================================================================
# 7) EMBEDDED FRONTEND (HTML + CSS + JS)
# ==========================================================================================

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Test Bench Monitor</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'><rect width='16' height='16' rx='3' fill='%230d1017'/><path d='M2 11l3-4 3 3 3-6 3 5' stroke='%234ea1ff' fill='none' stroke-width='1.7'/></svg>">

<!--VENDOR-->

<style>
:root{
  --bg:#0d1017; --panel:#141924; --panel2:#1a2030; --line:#252c3d;
  --txt:#dfe4ee; --dim:#8a93a6; --accent:#4ea1ff; --ok:#3ddc84; --warn:#ffb454; --bad:#ff5c6c;
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--txt);font:13px/1.35 "Segoe UI",Roboto,Arial,sans-serif;overflow:hidden}
button,select,input{font-family:inherit}

/* ---------- ERROR BANNER ---------- */
#errbar{display:none;position:fixed;top:0;left:0;right:0;z-index:9999;background:#7f1420;color:#fff;
  padding:7px 40px 7px 12px;font:12px/1.4 Consolas,monospace;border-bottom:1px solid #ff5c6c;white-space:pre-wrap;max-height:35vh;overflow:auto}
#errbar b{color:#ffd0d5}
#errclose{position:absolute;right:8px;top:5px;background:transparent;border:1px solid #ff8f99;color:#fff;cursor:pointer;border-radius:3px;padding:1px 7px}

/* ---------- TOP BAR ---------- */
#top{height:46px;display:flex;align-items:center;gap:10px;padding:0 12px;background:var(--panel);border-bottom:1px solid var(--line)}
#brand{font-weight:700;letter-spacing:.6px;font-size:14px;color:#fff;white-space:nowrap}
#brand span{color:var(--accent)}
.sep{width:1px;height:24px;background:var(--line)}
.btn{background:var(--panel2);border:1px solid var(--line);color:var(--txt);padding:5px 10px;border-radius:4px;cursor:pointer;font-size:12px}
.btn:hover{border-color:var(--accent);color:#fff}
.btn.sm{padding:3px 7px;font-size:11px}
.btn.live{min-width:62px;font-weight:700;letter-spacing:.3px}
.btn.live.on{background:#10351f;border-color:var(--ok);color:var(--ok)}
.btn.live.off{background:#3a2b12;border-color:var(--warn);color:var(--warn)}
.btn.tg.on{background:#10351f;border-color:var(--ok);color:var(--ok)}
select,input[type=text],input[type=password],input[type=number]{background:#0f131c;border:1px solid var(--line);color:var(--txt);padding:4px 6px;border-radius:4px;font-size:12px}
select:focus,input:focus{outline:none;border-color:var(--accent)}
.grow{flex:1}
#dbdot{width:9px;height:9px;border-radius:50%;background:#555;display:inline-block;margin-right:5px}
#dbstat{font-size:11px;color:var(--dim);white-space:nowrap}

/* ---------- LAYOUT ---------- */
#main{display:flex;height:calc(100% - 46px);width:100%}
#pool{width:236px;min-width:236px;background:var(--panel);border-right:1px solid var(--line);display:flex;flex-direction:column}
#poolHead{display:flex;align-items:center;gap:6px;padding:8px 10px;border-bottom:1px solid var(--line)}
#poolHead h3{margin:0;font-size:12px;letter-spacing:.7px;color:var(--dim);font-weight:600;flex:1}
#poolList{flex:1;overflow-y:auto;padding:8px;display:flex;flex-direction:column;gap:6px}
.scard{background:var(--panel2);border:1px solid var(--line);border-left:3px solid var(--accent);border-radius:4px;
  padding:7px 8px;cursor:grab;display:flex;align-items:center;gap:6px;user-select:none}
.scard:active{cursor:grabbing}
.scard:hover{border-color:var(--accent)}
.scard .nm{flex:1;font-size:12px;font-weight:600;overflow-wrap:anywhere;line-height:1.3;min-width:0}
.scard .src{color:var(--dim);font-weight:400;font-size:11px}
.scard .un{color:var(--accent);font-weight:600;font-size:11px}
.ccol{flex:none;width:20px;height:20px;padding:0;border:1px solid var(--line);border-radius:3px;
  background:transparent;cursor:pointer;appearance:none;-webkit-appearance:none}
.ccol::-webkit-color-swatch-wrapper{padding:2px}
.ccol::-webkit-color-swatch{border:none;border-radius:2px}
.ccol::-moz-color-swatch{border:none;border-radius:2px}
.xbtn{background:transparent;border:none;color:var(--dim);cursor:pointer;font-size:14px;line-height:1;padding:0 2px}
.xbtn:hover{color:var(--bad)}
#poolEmpty{color:var(--dim);font-size:11px;padding:10px;text-align:center;line-height:1.6}

/* ---------- ALARM ESIKLERI (havuz kartinin alt satiri) ---------- */
.scard{flex-wrap:wrap}
.lrow{display:flex;align-items:center;gap:3px;width:100%;margin-top:5px;padding-top:5px;border-top:1px dashed var(--line)}
.lrow b{font-size:9px;color:var(--dim);font-weight:700;letter-spacing:.3px;flex:none}
.lrow input{flex:1;min-width:0;width:100%;padding:2px 4px;font-size:10px;background:#0f131c;
  border:1px solid var(--line);color:var(--txt);border-radius:3px;text-align:right}
.lrow input::-webkit-outer-spin-button,.lrow input::-webkit-inner-spin-button{-webkit-appearance:none;margin:0}
.lrow input.set{border-color:var(--warn);color:#fff}
.lrow .lo.set{border-color:var(--accent)}

/* ---------- ARAYUZDEN OLUSTURULAN KART (ad + formul satiri) ---------- */
.scard.man{border-left-style:solid}
.scard.calc{border-left-style:double;border-left-width:5px}
.scard .nmi{flex:1;min-width:0;padding:2px 4px;font-size:12px;font-weight:600;background:#0f131c;
  border:1px solid transparent;color:var(--txt);border-radius:3px}
.scard .nmi:hover{border-color:var(--line)}
.scard .nmi:focus{border-color:var(--accent);outline:none;background:#0b0f16}
.scard .uni{width:36px;flex:none;padding:2px 3px;font-size:10px;background:#0f131c;text-align:center;
  border:1px solid transparent;color:var(--dim);border-radius:3px}
.scard .uni:hover{border-color:var(--line)}
.scard .uni:focus{border-color:var(--accent);outline:none;color:var(--txt)}
.fxrow{display:flex;align-items:center;gap:3px;width:100%;margin-top:5px}
.fxrow b{font-size:9px;font-style:italic;color:var(--accent);font-weight:700;flex:none;width:13px}
.fxi{flex:1;min-width:0;padding:3px 5px;font-size:11px;background:#0b0f16;border:1px solid var(--line);
  color:var(--txt);border-radius:3px;font-family:ui-monospace,Consolas,"DejaVu Sans Mono",monospace}
.fxi:focus{border-color:var(--accent);outline:none}
.fxi.eq{color:#ffd479}
.fxi.bad{border-color:var(--bad);color:var(--bad)}
.fxv{width:100%;font-size:9px;line-height:1.5;color:var(--dim);margin-top:2px;overflow-wrap:anywhere;
  font-family:ui-monospace,Consolas,"DejaVu Sans Mono",monospace}
.fxv.bad{color:var(--bad)}
.fxv.ok{color:var(--ok)}
.scard.draft{border-left-color:var(--warn) !important;border-color:var(--warn);background:#191d16}
.scard.draft .fxi{border-color:var(--warn)}
.fxtag{font-size:9px;color:var(--accent);border:1px solid #16354e;border-radius:3px;padding:0 3px;flex:none}
.srctag{font-size:9px;color:#d8a13a;border:1px solid #5a4318;border-radius:3px;padding:0 3px;flex:none;cursor:help}

/* ---------- ALARM: YANIP SONME ---------- */
@keyframes alarmCard{0%,100%{background:var(--panel2);border-color:var(--line)}
  50%{background:#4a1118;border-color:var(--bad)}}
@keyframes alarmFrame{0%,100%{border-color:var(--line);box-shadow:none}
  50%{border-color:var(--bad);box-shadow:0 0 0 2px rgba(255,92,108,.55) inset}}
.scard.alarm{animation:alarmCard 1s steps(1,end) infinite}
.card.alarm{animation:alarmFrame 1s steps(1,end) infinite}
.scard.alarm .nm{color:#fff}
#albar{display:none;align-items:center;gap:6px;font-size:11px;font-weight:700;color:var(--bad);
  background:#3a1119;border:1px solid var(--bad);border-radius:4px;padding:3px 8px;white-space:nowrap}
#albar.on{display:flex}
.btn.mute.on{background:#3a2b12;border-color:var(--warn);color:var(--warn)}

/* ---------- PNG DISA AKTARIM: form ogelerinin yerini alan kutucuklar ---------- */
.pv{display:inline-block;background:#161c29;border:1px solid var(--line);border-radius:3px;
  padding:2px 3px;font-size:10px;line-height:1.4;color:var(--txt);text-align:right;min-width:44px;
  vertical-align:middle;white-space:nowrap;box-sizing:border-box}
.pv-sel{background:#0f131c;font-size:11px;padding:3px 5px;text-align:left;min-width:64px}
.pv-col{min-width:0;width:20px;height:20px;padding:0;border-radius:3px}
.pv-dim{color:var(--dim)}
.pv.man{border-color:var(--accent);color:#fff}
.pv.set{border-color:var(--warn);color:#fff}
.lrow .pv{flex:1;min-width:0}

/* ---------- CHART GRID ---------- */
#grid{flex:1;display:grid;grid-template-columns:1fr 1fr;grid-template-rows:1fr 1fr;gap:8px;padding:8px;min-width:0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:5px;display:flex;flex-direction:column;min-width:0;min-height:0;overflow:hidden}
.card.dragover{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent) inset}
.chead{display:flex;align-items:center;gap:2px;padding:5px 6px;border-bottom:1px solid var(--line);background:var(--panel2);flex-wrap:wrap}
.chead select{font-size:11px;padding:3px 2px;max-width:70px}
.chead .btn.sm{padding:3px 5px}
.ygrp{display:flex;align-items:center;gap:1px;background:#0f131c;border:1px solid var(--line);
  border-radius:4px;padding:2px 3px}
.ygrp b{font-size:10px;color:var(--dim);font-weight:700;letter-spacing:.3px}
.ygrp input{width:44px;padding:2px 2px;font-size:10px;border-color:transparent;background:#161c29;text-align:right}
.btn.ex{padding:3px 4px;font-size:10px;font-weight:700;letter-spacing:.2px}
.ygrp input::-webkit-outer-spin-button,.ygrp input::-webkit-inner-spin-button{-webkit-appearance:none;margin:0}
.ygrp input.man{border-color:var(--accent);color:#fff}
.ygrp .btn.sm{padding:2px 5px;font-size:10px;font-weight:700;line-height:1.1}
.ctitle{font-size:11px;font-weight:700;letter-spacing:.3px;color:var(--dim);white-space:nowrap}
.ivl{font-size:10px;color:var(--accent);background:#0f131c;border:1px solid var(--line);border-radius:3px;padding:2px 4px;white-space:nowrap}
.chips{display:flex;flex-wrap:wrap;gap:4px;padding:4px 7px;min-height:26px;border-bottom:1px solid var(--line)}
.chip{display:flex;align-items:center;gap:4px;background:#0f131c;border:1px solid var(--line);border-radius:12px;padding:2px 5px 2px 8px;font-size:11px}
.chip .dot{width:8px;height:8px;border-radius:50%}
.chip .ax{background:#20283a;border:1px solid var(--line);color:var(--txt);border-radius:9px;padding:0 5px;font-size:10px;cursor:pointer}
.chip .ax:hover{border-color:var(--accent)}
.chip .ax.a2{background:#3a2b12;border-color:var(--warn);color:var(--warn)}
.chipsEmpty{color:var(--dim);font-size:11px;padding:3px 2px}
.infobar{padding:3px 7px;font:11px/1.5 Consolas,"Courier New",monospace;color:var(--txt);background:#0f131c;
  border-bottom:1px solid var(--line);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.infobar .k{color:var(--dim)}
.plot{flex:1;min-height:0;position:relative;overflow:hidden}
.uplot{font-family:inherit}
.u-select{background:rgba(78,161,255,.18);border:1px solid rgba(78,161,255,.55)}

/* ---------- GROUP BY + FINE (M/F) ---------- */
.igrp{display:flex;align-items:center;gap:2px;background:#0f131c;border:1px solid var(--line);
  border-radius:4px;padding:2px 3px}
.igrp .ivl{border:none;background:transparent;padding:0 2px;min-width:42px;text-align:center}
.igrp .btn.sm{padding:2px 5px;font-size:10px;font-weight:700;line-height:1.1}

/* ---------- TAM EKRAN (SOLO) ---------- */
#grid.solo{grid-template-columns:1fr;grid-template-rows:1fr}
#grid.solo>.card{display:none}
#grid.solo>.card.full{display:flex}
.btn.fs.on{background:#10351f;border-color:var(--ok);color:var(--ok)}

/* ---------- PRESET HAVUZU (gecici, sadece bu ekran) ---------- */
.psep{display:flex;align-items:center;gap:6px;margin:6px 0 1px;color:var(--warn);
  font-size:10px;letter-spacing:.5px;font-weight:700}
.psep .ln{flex:1;height:1px;background:#3a2b12}
.psep .xbtn{color:var(--warn)}
.scard.pre{border-style:dashed;background:#191a1b}
.scard.dropb{box-shadow:inset 0 3px 0 var(--accent)}
.scard.dropa{box-shadow:inset 0 -3px 0 var(--accent)}
.scard.pre .nm{font-style:italic}
.ptag{font-size:9px;color:var(--warn);border:1px solid #3a2b12;border-radius:3px;padding:0 3px;flex:none}

/* ---------- MODAL ---------- */
#mask{display:none;position:fixed;inset:0;background:rgba(0,0,0,.65);z-index:900;align-items:center;justify-content:center}
#modal{background:var(--panel);border:1px solid var(--line);border-radius:6px;width:420px;max-width:92vw;padding:16px}
#modal h3{margin:0 0 12px;font-size:14px}
.frow{display:flex;flex-direction:column;gap:3px;margin-bottom:10px}
.frow label{font-size:11px;color:var(--dim)}
.frow input{width:100%;padding:6px}
.mfoot{display:flex;gap:8px;justify-content:flex-end;margin-top:14px}
#mmsg{font-size:11px;color:var(--ok);min-height:14px}
#matMask{display:none;position:fixed;inset:0;background:rgba(0,0,0,.65);z-index:900;align-items:center;justify-content:center}
#matModal{background:var(--panel);border:1px solid var(--line);border-radius:6px;width:640px;max-width:94vw;
  max-height:92vh;overflow:auto;padding:16px;font-size:12px}
#matModal h3{margin:0 0 10px;font-size:14px}
#matModal h4{margin:14px 0 6px;font-size:12px;color:var(--dim);text-transform:uppercase;letter-spacing:.5px}
#matModal table{border-collapse:collapse;width:100%}
#matModal th,#matModal td{border:1px solid var(--line);padding:3px 6px;text-align:left}
#matModal th{color:var(--dim);font-weight:600}
#matModal code{background:#0f131c;padding:1px 4px;border-radius:3px;color:var(--accent)}
#matModal textarea{width:100%;min-height:90px;font-family:monospace;font-size:11px;background:#0f131c;
  color:var(--txt);border:1px solid var(--line);border-radius:3px;padding:6px;box-sizing:border-box}
#matModal .units td:nth-child(2){font-weight:700;color:var(--warn)}
#matMsg{min-height:16px;margin-top:6px}
#matMsg.bad{color:var(--bad)} #matMsg.ok{color:var(--ok)}
</style>
</head>
<body>

<div id="errbar"><button id="errclose" onclick="window.hideError()">KAPAT</button><span id="errtxt"></span></div>

<!-- ================= TOP BAR ================= -->
<div id="top">
  <div id="brand">TEST BENCH <span>MONITOR</span></div>
  <div class="sep"></div>
  <span style="font-size:11px;color:var(--dim)">Tumu:</span>
  <button class="btn sm" onclick="window.setAllLive(true)" title="Tum grafikleri canliya al">&#9679; LIVE</button>
  <button class="btn sm" onclick="window.setAllLive(false)" title="Tum grafikleri durdur">&#9646;&#9646; PAUSE</button>
  <div class="sep"></div>
  <span style="font-size:11px;color:var(--dim)">Preset:</span>
  <select id="presetSel" onchange="window.loadPreset(this.value)" style="min-width:150px"><option value="">-- Secilmedi --</option></select>
  <button class="btn sm" onclick="window.savePreset()">Kaydet</button>
  <button class="btn sm" onclick="window.deletePreset()">Sil</button>
  <div class="grow"></div>
  <span id="albar" title="Esik degeri asan sensorler"><span>&#9888;</span><span id="altxt"></span></span>
  <button class="btn sm mute" id="muteBtn" onclick="window.toggleMute()"
          title="Sesli ikazi ac/kapat">&#128266;</button>
  <div class="sep"></div>
  <span id="dbstat"><span id="dbdot"></span><span id="dbtxt">baglanti bekleniyor</span></span>
  <div class="sep"></div>
  <button class="btn" onclick="window.openMaterials()"
          title="Malzeme tablolari (CSV): formulde AD_dens(sicaklik) gibi kullanilir">&#9879; Malzemeler</button>
  <button class="btn" onclick="window.openSettings()">&#9881; Ayarlar</button>
</div>

<!-- ================= MAIN ================= -->
<div id="main">
  <div id="pool">
    <div id="poolHead">
      <h3>SENSOR HAVUZU</h3>
      <button class="btn sm" id="newBtn" onclick="window.newSensorCard()"
              title="Yeni kart: duz sensor adi yazin, ya da '=' ile formul girin">+ New</button>
      <button class="btn sm" onclick="window.clearAllSensors()">Clear All</button>
    </div>
    <div id="poolList"><div id="poolEmpty">Havuz bos.<br>HMI webhook bekleniyor...<br><br>Ctrl+Shift+M = Mock sensor</div></div>
  </div>
  <div id="grid"></div>
</div>

<!-- ================= SETTINGS MODAL ================= -->
<div id="mask">
  <div id="modal">
    <h3>&#9881; InfluxDB Baglanti Ayarlari</h3>
    <div class="frow"><label>InfluxDB URL</label><input type="text" id="cfgUrl" placeholder="http://10.10.2.6:8086"
         title="VARSAYILAN sunucu. Webhook'ta influx_url gonderilen sensorler kendi sunucusunu kullanir."></div>
    <div class="frow"><label>Veritabani Adi (db)</label><input type="text" id="cfgDb" placeholder="lc_100"
         title="VARSAYILAN veritabani. Webhook'ta db gonderilen sensorler kendi db'sini kullanir."></div>
    <div class="frow"><label>Measurement</label><input type="text" id="cfgMeas" placeholder="telemetry"
         title="VARSAYILAN measurement. Webhook'ta measurement gonderilen sensorler kendininkini kullanir."></div>
    <div class="frow"><label>Kullanici Adi</label><input type="text" id="cfgUser" autocomplete="off"></div>
    <div class="frow"><label>Sifre</label><input type="password" id="cfgPass" autocomplete="new-password"></div>
    <div class="frow"><label>Canli Yenileme Periyodu (ms)</label><input type="number" id="cfgRefresh" min="250" step="250"></div>
    <div class="frow"><label>Alarm Ses Seviyesi (%) &mdash; yalnizca bu ekran</label>
      <input type="number" id="cfgVol" min="0" max="100" step="5"></div>
    <div id="mmsg"></div>
    <div class="mfoot">
      <button class="btn" onclick="window.closeSettings()">Iptal</button>
      <button class="btn" onclick="window.testSettings()">Baglantiyi Test Et</button>
      <button class="btn" onclick="window.saveSettings()">Kaydet</button>
    </div>
  </div>
</div>

<!-- ================= MALZEME MODAL ================= -->
<div id="matMask">
  <div id="matModal">
    <h3>&#9879; Malzeme Tablolari</h3>
    <div>Sunucuda saklanir; tum PC'lerde ayni liste gorunur. Formulde
      <code>AD_dens(x)</code> <code>AD_cp(x)</code> <code>AD_dvis(x)</code> <code>AD_cond(x)</code>
      &mdash; <code>x</code> sicaklik [degC]: sayi (<code>SU_dens(25)</code>), sensor (<code>SU_dens(T1)</code>)
      ya da ifade olabilir. Tablodaki satirlar arasinda dogrusal ara deger alinir;
      tablo araliginin <b>disinda sonuc bostur</b>.</div>

    <h4>CSV bicimi &mdash; birimler ZORUNLU</h4>
    <table class="units">
      <tr><th>Sutun</th><th>Birim (zorunlu)</th><th>Anlam</th><th>Formulde</th></tr>
      <tr><td>temp</td><td>degC</td><td>Sicaklik (ara deger ekseni)</td><td>&mdash;</td></tr>
      <tr><td>dens</td><td>kg/m3</td><td>Yogunluk</td><td><code>AD_dens(x)</code></td></tr>
      <tr><td>cp</td><td>J/kg&middot;K</td><td>Ozgul isi</td><td><code>AD_cp(x)</code></td></tr>
      <tr><td>dvis</td><td>Pa&middot;s</td><td>Dinamik viskozite</td><td><code>AD_dvis(x)</code></td></tr>
      <tr><td>cond</td><td>W/m&middot;K</td><td>Isil iletkenlik</td><td><code>AD_cond(x)</code></td></tr>
    </table>
    <div style="margin-top:6px;color:var(--dim)">Ilk satir tam olarak su baslik olmali (sutun sirasi serbest;
      <code>;</code> ayracli ve ondalik virgullu Turkce Excel CSV'si de kabul edilir):<br>
      <code>temp [degC],dens [kg/m3],cp [J/kg&middot;K],dvis [Pa&middot;s],cond [W/m&middot;K]</code></div>

    <h4>Kayitli malzemeler</h4>
    <div id="matList"></div>

    <h4>Yeni malzeme / guncelle</h4>
    <div class="frow"><label>Ad (formulde onek olur: harfle baslar, A-Z a-z 0-9 _)</label>
      <input type="text" id="matName" placeholder="SU" maxlength="32" spellcheck="false"></div>
    <div class="frow"><label>CSV dosyasi</label>
      <input type="file" id="matFile" accept=".csv,text/csv,text/plain"></div>
    <div class="frow"><label>...ya da CSV icerigini yapistirin</label>
      <textarea id="matCsv" spellcheck="false" placeholder="temp [degC],dens [kg/m3],cp [J/kg·K],dvis [Pa·s],cond [W/m·K]&#10;0,998.73,4235.36,0.001779548,0.5567&#10;..."></textarea></div>
    <div id="matMsg"></div>
    <div class="mfoot">
      <button class="btn" onclick="window.closeMaterials()">Kapat</button>
      <button class="btn" id="matSave" onclick="window.saveMaterial()">Kaydet</button>
    </div>
  </div>
</div>

<script>
/* ##########################################################################################
   GOMULU GRAFIK MOTORU  --  uPlot API-UYUMLU, SIFIR DIS BAGIMLILIK
   ------------------------------------------------------------------------------------------
   Fabrika aginda internet/CDN olmadigi icin cizim motoru dogrudan bu dosyaya gomulmustur.
   /vendor klasorunde gercek uPlot varsa ONA dokunulmaz; sadece yoksa bu motor devreye girer.
   Desteklenen API yuzeyi (uPlot ile birebir ayni cagrilar):
     new uPlot(opts, data, host) | .setData(data, resetScales) | .setScale(key,{min,max})
     .setSize({width,height}) | .posToVal(pos,key) | .destroy()
     .scales .data .series .cursor .over .bbox | uPlot.rangeNum(min,max,mult,extend)
   ########################################################################################## */
(function () {
  if (typeof window.uPlot !== 'undefined') return;   /* gercek uPlot yuklendiyse cik */

  function dpr() { return window.devicePixelRatio || 1; }
  function p2(n) { return n < 10 ? '0' + n : '' + n; }

  function niceStep(raw) {
    if (!isFinite(raw) || raw <= 0) return 1;
    var exp = Math.floor(Math.log(raw) / Math.LN10);
    var f = raw / Math.pow(10, exp);
    var n = f <= 1 ? 1 : (f <= 2 ? 2 : (f <= 5 ? 5 : 10));
    return n * Math.pow(10, exp);
  }

  var TIME_STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400];

  function timeStep(span, want) {
    var raw = span / Math.max(2, want || 6);
    for (var i = 0; i < TIME_STEPS.length; i++) if (TIME_STEPS[i] >= raw) return TIME_STEPS[i];
    return TIME_STEPS[TIME_STEPS.length - 1];
  }

  function fmtTimeTick(sec, step) {
    var d = new Date(sec * 1000);
    if (step < 60) return p2(d.getHours()) + ':' + p2(d.getMinutes()) + ':' + p2(d.getSeconds());
    if (step < 86400) return p2(d.getHours()) + ':' + p2(d.getMinutes());
    return p2(d.getDate()) + '.' + p2(d.getMonth() + 1);
  }

  function fmtYTick(v, step) {
    var a = Math.abs(v);
    if (a !== 0 && (a >= 1e6 || a < 1e-4)) return v.toExponential(1);
    var dec = step >= 1 ? 0 : Math.min(6, Math.ceil(-Math.log(step) / Math.LN10));
    var s = v.toFixed(dec);
    return s === '-0' ? '0' : s;
  }

  /* -------------------------------------------------------------------- CONSTRUCTOR */
  function MiniPlot(opts, data, host) {
    var self = this;
    this.opts = opts || {};
    this.series = (this.opts.series || [{}]).slice();
    this.hooks = this.opts.hooks || {};
    this.data = data || [[]];
    this.host = host;
    this.destroyed = false;
    this.dragging = false;

    this.padTop = 10; this.padBottom = 26; this.padLeft = 58; this.padRight = 58;
    (this.opts.axes || []).forEach(function (a) {
      if (!a) return;
      if (a.scale === 'y1' && a.size) self.padLeft = a.size;
      if (a.scale === 'y2' && a.size) self.padRight = a.size;
    });

    this.scales = {
      x:  { min: null, max: null, time: true },
      y1: { min: 0, max: 1 },
      y2: { min: 0, max: 1 }
    };
    this.cursor = { left: -10, top: -10, idx: null };

    var root = document.createElement('div');
    root.className = 'uplot u-mini';
    root.style.position = 'relative';
    root.style.overflow = 'hidden';
    this.root = root;

    var cv = document.createElement('canvas');
    cv.style.position = 'absolute';
    cv.style.left = '0'; cv.style.top = '0';
    this.canvas = cv;
    this.ctx = cv.getContext('2d');
    root.appendChild(cv);

    var over = document.createElement('div');
    over.className = 'u-over';
    over.style.position = 'absolute';
    over.style.cursor = 'crosshair';
    this.over = over;
    root.appendChild(over);

    var sel = document.createElement('div');
    sel.className = 'u-select';
    sel.style.position = 'absolute';
    sel.style.display = 'none';
    sel.style.pointerEvents = 'none';
    this.sel = sel;
    over.appendChild(sel);

    host.appendChild(root);

    this._resize(this.opts.width || host.clientWidth || 320,
                 this.opts.height || host.clientHeight || 180);
    this.setData(this.data, true);
    this._bind();

    (this.opts.plugins || []).forEach(function (p) {
      if (p && p.hooks && typeof p.hooks.ready === 'function') p.hooks.ready(self);
    });
  }

  MiniPlot.prototype._fire = function (name, arg) {
    var hs = this.hooks[name];
    if (!hs || !hs.length) return;
    for (var i = 0; i < hs.length; i++) {
      try { hs[i](this, arg); }
      catch (e) { if (window.showError) window.showError('hook ' + name + ': ' + e.message); }
    }
  };

  /* -------------------------------------------------------------------- GEOMETRI */
  MiniPlot.prototype._resize = function (w, h) {
    this.cssW = Math.max(60, Math.round(w));
    this.cssH = Math.max(60, Math.round(h));
    this.root.style.width = this.cssW + 'px';
    this.root.style.height = this.cssH + 'px';

    var d = dpr();
    this.canvas.width = Math.round(this.cssW * d);
    this.canvas.height = Math.round(this.cssH * d);
    this.canvas.style.width = this.cssW + 'px';
    this.canvas.style.height = this.cssH + 'px';

    this.plotW = Math.max(10, this.cssW - this.padLeft - this.padRight);
    this.plotH = Math.max(10, this.cssH - this.padTop - this.padBottom);

    this.over.style.left = this.padLeft + 'px';
    this.over.style.top = this.padTop + 'px';
    this.over.style.width = this.plotW + 'px';
    this.over.style.height = this.plotH + 'px';

    /* uPlot ile ayni birim: bbox cihaz pikseli cinsindendir */
    this.bbox = { left: this.padLeft * d, top: this.padTop * d, width: this.plotW * d, height: this.plotH * d };
  };

  MiniPlot.prototype.setSize = function (o) {
    if (this.destroyed || !o) return;
    this._resize(o.width, o.height);
    this.redraw();
  };

  MiniPlot.prototype.posToVal = function (pos, key) {
    var s = this.scales[key];
    if (!s || s.min == null || s.max == null) return null;
    if (key === 'x') return s.min + (pos / this.plotW) * (s.max - s.min);
    return s.max - (pos / this.plotH) * (s.max - s.min);
  };

  /* uPlot ile AYNI imza: canPx=true -> cihaz pikseli (bbox ile ayni birim) */
  MiniPlot.prototype.valToPos = function (val, key, canPx) {
    var d = canPx ? dpr() : 1;
    var s = this.scales[key];
    if (!s || s.min == null || s.max == null) return 0;
    if (key === 'x') {
      var sp = (s.max - s.min) || 1;
      return (this.padLeft + (val - s.min) / sp * this.plotW) * d;
    }
    var yr = (s.max - s.min) || 1;
    return (this.padTop + this.plotH - (val - s.min) / yr * this.plotH) * d;
  };

  /* -------------------------------------------------------------------- OLCEKLER */
  MiniPlot.rangeNum = function (min, max, mult, extend) {
    if (min == null || max == null || !isFinite(min) || !isFinite(max)) return [0, 1];
    if (min === max) { min -= 0.5; max += 0.5; }
    var pad = (max - min) * (mult == null ? 0.1 : mult);
    var lo = min - pad, hi = max + pad;
    if (extend) {
      var st = niceStep((hi - lo) / 5);
      lo = Math.floor(lo / st) * st;
      hi = Math.ceil(hi / st) * st;
    }
    if (lo === hi) hi = lo + 1;
    return [lo, hi];
  };

  MiniPlot.prototype._rangeY = function (key) {
    var dmin = null, dmax = null;
    var xs = this.data[0] || [];
    var xmin = this.scales.x.min, xmax = this.scales.x.max;
    for (var si = 1; si < this.series.length; si++) {
      var s = this.series[si] || {};
      if ((s.scale || 'y1') !== key) continue;
      var arr = this.data[si];
      if (!arr) continue;
      for (var i = 0; i < arr.length; i++) {
        if (xmin != null && (xs[i] < xmin || xs[i] > xmax)) continue;
        var v = arr[i];
        if (v == null || !isFinite(v)) continue;
        if (dmin === null || v < dmin) dmin = v;
        if (dmax === null || v > dmax) dmax = v;
      }
    }
    var cfg = (this.opts.scales || {})[key] || {};
    var r;
    if (typeof cfg.range === 'function') r = cfg.range(this, dmin, dmax);
    else r = (dmin === null) ? [0, 1] : MiniPlot.rangeNum(dmin, dmax, 0.12, true);
    if (!r || r.length < 2 || !isFinite(r[0]) || !isFinite(r[1]) || r[0] === r[1]) r = [0, 1];
    this.scales[key].min = r[0];
    this.scales[key].max = r[1];
  };

  MiniPlot.prototype.setData = function (data, resetScales) {
    if (this.destroyed) return;
    this.data = data || [[]];
    var xs = this.data[0] || [];
    if (resetScales !== false || this.scales.x.min == null) {
      if (xs.length > 1) { this.scales.x.min = xs[0]; this.scales.x.max = xs[xs.length - 1]; }
      else if (xs.length === 1) { this.scales.x.min = xs[0] - 1; this.scales.x.max = xs[0] + 1; }
      else { var now = Date.now() / 1000; this.scales.x.min = now - 300; this.scales.x.max = now; }
      this._fire('setScale', 'x');
    }
    this._rangeY('y1'); this._fire('setScale', 'y1');
    this._rangeY('y2'); this._fire('setScale', 'y2');
    this.redraw();
  };

  MiniPlot.prototype.setScale = function (key, o) {
    if (this.destroyed) return;
    var s = this.scales[key];
    if (!s || !o) return;
    if (o.min != null && isFinite(o.min)) s.min = o.min;
    if (o.max != null && isFinite(o.max)) s.max = o.max;
    if (s.max <= s.min) s.max = s.min + 1e-9;
    this._fire('setScale', key);
    if (key === 'x') {
      this._rangeY('y1'); this._fire('setScale', 'y1');
      this._rangeY('y2'); this._fire('setScale', 'y2');
    }
    this.redraw();
  };

  /* -------------------------------------------------------------------- CIZIM */
  MiniPlot.prototype.redraw = function () {
    if (this.destroyed || !this.ctx) return;
    var ctx = this.ctx, d = dpr();
    var L = this.padLeft, T = this.padTop, W = this.plotW, H = this.plotH;
    var sx = this.scales.x, span = sx.max - sx.min;
    if (!isFinite(span) || span <= 0) span = 1;

    ctx.setTransform(d, 0, 0, d, 0, 0);
    ctx.clearRect(0, 0, this.cssW, this.cssH);
    ctx.font = '11px Consolas, "Courier New", monospace';
    ctx.textBaseline = 'middle';
    ctx.lineWidth = 1;

    var GRID = '#222a3b', AXIS = '#8a93a6', BORDER = '#2b3448';

    /* --- X ekseni --- */
    var xstep = timeStep(span, Math.max(3, Math.floor(W / 95)));
    var t0 = Math.ceil(sx.min / xstep) * xstep;
    ctx.textAlign = 'center';
    for (var t = t0; t <= sx.max; t += xstep) {
      var px = Math.round(L + (t - sx.min) / span * W) + 0.5;
      ctx.strokeStyle = GRID;
      ctx.beginPath(); ctx.moveTo(px, T); ctx.lineTo(px, T + H); ctx.stroke();
      ctx.strokeStyle = BORDER;
      ctx.beginPath(); ctx.moveTo(px, T + H); ctx.lineTo(px, T + H + 4); ctx.stroke();
      ctx.fillStyle = AXIS;
      ctx.fillText(fmtTimeTick(t, xstep), px, T + H + 14);
    }

    /* --- Y1 (sol) ve Y2 (sag) eksenleri --- */
    var wantY = Math.max(2, Math.min(8, Math.floor(H / 40)));
    this._drawYAxis('y1', wantY, true);
    this._drawYAxis('y2', wantY, false);

    /* --- Cerceve --- */
    ctx.strokeStyle = BORDER;
    ctx.strokeRect(Math.round(L) + 0.5, Math.round(T) + 0.5, W, H);

    /* --- Seriler --- */
    ctx.save();
    ctx.beginPath(); ctx.rect(L, T, W, H); ctx.clip();
    var xs = this.data[0] || [];
    for (var si = 1; si < this.series.length; si++) {
      var s = this.series[si] || {};
      if (s.show === false) continue;
      var arr = this.data[si];
      if (!arr || !arr.length) continue;
      var sc = this.scales[s.scale || 'y1'];
      var yr = (sc.max - sc.min) || 1;
      ctx.beginPath();
      ctx.strokeStyle = s.stroke || '#4ea1ff';
      ctx.lineWidth = s.width || 1.6;
      ctx.lineJoin = 'round';
      ctx.lineCap = 'round';

      var stride = arr.length > W * 4 ? Math.floor(arr.length / (W * 2)) : 1;
      var started = false;
      for (var i = 0; i < arr.length; i += stride) {
        var v = arr[i], k;
        if (stride > 1) {                       /* kova icindeki uc degeri koru */
          var best = null;
          for (k = i; k < i + stride && k < arr.length; k++) {
            var c = arr[k];
            if (c == null || !isFinite(c)) continue;
            if (best === null || Math.abs(c - sc.min) > Math.abs(best - sc.min)) best = c;
          }
          v = best;
        }
        if (v == null || !isFinite(v)) { if (!s.spanGaps) started = false; continue; }
        var X = L + (xs[i] - sx.min) / span * W;
        var Y = T + H - (v - sc.min) / yr * H;
        if (!started) { ctx.moveTo(X, Y); started = true; } else ctx.lineTo(X, Y);
      }
      ctx.stroke();
    }

    /* --- Eklenti cizimleri (alarm esik cizgileri) --- */
    this._fire('draw');

    /* --- Imlec (crosshair) --- */
    var cl = this.cursor.left, ct = this.cursor.top;
    if (cl >= 0 && cl <= W && ct >= 0 && ct <= H) {
      ctx.setLineDash([3, 3]);
      ctx.strokeStyle = '#6b7690';
      ctx.lineWidth = 1;
      var cx = Math.round(L + cl) + 0.5, cy = Math.round(T + ct) + 0.5;
      ctx.beginPath(); ctx.moveTo(cx, T); ctx.lineTo(cx, T + H); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(L, cy); ctx.lineTo(L + W, cy); ctx.stroke();
      ctx.setLineDash([]);
      var idx = this.cursor.idx;
      if (idx != null && xs.length) {
        for (var sj = 1; sj < this.series.length; sj++) {
          var sr = this.series[sj] || {};
          var ar = this.data[sj];
          if (!ar) continue;
          var vv = ar[idx];
          if (vv == null || !isFinite(vv)) continue;
          var scj = this.scales[sr.scale || 'y1'];
          var yrj = (scj.max - scj.min) || 1;
          var PX = L + (xs[idx] - sx.min) / span * W;
          var PY = T + H - (vv - scj.min) / yrj * H;
          ctx.fillStyle = sr.stroke || '#4ea1ff';
          ctx.beginPath(); ctx.arc(PX, PY, 3, 0, 6.2832); ctx.fill();
        }
      }
    }
    ctx.restore();
  };

  MiniPlot.prototype._drawYAxis = function (key, want, isLeft) {
    var ctx = this.ctx;
    var sc = this.scales[key];
    if (!sc || sc.min == null) return;
    var L = this.padLeft, T = this.padTop, W = this.plotW, H = this.plotH;
    var step = niceStep((sc.max - sc.min) / want);
    var start = Math.ceil(sc.min / step) * step;
    ctx.textAlign = isLeft ? 'right' : 'left';
    for (var v = start; v <= sc.max + step * 1e-6; v += step) {
      var y = Math.round(T + H - (v - sc.min) / (sc.max - sc.min) * H) + 0.5;
      if (y < T - 1 || y > T + H + 1) continue;
      if (isLeft) {
        ctx.strokeStyle = '#222a3b';
        ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(L + W, y); ctx.stroke();
      }
      ctx.strokeStyle = '#2b3448';
      ctx.beginPath();
      if (isLeft) { ctx.moveTo(L - 4, y); ctx.lineTo(L, y); }
      else { ctx.moveTo(L + W, y); ctx.lineTo(L + W + 4, y); }
      ctx.stroke();
      ctx.fillStyle = '#8a93a6';
      ctx.fillText(fmtYTick(v, step), isLeft ? L - 7 : L + W + 7, y);
    }
  };

  /* -------------------------------------------------------------------- ETKILESIM */
  MiniPlot.prototype._nearestIdx = function (px) {
    var xs = this.data[0];
    if (!xs || !xs.length) return null;
    var v = this.posToVal(px, 'x');
    if (v == null) return null;
    var lo = 0, hi = xs.length - 1;
    if (v <= xs[0]) return 0;
    if (v >= xs[hi]) return hi;
    while (hi - lo > 1) { var mid = (lo + hi) >> 1; if (xs[mid] < v) lo = mid; else hi = mid; }
    return (v - xs[lo] <= xs[hi] - v) ? lo : hi;
  };

  MiniPlot.prototype._bind = function () {
    var self = this, over = this.over;

    this._onMove = function (e) {
      if (self.destroyed) return;
      var r = over.getBoundingClientRect();
      self.cursor.left = e.clientX - r.left;
      self.cursor.top = e.clientY - r.top;
      self.cursor.idx = self._nearestIdx(self.cursor.left);
      if (self.dragging) {
        var x2 = Math.min(Math.max(e.clientX - r.left, 0), self.plotW);
        self.sel.style.left = Math.min(self.dragX0, x2) + 'px';
        self.sel.style.width = Math.abs(x2 - self.dragX0) + 'px';
      }
      self._fire('setCursor');
      self.redraw();
    };

    this._onLeave = function () {
      if (self.destroyed || self.dragging) return;
      self.cursor.left = -10; self.cursor.top = -10; self.cursor.idx = null;
      self._fire('setCursor');
      self.redraw();
    };

    this._onDown = function (e) {
      if (self.destroyed || e.button !== 0 || e.shiftKey) return;   /* shift = pan eklentisi */
      var dg = (self.opts.cursor && self.opts.cursor.drag) || {};
      if (dg.x === false) return;
      var r = over.getBoundingClientRect();
      self.dragging = true;
      self.dragX0 = e.clientX - r.left;
      self.sel.style.display = 'block';
      self.sel.style.top = '0px';
      self.sel.style.height = self.plotH + 'px';
      self.sel.style.left = self.dragX0 + 'px';
      self.sel.style.width = '0px';
      e.preventDefault();
    };

    this._onUp = function (e) {
      if (self.destroyed || !self.dragging) return;
      self.dragging = false;
      self.sel.style.display = 'none';
      var r = over.getBoundingClientRect();
      var x2 = Math.min(Math.max(e.clientX - r.left, 0), self.plotW);
      if (Math.abs(x2 - self.dragX0) < 6) return;
      var a = self.posToVal(Math.min(self.dragX0, x2), 'x');
      var b = self.posToVal(Math.max(self.dragX0, x2), 'x');
      self._fire('setSelect');
      self.setScale('x', { min: a, max: b });
    };

    this._onDbl = function () { if (!self.destroyed) self.setData(self.data, true); };

    over.addEventListener('mousemove', this._onMove);
    over.addEventListener('mouseleave', this._onLeave);
    over.addEventListener('mousedown', this._onDown);
    over.addEventListener('dblclick', this._onDbl);
    document.addEventListener('mouseup', this._onUp);
  };

  /* -------------------------------------------------------------------- BELLEK */
  MiniPlot.prototype.destroy = function () {
    if (this.destroyed) return;
    this.destroyed = true;
    try {
      this.over.removeEventListener('mousemove', this._onMove);
      this.over.removeEventListener('mouseleave', this._onLeave);
      this.over.removeEventListener('mousedown', this._onDown);
      this.over.removeEventListener('dblclick', this._onDbl);
      document.removeEventListener('mouseup', this._onUp);
    } catch (e) {}
    try {
      this.canvas.width = 0; this.canvas.height = 0;     /* canvas backing store'u serbest birak */
      if (this.root && this.root.parentNode) this.root.parentNode.removeChild(this.root);
    } catch (e) {}
    this.ctx = null; this.data = null; this.series = null;
    this.hooks = {}; this.opts = {};
  };

  window.uPlot = MiniPlot;
  window.UPLOT_EMBEDDED = true;
})();
</script>

<script>
/* ======================================================================================
   0) GLOBAL ERROR BANNER  (her sey buna bagli, en basta)
   ====================================================================================== */
window.showError = function (msg) {
  var bar = document.getElementById('errbar');
  var txt = document.getElementById('errtxt');
  if (!bar || !txt) { console.error(msg); return; }
  txt.textContent = '[HATA] ' + msg;
  bar.style.display = 'block';
};
window.hideError = function () {
  var bar = document.getElementById('errbar');
  if (bar) bar.style.display = 'none';
};
window.onerror = function (msg, src, line, col, err) {
  window.showError(msg + '   (' + (src || '?') + ':' + line + ':' + col + ')' + (err && err.stack ? '\n' + err.stack : ''));
  return false;
};
window.addEventListener('unhandledrejection', function (e) {
  window.showError('Promise reddedildi: ' + (e.reason && e.reason.message ? e.reason.message : e.reason));
});

/* ======================================================================================
   1) GLOBAL STATE
   ====================================================================================== */
/* --- IKI AYRI HAVUZ ---------------------------------------------------------------
   POOL  : WEBHOOK havuzu. Sunucuda tutulur, WebSocket ile TUM ekranlara yayilir.
   PPOOL : PRESET havuzu. Preset secildiginde dolar, SADECE bu tarayicida yasar.
           Sunucuya hicbir zaman yazilmaz -> baska bir PC'de preset secen biri
           kimsenin ekranini bozamaz. Sayfa yenilenince kaybolur (gecicidir).      */
window.POOL      = [];
window.PPOOL     = [];
window.POOLSIG   = '';    /* havuzun son cizilen yapisi (gereksiz yeniden cizimi onler) */
window.CHARTS    = [];      /* 4 grafik state objesi */
window.SUPPRESS  = 0;       /* kendi setScale cagrilarimiz pause tetiklemesin */
window.WS        = null;
window.REFRESH   = 1000;
window.RANGES    = [];
window.PRESETS   = [];      /* sunucudaki (global) preset adlari */
window.FULL      = -1;      /* tam ekrandaki grafik indeksi, -1 = 4'lu gorunum */

/* Sensor bazli GLOBAL renk haritasi: sensor_id -> '#rrggbb'
   Ayni sensor hangi grafige atilirsa atilsin ayni renkte cizilir. */
window.SCOLORS   = {};
window.COLOR_KEY = 'tbm_colors_v1';

window.PALETTE = ['#4ea1ff', '#ffb454', '#3ddc84', '#ff5c6c', '#b58cff', '#3fd0d4',
                  '#f2e14c', '#ff8f3f', '#7ee081', '#ff7ac6', '#57c7ff', '#c0d94a',
                  '#ffa1a1', '#8ed1fc', '#d6a2ff', '#5ad6a8'];

window.CHART_COUNT = 4;

/* ======================================================================================
   2) YARDIMCI FONKSIYONLAR
   ====================================================================================== */
window.pad = function (n, w) { var s = String(n); while (s.length < w) s = '0' + s; return s; };

window.fmtTimeMs = function (epochSec) {
  if (epochSec == null || !isFinite(epochSec)) return '--';
  var d = new Date(epochSec * 1000);
  return window.pad(d.getHours(), 2) + ':' + window.pad(d.getMinutes(), 2) + ':' +
         window.pad(d.getSeconds(), 2) + '.' + window.pad(d.getMilliseconds(), 3);
};

window.fmtDateTimeMs = function (epochSec) {
  if (epochSec == null || !isFinite(epochSec)) return '--';
  var d = new Date(epochSec * 1000);
  return window.pad(d.getDate(), 2) + '.' + window.pad(d.getMonth() + 1, 2) + ' ' + window.fmtTimeMs(epochSec);
};

window.fmtNum = function (v) {
  if (v == null || !isFinite(v)) return '--';
  var a = Math.abs(v);
  if (a >= 100000 || (a > 0 && a < 0.001)) return v.toExponential(3);
  return v.toFixed(a >= 100 ? 2 : 4);
};

/* Dar min/max kutularina sigacak, gereksiz sifirlari atilmis kisa sayi */
window.fmtBox = function (v) {
  if (v == null || !isFinite(v)) return '';
  var a = Math.abs(v), s;
  if (a >= 100000 || (a > 0 && a < 0.001)) s = v.toExponential(1);
  else if (a >= 1000) s = v.toFixed(0);
  else if (a >= 100) s = v.toFixed(1);
  else if (a >= 1) s = v.toFixed(2);
  else s = v.toPrecision(3);
  if (s.indexOf('.') >= 0 && s.indexOf('e') < 0) s = s.replace(/\.?0+$/, '');
  return s === '-0' ? '0' : s;
};

window.roundNice = function (v) {
  if (v == null || !isFinite(v)) return '';
  var a = Math.abs(v);
  if (a >= 100) return v.toFixed(2);
  if (a >= 1) return v.toFixed(4);
  return v.toPrecision(5);
};

/* 1 - 2 - 5 x 10^n kademesi (eksen yuvarlamalari icin) */
window.niceStep = function (raw) {
  if (!isFinite(raw) || raw <= 0) return 1;
  var exp = Math.floor(Math.log(raw) / Math.LN10);
  var f = raw / Math.pow(10, exp);
  var n = f <= 1 ? 1 : (f <= 2 ? 2 : (f <= 5 ? 5 : 10));
  return n * Math.pow(10, exp);
};

/* FIT araligi:
     alt = min - 0.1*(max-min)   ->  ASAGIYA dogru duzgun bir degere yuvarlanir
     ust = max + 0.1*(max-min)   ->  YUKARIYA dogru duzgun bir degere yuvarlanir
   Boylece eksen ucu 23.87451 gibi kusuratli degil, 20 / 24 / 0.005 gibi okunur olur. */
window.fitRange = function (lo, hi) {
  if (lo == null || hi == null || !isFinite(lo) || !isFinite(hi)) return null;
  var span = hi - lo;
  if (!(span > 0)) span = (Math.abs(hi) > 0 ? Math.abs(hi) * 0.1 : 1);
  var padLo = lo - 0.1 * span;
  var padHi = hi + 0.1 * span;
  var step = window.niceStep((padHi - padLo) / 20);
  var a = Math.floor(padLo / step) * step;
  var b = Math.ceil(padHi / step) * step;
  /* kayan nokta artigi temizligi: 0.30000000000000004 -> 0.3 */
  var dec = Math.max(0, Math.min(12, Math.ceil(-Math.log(step) / Math.LN10) + 1));
  a = parseFloat(a.toFixed(dec));
  b = parseFloat(b.toFixed(dec));
  if (!(b > a)) b = parseFloat((a + step).toFixed(dec));
  return [a, b];
};

window.esc = function (s) {
  return String(s).replace(/[&<>"']/g, function (c) {
    return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c];
  });
};

window.setDbStatus = function (ok, text) {
  var dot = document.getElementById('dbdot');
  var txt = document.getElementById('dbtxt');
  if (dot) dot.style.background = ok === null ? '#555' : (ok ? '#3ddc84' : '#ff5c6c');
  if (txt) txt.textContent = text;
};

/* ======================================================================================
   3) GLOBAL RENK HARITASI  --  bir sensor = bir renk (tum grafiklerde ayni)
   ====================================================================================== */
window.loadColors = function () {
  try { window.SCOLORS = JSON.parse(localStorage.getItem(window.COLOR_KEY) || '{}') || {}; }
  catch (e) { window.SCOLORS = {}; }
};

window.persistColors = function () {
  try { localStorage.setItem(window.COLOR_KEY, JSON.stringify(window.SCOLORS)); } catch (e) {}
};

window.hsl2hex = function (h, s, l) {
  s /= 100; l /= 100;
  var k = function (n) { return (n + h / 30) % 12; };
  var a = s * Math.min(l, 1 - l);
  var f = function (n) {
    var v = l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
    return Math.round(255 * v);
  };
  var hx = function (v) { var t = v.toString(16); return t.length < 2 ? '0' + t : t; };
  return '#' + hx(f(0)) + hx(f(8)) + hx(f(4));
};

/* Once paletten kullanilmamis bir renk, palet bitince rastgele HSL */
window.randColor = function () {
  var used = {};
  Object.keys(window.SCOLORS).forEach(function (k) {
    used[String(window.SCOLORS[k]).toLowerCase()] = true;
  });
  for (var i = 0; i < window.PALETTE.length; i++) {
    if (!used[window.PALETTE[i].toLowerCase()]) return window.PALETTE[i];
  }
  return window.hsl2hex(Math.floor(Math.random() * 360), 60 + Math.floor(Math.random() * 25), 58);
};

/* Sensorun rengi; yoksa ILK EKLEMEDE rastgele atanir ve kalici yazilir */
window.colorFor = function (sensorId) {
  var c = window.SCOLORS[sensorId];
  if (typeof c === 'string' && /^#[0-9a-fA-F]{6}$/.test(c)) return c;
  c = window.randColor();
  window.SCOLORS[sensorId] = c;
  window.persistColors();
  return c;
};

/* Sol panelden renk degisti -> TUM grafiklerde ayni anda guncelle */
window.setSensorColor = function (sensorId, hex) {
  if (!/^#[0-9a-fA-F]{6}$/.test(hex)) return;
  window.SCOLORS[sensorId] = hex;
  window.persistColors();
  window.applyColors();
};

window.applyColors = function () {
  window.CHARTS.forEach(function (C) {
    var touched = false;
    C.series.forEach(function (s, k) {
      var c = window.colorFor(s.sensor_id);
      if (s.color === c) return;
      s.color = c;
      touched = true;
      if (C.u && C.u.series && C.u.series[k + 1]) C.u.series[k + 1].stroke = c;
    });
    if (touched) {
      window.renderChips(C);
      if (C.u && C.u.redraw) { try { C.u.redraw(); } catch (e) {} }
    }
  });
  var list = document.getElementById('poolList');
  if (list) {
    window.allSensors().forEach(function (s) {
      var card = list.querySelector('[data-sid="' + window.cssEsc(s.sensor_id) + '"]');
      if (card) card.style.borderLeftColor = window.colorFor(s.sensor_id);
    });
  }
};

window.cssEsc = function (s) { return String(s).replace(/["\\]/g, '\\$&'); };

/* ======================================================================================
   3b) SENSOR HAVUZU (SOL PANEL)  --  REAKTIF, F5 GEREKTIRMEZ
   ====================================================================================== */
/* Gosterim bicimi:  NAME (influxdb_name) [UNIT]   -- name yoksa influxdb_name kullanilir */
window.sensorLabel = function (s) {
  if (!s) return '';
  var nm = (s.name || '').trim();
  var out = nm ? window.esc(nm) + ' <span class="src">(' + window.esc(s.sensor_id) + ')</span>'
               : window.esc(s.sensor_id);
  if (s.unit) out += ' <span class="un">[' + window.esc(s.unit) + ']</span>';
  return out;
};

/* Grafik chip'leri icin kisa, duz metin etiket */
window.sensorLabelText = function (s) {
  if (!s) return '';
  var nm = (s.name || '').trim();
  var out = nm ? nm + ' (' + s.sensor_id + ')' : s.sensor_id;
  if (s.unit) out += ' [' + s.unit + ']';
  return out;
};

/* Webhook havuzu + preset havuzu birlestirilmis liste (webhook onceliklidir:
   ayni sensor her ikisinde de varsa GERCEK webhook kaydi kullanilir) */
window.allSensors = function () {
  var out = [], seen = {};
  window.POOL.forEach(function (s) { seen[s.sensor_id] = true; out.push(s); });
  window.PPOOL.forEach(function (s) { if (!seen[s.sensor_id]) out.push(s); });
  return out;
};

window.isPresetOnly = function (sensorId) {
  for (var i = 0; i < window.POOL.length; i++) if (window.POOL[i].sensor_id === sensorId) return false;
  for (var j = 0; j < window.PPOOL.length; j++) if (window.PPOOL[j].sensor_id === sensorId) return true;
  return false;
};

/* Bir girdi kutusu odaktayken kartin surukleme davranisi kapatilir */
window.noDrag = function (card, el) {
  if (!el) return;
  el.addEventListener('mousedown', function (ev) { ev.stopPropagation(); card.draggable = false; });
  el.addEventListener('focus', function () { card.draggable = false; });
  el.addEventListener('blur', function () { card.draggable = true; });
};

/* SENSOR BAZLI KAYNAK rozeti.
   Kartta db / measurement / influx_url gecersiz kilmasi varsa kucuk bir "db"
   isareti cikar; fareyle uzerine gelince hangi alanlarin genel ayardan
   AYRILDIGI okunur. Hicbiri yoksa rozet de yoktur (genel ayar kullaniliyor). */
window.srcTagHtml = function (s) {
  if (!s) return '';
  var bits = [];
  if (s.db) bits.push('db = ' + s.db);
  if (s.meas) bits.push('measurement = ' + s.meas);
  if (s.url) bits.push('sunucu = ' + s.url);
  if (!bits.length) return '';
  return '<span class="srctag" title="' +
         window.esc('Bu kart genel ayar yerine kendi kaynagini kullaniyor:\n' + bits.join('\n')) +
         '">db</span>';
};

/* ALARM satirinin ortak HTML'i */
window.limRowHtml = function (lim) {
  return '<span class="lrow"><b>ALARM</b>' +
    '<input class="lo' + (lim.min != null ? ' set' : '') + '" type="number" step="any" placeholder="min" ' +
      'title="Alarm alt esigi (bos = kapali)" value="' + (lim.min != null ? lim.min : '') + '">' +
    '<input class="hi' + (lim.max != null ? ' set' : '') + '" type="number" step="any" placeholder="max" ' +
      'title="Alarm ust esigi (bos = kapali)" value="' + (lim.max != null ? lim.max : '') + '">' +
  '</span>';
};

/* Tek bir sensor kartini uretir.
   * WEBHOOK kartlari: ad sabit metin (HMI gonderir).
   * ARAYUZ kartlari ("+ New" ile eklenenler): ad/birim duzenlenebilir ve altinda
     Excel'in formul cubugu gibi bir "fx" satiri bulunur.                        */
window.makeSensorCard = function (s, isPreset) {
  var sid = s.sensor_id;
  var col = window.colorFor(sid);
  var ex  = window.exprOf(sid);
  var man = window.isManual(sid);
  var lim = window.limitsOf(sid);

  var card = document.createElement('div');
  card.className = 'scard' + (isPreset ? ' pre' : '') + (man ? ' man' : '') + (ex ? ' calc' : '');
  card.draggable = true;
  card.setAttribute('data-sid', sid);
  card.style.borderLeftColor = col;
  card.title = isPreset ? 'Preset havuzundan (gecici -- yalnizca bu ekranda)'
             : (ex ? 'HESAP karti: degeri formulden uretilir (InfluxDB alani degildir)'
                   : (man ? 'Arayuzden eklendi -- webhook gerekmez'
                          : 'Webhook havuzu (tum ekranlarda ortak)'));

  var colHtml = '<input type="color" class="ccol" value="' + col + '" title="Seri rengi (tum grafikler)">';
  var head = man
    ? colHtml +
      '<input class="nmi" type="text" placeholder="ad" title="Kart adi -- serbestce degistirilebilir">' +
      '<input class="uni" type="text" placeholder="br" title="Olcu birimi">' +
      (ex ? '<span class="fxtag" title="Hesap karti (formul sonucu)">fx</span>' : '') +
      window.srcTagHtml(s) +
      '<button class="xbtn" title="Karti sil">&#10005;</button>'
    : colHtml +
      '<span class="nm">' + window.sensorLabel(s) + '</span>' +
      (isPreset ? '<span class="ptag">P</span>' : '') +
      window.srcTagHtml(s) +
      '<button class="xbtn" title="' + (isPreset ? 'Preset havuzundan cikar' : 'Sensoru sil') + '">&#10005;</button>';

  var fxHtml = man
    ? '<span class="fxrow"><b>fx</b>' +
        '<input class="fxi" type="text" spellcheck="false" autocomplete="off" ' +
          'title="Duz sensor adi = InfluxDB alani.  &quot;=&quot; ile baslarsa formul." ' +
          'placeholder="P1   ya da   =P1*2"></span>' +
      '<span class="fxv"></span>'
    : '';

  card.innerHTML = head + fxHtml + window.limRowHtml(lim);

  card.addEventListener('dragstart', function (ev) {
    ev.dataTransfer.setData('text/plain', sid);
    ev.dataTransfer.effectAllowed = 'copyMove';      /* grafige kopya, havuzda tasima */
    window.POOLDRAG = { sid: sid, pre: !!isPreset };
  });
  card.addEventListener('dragend', function () {
    window.POOLDRAG = null;
    window.clearDropMarks();
  });

  /* --- Esik kutulari --------------------------------------------------------- */
  ['lo', 'hi'].forEach(function (cls) {
    var inp = card.querySelector('.' + cls);
    if (!inp) return;
    var side = (cls === 'lo') ? 'min' : 'max';
    window.noDrag(card, inp);
    inp.addEventListener('change', function () { window.setLimit(sid, side, this.value); });
  });

  /* --- Renk ------------------------------------------------------------------ */
  var colIn = card.querySelector('.ccol');
  window.noDrag(card, colIn);
  colIn.addEventListener('input', function () {
    card.style.borderLeftColor = this.value;
    window.setSensorColor(sid, this.value);
  });

  /* --- Arayuz karti: ad / birim / formul ------------------------------------- */
  if (man) {
    var nmi = card.querySelector('.nmi');
    var uni = card.querySelector('.uni');
    var fxi = card.querySelector('.fxi');

    nmi.value = s.name || '';
    uni.value = s.unit || '';
    fxi.value = ex ? ('=' + ex) : sid;
    fxi.classList.toggle('eq', !!ex);

    window.noDrag(card, nmi);
    window.noDrag(card, uni);
    window.noDrag(card, fxi);

    var saveLabel = function () { window.renameCard(sid, nmi.value, uni.value); };
    nmi.addEventListener('change', saveLabel);
    uni.addEventListener('change', saveLabel);
    [nmi, uni].forEach(function (el) {
      el.addEventListener('keydown', function (ev) { if (ev.key === 'Enter') el.blur(); });
    });

    fxi.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter') { ev.preventDefault(); fxi.blur(); }
      if (ev.key === 'Escape') { fxi.value = ex ? ('=' + ex) : sid; fxi.blur(); }
    });
    fxi.addEventListener('input', function () {
      var t = fxi.value.trim();
      fxi.classList.toggle('eq', t.charAt(0) === '=');
      window.fxPreview(card, t, sid);
    });
    fxi.addEventListener('change', function () {
      window.applyCardSource(sid, fxi.value, nmi.value, uni.value);
    });

    window.paintFxRow(card, sid);
  }

  card.querySelector('.xbtn').addEventListener('click', function (ev) {
    ev.stopPropagation();
    if (isPreset) window.removePresetSensor(sid);
    else window.deleteSensor(sid);
  });
  return card;
};

/* fx satirinin altindaki durum yazisini tazeler (DOM'u yeniden kurmadan) */
window.paintFxRow = function (card, sid) {
  var out = card.querySelector('.fxv');
  if (!out) return;
  var fxi = card.querySelector('.fxi');
  if (fxi && document.activeElement === fxi) return;   /* yazarken ustune yazma */
  var st = window.fxStatus(sid);
  if (!st.txt && !window.isCalc(sid)) {
    var v = window.LASTVAL[sid];
    st = { txt: (v == null || !isFinite(v)) ? '' : (sid + ' = ' + window.fxNum(v)), cls: '' };
  }
  /* Kutu dar: tam formul fareyle ustune gelince gorunsun */
  if (fxi) {
    var ex = window.exprOf(sid);
    fxi.title = ex ? ('=' + ex + '\n\n' + st.txt) : ('InfluxDB alani: ' + sid);
  }
  out.textContent = st.txt;
  out.className = 'fxv' + (st.cls ? ' ' + st.cls : '');
  if (fxi) fxi.classList.toggle('bad', st.cls === 'bad');
};

/* Yazarken canli geri bildirim (henuz kaydetmeden) */
window.fxPreview = function (card, text, selfId) {
  var out = card.querySelector('.fxv');
  if (!out) return;
  var set = function (txt, cls) {
    out.textContent = txt;
    out.className = 'fxv' + (cls ? ' ' + cls : '');
    var fxi = card.querySelector('.fxi');
    if (fxi) fxi.classList.toggle('bad', cls === 'bad');
  };
  text = String(text || '').trim();
  if (!text) { set('bos birakilamaz', 'bad'); return; }

  if (text.charAt(0) !== '=') {
    if (!window.validSid(text)) { set('gecersiz ad (izinli: A-Z a-z 0-9 _ . -)', 'bad'); return; }
    var clash = window.poolFind(text);
    if (clash && text !== selfId) { set('"' + text + '" zaten havuzda', 'bad'); return; }
    set('InfluxDB alani: ' + text, 'ok');
    return;
  }

  var c = window.fxCompile(text.slice(1));
  if (c.err) { set('! ' + c.err, 'bad'); return; }
  var bad = window.fxBadNames(c.deps);
  if (bad.length) { set('gecersiz ad: ' + bad.join(', '), 'bad'); return; }
  if (selfId && window.fxWouldCycle(selfId, c.deps)) { set('dongusel formul', 'bad'); return; }
  var ext = window.fxExternal(c.deps);
  set('tamam -- kullanilan: ' + (c.deps.length ? c.deps.join(', ') : 'yalnizca sabitler') +
      (ext.length ? '  (InfluxDB\'den: ' + ext.join(', ') + ')' : ''), 'ok');
};

/* ======================================================================================
   3b-2) YENI KART ("+ New")  --  havuzda dogrudan acilan, yazilabilir taslak kart
   ====================================================================================== */
window.DRAFT = null;      /* {name, unit, text} -- yalnizca bu ekranda, kaydedilene dek */

window.slugId = function (txt) {
  var map = { 'ç': 'c', 'Ç': 'C', 'ğ': 'g', 'Ğ': 'G', 'ı': 'i', 'İ': 'I',
              'ö': 'o', 'Ö': 'O', 'ş': 's', 'Ş': 'S', 'ü': 'u', 'Ü': 'U' };
  var out = String(txt || '').replace(/[çÇğĞıİöÖşŞüÜ]/g, function (m) { return map[m] || m; });
  return out.replace(/[^A-Za-z0-9_]+/g, '_').replace(/^_+|_+$/g, '').toUpperCase().slice(0, 40);
};

/* Hesap kartlari InfluxDB'de yoktur: kendi kalici kimliklerini uretirler.
   Kimlik BIR KEZ atanir; ad sonradan degisse de grafik baglantilari kopmaz. */
window.newCalcId = function (hint) {
  var base = 'CALC_' + (window.slugId(hint) || 'X');
  var taken = {};
  window.allSensors().forEach(function (s) { taken[s.sensor_id] = 1; });
  if (!taken[base]) return base;
  for (var k = 2; k < 999; k++) { if (!taken[base + '_' + k]) return base + '_' + k; }
  return base + '_' + Date.now();
};

/* Sunucuya kart yaz (webhook ucu -- hem duz hem hesap kartlari icin ayni yol) */
window.pushSensor = function (id, name, unit, exprText, lim) {
  var p = 'influx_name=' + encodeURIComponent(id) +
          '&name=' + encodeURIComponent(name || '') +
          '&unit=' + encodeURIComponent(unit || '') +
          '&expr=' + encodeURIComponent(exprText || '') +
          '&manual=1';
  if (lim && lim.min != null) p += '&min=' + encodeURIComponent(lim.min);
  if (lim && lim.max != null) p += '&max=' + encodeURIComponent(lim.max);
  return fetch('/api/webhook/sensor?' + p).then(function (r) {
    if (!r.ok) {
      return r.json().catch(function () { return {}; }).then(function (e) {
        throw new Error(e.detail || ('HTTP ' + r.status));
      });
    }
    return r.json();
  }).then(function (j) {
    if (j && j.sensors) window.applyPool(j.sensors);
    return j;
  });
};

window.newSensorCard = function () {
  if (window.DRAFT) {
    var open = document.querySelector('#poolList .scard.draft .nmi');
    if (open) open.focus();
    return;
  }
  window.DRAFT = { name: '', unit: '', text: '' };
  window.renderPool(true);
  var el = document.querySelector('#poolList .scard.draft .nmi');
  if (el) { el.focus(); el.scrollIntoView({ block: 'nearest' }); }
  var lst = document.getElementById('poolList');
  if (lst) lst.scrollTop = 0;
};

window.cancelDraft = function () {
  window.DRAFT = null;
  window.renderPool(true);
};

window.commitDraft = function () {
  var d = window.DRAFT;
  if (!d) return;
  var card = document.querySelector('#poolList .scard.draft');
  var text = String(d.text || '').trim();
  var name = String(d.name || '').trim();
  var unit = String(d.unit || '').trim();
  var fail = function (msg) { if (card) window.fxPreview(card, text || ' ', null); window.showError(msg); };

  if (!text) { fail('Kaynak satiri bos: duz sensor adi yazin ya da "=" ile formul girin.'); return; }

  var id, exprText;
  if (text.charAt(0) === '=') {
    var src = text.slice(1).trim();
    var c = window.fxCompile(src);
    if (c.err) { fail('Formul hatasi: ' + c.err); return; }
    id = window.newCalcId(name || src);
    exprText = '=' + src;
    if (!name) name = id;
  } else {
    if (!window.validSid(text)) { fail('Gecersiz sensor adi (izinli: A-Z a-z 0-9 _ . -)'); return; }
    if (window.poolFind(text)) { fail('"' + text + '" zaten havuzda.'); return; }
    id = text;
    exprText = '';
  }

  /* Odak hala taslaktaki kutuda: once birak, yoksa "yazarken cizme" korumasi
     yeni karti ekrana basmayi engeller. */
  if (document.activeElement && document.activeElement.blur) document.activeElement.blur();

  window.DRAFT = null;
  window.pushSensor(id, name, unit, exprText, null)
    .then(function () {
      window.renderPool(true);
      window.refreshAll(false);
      window.alarmWatch();
    })
    .catch(function (e) {
      window.showError('Kart olusturulamadi: ' + e.message);
      window.renderPool(true);
    });
};

/* Taslak kart: henuz havuzda YOK, yalnizca DOM'da yasar */
window.makeDraftCard = function () {
  var d = window.DRAFT || { name: '', unit: '', text: '' };
  var card = document.createElement('div');
  card.className = 'scard man draft';
  card.draggable = false;
  card.setAttribute('data-draft', '1');
  card.title = 'Yeni kart -- kaydetmek icin Enter, vazgecmek icin Esc';
  card.innerHTML =
    '<input class="nmi" type="text" placeholder="ad" title="Kart adi">' +
    '<input class="uni" type="text" placeholder="br" title="Olcu birimi">' +
    '<button class="xbtn" title="Vazgec">&#10005;</button>' +
    '<span class="fxrow"><b>fx</b>' +
      '<input class="fxi" type="text" spellcheck="false" autocomplete="off" ' +
        'placeholder="P1   ya da   =P1*2" ' +
        'title="Duz ad yazarsaniz o InfluxDB alani olur. &quot;=&quot; ile formul girilir."></span>' +
    '<span class="fxv">sensor adi yazin, ya da = ile formul</span>';

  var nmi = card.querySelector('.nmi');
  var uni = card.querySelector('.uni');
  var fxi = card.querySelector('.fxi');
  nmi.value = d.name; uni.value = d.unit; fxi.value = d.text;

  nmi.addEventListener('input', function () { window.DRAFT.name = this.value; });
  uni.addEventListener('input', function () { window.DRAFT.unit = this.value; });
  fxi.addEventListener('input', function () {
    window.DRAFT.text = this.value;
    fxi.classList.toggle('eq', this.value.trim().charAt(0) === '=');
    window.fxPreview(card, this.value, null);
  });

  [nmi, uni, fxi].forEach(function (el) {
    el.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter') { ev.preventDefault(); window.commitDraft(); }
      else if (ev.key === 'Escape') { ev.preventDefault(); window.cancelDraft(); }
    });
  });

  card.querySelector('.xbtn').addEventListener('click', function (ev) {
    ev.stopPropagation();
    window.cancelDraft();
  });
  return card;
};

/* ---- Mevcut bir arayuz kartinin ad / birim / kaynak satirini degistirme --------- */
window.renameCard = function (sid, name, unit) {
  /* HESAP karti: degisken adi (CALC_<AD>) kart adini izler; bu adi kullanan
     diger formuller de ayni anda guncellenir. */
  if (window.isCalc(sid)) {
    var want = window.calcIdFor(name, sid);
    if (want !== sid) return window.changeCardId(sid, want, name, unit, undefined, true);
  }
  if (window.isPresetOnly(sid)) {
    var s = window.poolFind(sid);
    if (s) { s.name = String(name || '').trim(); s.unit = String(unit || '').trim(); }
    window.renderPool(true);
    window.syncChartsWithPool();
    return Promise.resolve();
  }
  return window.pushSensor(sid, name, unit, window.isCalc(sid) ? ('=' + window.exprOf(sid)) : '', null)
    .then(function () { window.syncChartsWithPool(); })
    .catch(function (e) { window.showError('Ad kaydedilemedi: ' + e.message); });
};

window.setExpr = function (sid, text) {
  if (window.isPresetOnly(sid)) {
    var s = window.poolFind(sid);
    if (s) {
      s.expr = String(text || '').replace(/^=/, '').trim() || null;
      s.manual = true;
    }
    window.renderPool(true);
    window.refreshAll(false);
    window.alarmWatch();
    return Promise.resolve();
  }
  return fetch('/api/sensors/' + encodeURIComponent(sid) + '/expr?expr=' + encodeURIComponent(text || ''))
    .then(function (r) { return r.json(); })
    .then(function (j) {
      if (j && j.sensors) window.applyPool(j.sensors);
      window.renderPool(true);
      window.refreshAll(false);
      window.alarmWatch();
    })
    .catch(function (e) { window.showError('Formul kaydedilemedi: ' + e.message); });
};

/* Grafiklerdeki / renkteki / alarmdaki id baglantilarini yeni id'ye tasir */
window.remapSeries = function (oldId, newId) {
  window.CHARTS.forEach(function (C) {
    var hit = false;
    C.series.forEach(function (s) { if (s.sensor_id === oldId) { s.sensor_id = newId; hit = true; } });
    if (hit) { window.renderChips(C); window.buildChart(C); }
  });
  if (window.LASTVAL[oldId] !== undefined) {
    window.LASTVAL[newId] = window.LASTVAL[oldId];
    delete window.LASTVAL[oldId];
  }
  if (window.ALARM[oldId]) { window.ALARM[newId] = true; delete window.ALARM[oldId]; }
};

/* Hesap kartinin adina gore olmasi gereken degisken adi. Ad bos ya da ayni
   kimlige denk geliyorsa (CALC_GUC, CALC_GUC_2 ...) mevcut kimlik korunur. */
window.calcIdFor = function (name, selfId) {
  var slug = window.slugId(name);
  if (!slug) return selfId;
  var base = 'CALC_' + slug;
  if (selfId === base || new RegExp('^' + base + '_\\d+$').test(selfId)) return selfId;
  return window.newCalcId(name);
};

/* Kartin KIMLIGINI degistirir: sunucuda yerinde (sira, esik, kaynak korunur),
   grafik serileri / renk / alarm baglantilari yeni kimlige tasinir.
   exprText: undefined = formule dokunma, '' = formulu kaldir, '=...' = yeni formul.
   rewrite : eski kimligi kullanan diger formuller de yeni ada cevrilsin mi. */
window.changeCardId = function (oldId, newId, name, unit, exprText, rewrite) {
  if (newId !== oldId && window.poolFind(newId)) {
    window.showError('"' + newId + '" zaten havuzda.');
    window.renderPool(true);
    return Promise.resolve();
  }
  var preOnly = window.isPresetOnly(oldId);
  var rec = window.poolFind(oldId);
  var srvExprs = {}, localBack = [];
  if (rewrite && newId !== oldId) {
    window.allSensors().forEach(function (s) {
      if (s.sensor_id === oldId || !s.expr) return;
      var ne = window.fxRename(s.expr, oldId, newId);
      if (ne === s.expr) return;
      if (window.isPresetOnly(s.sensor_id)) { localBack.push([s, s.expr]); s.expr = ne; }
      else srvExprs[s.sensor_id] = ne;
    });
  }
  var oldRec = rec ? { sensor_id: rec.sensor_id, name: rec.name, unit: rec.unit,
                       expr: rec.expr, manual: rec.manual } : null;

  /* Yerelde hemen uygula: aradaki bir havuz yayini seriyi "havuzda yok" diye silmesin */
  window.SCOLORS[newId] = window.colorFor(oldId);
  window.persistColors();
  if (rec) {
    rec.sensor_id = newId;
    rec.name = String(name || '').trim();
    rec.unit = String(unit || '').trim();
    if (exprText !== undefined) {
      rec.expr = String(exprText || '').replace(/^=/, '').trim() || null;
      rec.manual = true;
    }
  }
  Object.keys(srvExprs).forEach(function (k) {
    var o = window.poolFind(k);
    if (o) o.expr = srvExprs[k];
  });
  window.remapSeries(oldId, newId);

  var done = function () {
    window.renderPool(true);
    window.syncChartsWithPool();
    window.refreshAll(false);
    window.alarmWatch();
  };

  if (preOnly) {
    /* Preset karti yalnizca bu ekranda: sunucudaki kartlarin formulleri ayrica yazilir */
    var jobs = Object.keys(srvExprs).map(function (k) {
      return fetch('/api/sensors/' + encodeURIComponent(k) + '/expr?expr=' + encodeURIComponent(srvExprs[k]));
    });
    done();
    return Promise.all(jobs).catch(function (e) { window.showError('Formul kaydedilemedi: ' + e.message); });
  }

  return fetch('/api/sensors/' + encodeURIComponent(oldId) + '/rename', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      to: newId, name: String(name || '').trim(), unit: String(unit || '').trim(),
      expr: exprText === undefined ? null : String(exprText || ''),
      exprs: srvExprs
    })
  }).then(function (r) {
    return r.json().catch(function () { return {}; }).then(function (j) {
      if (!r.ok) throw new Error(j.detail || ('HTTP ' + r.status));
      return j;
    });
  }).then(function (j) {
    if (j && j.sensors) window.applyPool(j.sensors);
    done();
  }).catch(function (e) {
    /* geri al: yerel degisiklikleri eski haline dondur, sunucudan taze havuz cek */
    if (rec && oldRec) { for (var k in oldRec) rec[k] = oldRec[k]; }
    localBack.forEach(function (p) { p[0].expr = p[1]; });
    window.remapSeries(newId, oldId);
    window.showError('Kart guncellenemedi: ' + e.message);
    return fetch('/api/sensors').then(function (r) { return r.json(); })
      .then(function (j) { if (j && j.sensors) window.applyPool(j.sensors); done(); });
  });
};

/* Kaynak satiri baska bir alana / formule gecti: kimlik degisir (yerinde) */
window.replaceCard = function (oldId, newId, name, unit, exprText) {
  /* Eski kimlik bir HESAP kimligiyse kartin kendisini gosterir: onu kullanan
     formuller yeni kimlige tasinir. Duz InfluxDB alaniysa (P1) formuller o
     ALANI kastediyordur, dokunulmaz. */
  return window.changeCardId(oldId, newId, name, unit, exprText, window.isCalc(oldId));
};

/* fx satiri degisti: duz ad <-> formul gecisleri dahil hepsini burasi karara baglar */
window.applyCardSource = function (sid, text, name, unit) {
  text = String(text || '').trim();
  var wasExpr = window.exprOf(sid);

  if (!text) {
    window.showError('Kaynak satiri bos birakilamaz.');
    window.renderPool(true);
    return;
  }

  if (text.charAt(0) === '=') {
    var src = text.slice(1).trim();
    var c = window.fxCompile(src);
    if (c.err) { window.showError('Formul hatasi: ' + c.err); return; }
    if (window.fxWouldCycle(sid, c.deps)) {
      window.showError('Dongusel formul reddedildi: ' + sid + ' kendine donuyor.');
      window.renderPool(true);
      return;
    }
    if (wasExpr) {
      if (src === wasExpr) return;              /* degismedi */
      window.setExpr(sid, '=' + src);           /* kimlik sabit kalir */
    } else {
      /* duz karttan hesap kartina gecis -> yeni (sentetik) kimlik gerekir */
      window.replaceCard(sid, window.newCalcId(name || src), name, unit, '=' + src);
    }
    return;
  }

  if (!window.validSid(text)) { window.showError('Gecersiz sensor adi (izinli: A-Z a-z 0-9 _ . -)'); return; }
  if (text === sid) {
    if (wasExpr) window.setExpr(sid, '');       /* formulu kaldir, duz alana don */
    return;
  }
  window.replaceCard(sid, text, name, unit, '');
};

/* Havuzun YAPISAL parmak izi (esik DEGERLERI haric -- onlar yerinde guncellenir).
   Degismediyse kartlari yeniden uretmeyiz; yeniden uretmek devam eden
   yanip sonme animasyonlarini sifirlar ve alarmlar birbirini etkiler. */
window.poolSig = function () {
  var f = function (tag) {
    return function (s) {
      return tag + '|' + s.sensor_id + '|' + (s.name || '') + '|' + (s.unit || '') +
             '|' + (s.expr || '') + '|' + (s.manual ? 1 : 0) +
             '|' + window.colorFor(s.sensor_id);
    };
  };
  return window.POOL.map(f('W')).join('\n') + '\n--\n' + window.PPOOL.map(f('P')).join('\n') +
         '\n--D--' + (window.DRAFT ? '1' : '0');
};

/* Havuzda bir metin kutusu duzenleniyor mu? (yeniden cizim yazilani siler) */
window.poolBusy = function () {
  var a = document.activeElement;
  if (!a || !a.classList) return false;
  if (!(a.classList.contains('fxi') || a.classList.contains('nmi') || a.classList.contains('uni'))) return false;
  var p = a.parentNode;
  while (p) { if (p.id === 'poolList') return true; p = p.parentNode; }
  return false;
};

/* Esik kutularinin degerlerini ve fx durum satirlarini DOM'u yeniden kurmadan tazele */
window.syncLimitInputs = function () {
  var cards = document.querySelectorAll('#poolList .scard');
  for (var i = 0; i < cards.length; i++) {
    (function (card) {
      var sid = card.getAttribute('data-sid');
      if (!sid) return;                                     /* taslak kart */
      var lim = window.limitsOf(sid);
      [['lo', lim.min], ['hi', lim.max]].forEach(function (pair) {
        var el = card.querySelector('.' + pair[0]);
        if (!el || document.activeElement === el) return;   /* yazarken ustune yazma */
        el.value = (pair[1] == null) ? '' : pair[1];
        el.classList.toggle('set', pair[1] != null);
      });
      if (card.querySelector('.fxv')) window.paintFxRow(card, sid);
    })(cards[i]);
  }
};

window.renderPool = function (force) {
  var list = document.getElementById('poolList');
  if (!list) return;

  /* Kullanici bir ad/formul kutusuna yaziyorsa DOM'u ASLA yeniden kurma */
  if (!force && window.poolBusy()) {
    window.applyAlarmClasses();
    return;
  }

  /* Yapi aynıysa DOM'a DOKUNMA: yanip sonen kartlarin fazi korunur */
  var sig = window.poolSig();
  if (!force && sig === window.POOLSIG && list.querySelector('.scard')) {
    window.syncLimitInputs();
    window.applyAlarmClasses();
    return;
  }
  window.POOLSIG = sig;
  list.innerHTML = '';

  var extra = window.PPOOL.filter(function (s) {
    for (var i = 0; i < window.POOL.length; i++) if (window.POOL[i].sensor_id === s.sensor_id) return false;
    return true;
  });

  if (!window.POOL.length && !extra.length && !window.DRAFT) {
    var e = document.createElement('div');
    e.id = 'poolEmpty';
    e.innerHTML = 'Havuz bos.<br>HMI webhook bekleniyor...<br><br>' +
                  '<b>+ New</b> ile elle kart ekleyebilirsiniz<br>(duz sensor adi ya da = formul)' +
                  '<br><br>Ctrl+Shift+M = Mock sensor';
    list.appendChild(e);
    return;
  }

  /* Yeni kart taslagi listenin EN USTUNDE acilir (kaydedilince de orada kalir) */
  if (window.DRAFT) list.appendChild(window.makeDraftCard());

  window.POOL.forEach(function (s) { list.appendChild(window.makeSensorCard(s, false)); });

  if (extra.length) {
    var sep = document.createElement('div');
    sep.className = 'psep';
    sep.innerHTML = '<span>PRESET HAVUZU</span><span class="ln"></span>' +
                    '<button class="xbtn" title="Preset havuzunu tamamen temizle">&#10005;</button>';
    sep.querySelector('.xbtn').addEventListener('click', window.clearPresetPool);
    list.appendChild(sep);
    extra.forEach(function (s) { list.appendChild(window.makeSensorCard(s, true)); });
  }

  /* kartlar yeniden uretildi -> aktif alarmlarin yanip sonmesini geri uygula */
  window.applyAlarmClasses();
};

/* ---- KART SIRALAMA (surukle-birak) ----------------------------------------------
   Kart grafige birakilirsa seri eklenir (kopya); havuzda baska bir kartin ustune
   birakilirsa yeri degisir. Webhook havuzunun sirasi sunucuda tutulur (tum
   ekranlar ayni sirayi gorur); preset havuzu yalnizca bu ekranda siralanir. */
window.POOLDRAG = null;

window.clearDropMarks = function () {
  var els = document.querySelectorAll('#poolList .dropb, #poolList .dropa');
  for (var i = 0; i < els.length; i++) els[i].classList.remove('dropb', 'dropa');
};

window.initPoolReorder = function () {
  var list = document.getElementById('poolList');
  if (!list || list._reorder) return;
  list._reorder = true;
  var target = function (ev) {
    var d = window.POOLDRAG;
    if (!d || !ev.target || !ev.target.closest) return null;
    var el = ev.target.closest('.scard[data-sid]');
    if (!el || el.classList.contains('pre') !== d.pre) return null;   /* bolumler arasi yok */
    var r = el.getBoundingClientRect();
    return { el: el, sid: el.getAttribute('data-sid'), after: ev.clientY > r.top + r.height / 2 };
  };
  list.addEventListener('dragover', function (ev) {
    var t = target(ev);
    if (!t) return;
    ev.preventDefault();
    ev.dataTransfer.dropEffect = 'move';
    window.clearDropMarks();
    if (t.sid !== window.POOLDRAG.sid) t.el.classList.add(t.after ? 'dropa' : 'dropb');
  });
  list.addEventListener('dragleave', function (ev) {
    if (!ev.relatedTarget || !list.contains(ev.relatedTarget)) window.clearDropMarks();
  });
  list.addEventListener('drop', function (ev) {
    var t = target(ev);
    window.clearDropMarks();
    if (!t) return;
    ev.preventDefault();
    window.movePoolCard(window.POOLDRAG.sid, t.sid, t.after, window.POOLDRAG.pre);
  });
};

window.movePoolCard = function (sid, refSid, after, pre) {
  if (sid === refSid) return;
  var arr = pre ? window.PPOOL : window.POOL;
  var idx = function (id) {
    for (var i = 0; i < arr.length; i++) if (arr[i].sensor_id === id) return i;
    return -1;
  };
  var from = idx(sid);
  if (from < 0 || idx(refSid) < 0) return;
  var item = arr.splice(from, 1)[0];
  var to = idx(refSid);
  arr.splice(after ? to + 1 : to, 0, item);
  window.renderPool(true);
  if (pre) return;
  window.saveOrder(arr.map(function (s) { return s.sensor_id; }));
};

/* Webhook havuzunun sirasini sunucuya yazar (tum ekranlara yayilir) */
window.saveOrder = function (ids) {
  return fetch('/api/sensors/order?ids=' + encodeURIComponent(ids.join(',')), { method: 'POST' })
    .then(function (r) { return r.json(); })
    .then(function (j) { if (j && j.sensors) window.applyPool(j.sensors); })
    .catch(function (e) { window.showError('Sira kaydedilemedi: ' + e.message); });
};

window.poolFind = function (sensorId) {
  var all = window.allSensors();
  for (var i = 0; i < all.length; i++) {
    if (all[i].sensor_id === sensorId) return all[i];
  }
  return null;
};

window.poolUnit = function (sensorId) {
  var s = window.poolFind(sensorId);
  return s ? (s.unit || '') : '';
};

window.poolName = function (sensorId) {
  var s = window.poolFind(sensorId);
  return s ? (s.name || '') : '';
};

/* Havuzu ekrana uygula. Normalde WebSocket ile gelir; WS kopuksa HTTP yanitindan
   dogrudan uygulanir -- boylece F5 hicbir kosulda gerekmez. */
window.applyPool = function (sensors) {
  window.POOL = sensors || [];
  window.renderPool();
  window.syncChartsWithPool();
  window.applyColors();
  window.refreshLimitLines();    /* baska bir ekranda esik degistiyse cizgiler guncellensin */
  window.evalAlarms();
};

window.deleteSensor = function (sensorId) {
  fetch('/api/sensors/' + encodeURIComponent(sensorId), { method: 'DELETE' })
    .then(function (r) { return r.json(); })
    .then(function (j) { if (j && j.sensors) window.applyPool(j.sensors); })
    .catch(function (e) { window.showError('Sensor silinemedi: ' + e.message); });
};

window.clearAllSensors = function () {
  if (!confirm('WEBHOOK havuzundaki tum sensorler silinecek (tum ekranlari etkiler).\nOnayliyor musunuz?')) return;
  fetch('/api/sensors/clear', { method: 'POST' })
    .then(function (r) { return r.json(); })
    .then(function (j) { window.applyPool((j && j.sensors) || []); })
    .catch(function (e) { window.showError('Clear All basarisiz: ' + e.message); });
};

/* --- PRESET HAVUZU (yerel) --------------------------------------------------------- */
window.removePresetSensor = function (sensorId) {
  window.PPOOL = window.PPOOL.filter(function (s) { return s.sensor_id !== sensorId; });
  window.renderPool();
  window.syncChartsWithPool();
};

window.clearPresetPool = function () {
  window.PPOOL = [];
  window.renderPool();
  window.syncChartsWithPool();
};

/* Preset icindeki havuzu SADECE bu ekrana yukler (sunucuya dokunmaz) */
window.setPresetPool = function (items) {
  var num = function (v) {
    if (v === null || v === undefined || v === '') return null;
    var f = parseFloat(v);
    return isFinite(f) ? f : null;
  };
  window.PPOOL = (items || []).map(function (it) {
    var ex = String(it.expr || '').replace(/^=/, '').trim();
    return {
      sensor_id: (it.influx_name || it.sensor_id || '').trim(),
      name: it.name || '',
      unit: it.unit || '',
      amin: num(it.min !== undefined ? it.min : it.amin),
      amax: num(it.max !== undefined ? it.max : it.amax),
      expr: ex || null,
      manual: !!(it.manual || ex),
      src: 'preset'
    };
  }).filter(function (s) { return !!s.sensor_id; });
  window.renderPool();
  window.applyColors();
  window.refreshLimitLines();
  window.evalAlarms();
};

window.triggerMock = function () {
  fetch('/api/mock/trigger', { method: 'POST' })
    .then(function (r) { return r.json(); })
    .then(function (j) { if (j && j.sensors) window.applyPool(j.sensors); })
    .catch(function (e) { window.showError('Mock tetiklenemedi: ' + e.message); });
};

/* Havuz degistiginde grafiklerdeki artik gecersiz serileri temizle */
window.syncChartsWithPool = function () {
  var ids = {};
  window.allSensors().forEach(function (s) { ids[s.sensor_id] = s; });
  window.CHARTS.forEach(function (C) {
    var before = C.series.length;
    C.series = C.series.filter(function (s) { return !!ids[s.sensor_id]; });
    /* ad / birim havuzda degistiyse grafige de yansisin */
    var relabel = false;
    C.series.forEach(function (s) {
      var p = ids[s.sensor_id];
      if (!p) return;
      if ((p.name || '') !== (s.name || '') || (p.unit || '') !== (s.unit || '')) {
        s.name = p.name || '';
        s.unit = p.unit || '';
        relabel = true;
      }
    });
    if (relabel && C.series.length === before) window.renderChips(C);
    if (C.series.length !== before) {
      window.renderChips(C);
      window.buildChart(C);
      window.fetchChart(C, true);
    }
  });
};

/* ======================================================================================
   3c) FORMUL MOTORU  --  HESAP (SANAL) SENSORLER
   --------------------------------------------------------------------------------------
   Havuzdaki bir kartin "expr" alani doluysa o kart bir HESAP sensorudur: degeri
   InfluxDB'den OKUNMAZ, havuzdaki diger sensorlerin degerlerinden HESAPLANIR.

     =P1*2 + T3              -> influx alan adlariyla dogrudan islem
     =sqrt(N5) + avg(A,B)    -> fonksiyonlar
     =[YAG-SIC] * 1.8 + 32   -> adinda '-' gibi operator karakteri varsa koseli parantez
     =ma(P1, 5)              -> HAREKETLI ORTALAMA: son 5 ornegin ortalamasi
     =ma(P1*2 + T3, 10)      -> ma()'nin ilk argumani tam bir ifade olabilir

   ma() hakkinda iki nokta:
     * Pencere ORNEK sayisidir, sure degil. Grafikte bir ornek = bir GROUP BY
       kovasi (5 Dk araliginda 1s -> ma(P1,5) = 5 saniyelik ortalama), alarm
       izleyicisinde ise bir ornek = bir yoklama (refresh_ms). Ayni formul bu
       yuzden iki yerde farkli sure boyunca yumusatir.
     * Pencere dolana dek sonuc BOSTUR (grafigin ilk N-1 noktasi bos gorunur).
       Penceredeki tekil bosluklar atlanir, hepsi bossa sonuc bos kalir.

   Degerlendirme TARAYICIDA yapilir; boylece preset havuzundaki (yalnizca bu ekranda
   yasayan) kartlar da calisir ve formul degisince sunucuya sorulmadan aninda yeniden
   cizilir. Sunucu formulu yalnizca METIN olarak saklar.
   eval() KULLANILMAZ -- kendi sozcuk cozumleyici + ayristiricimiz vardir.
   ====================================================================================== */
window.FXCACHE = {};                              /* formul metni -> derlenmis sonuc */

/* --- MALZEME FONKSIYONLARI:  AD_dens(x) AD_cp(x) AD_dvis(x) AD_cond(x) -----------------
   Tablolar sunucudadir (materials.json) ve WebSocket ile her ekrana gelir.
   x = sicaklik [degC]; satirlar arasinda DOGRUSAL ara deger; aralik disi -> bos. */
window.MATERIALS = {};
window.MATPROPS = { dens: 1, cp: 2, dvis: 3, cond: 4 };   /* rows[i] icindeki sutun */
window.MATRE = /^(.+)_(dens|cp|dvis|cond)$/;

window.matFind = function (name) {
  var k, low = String(name || '').toLowerCase();
  for (k in window.MATERIALS) {
    if (Object.prototype.hasOwnProperty.call(window.MATERIALS, k) && k.toLowerCase() === low) {
      return window.MATERIALS[k];
    }
  }
  return null;
};

window.matInterp = function (rows, col, t) {
  if (t == null || !isFinite(t) || !rows || rows.length < 2) return null;
  var n = rows.length;
  if (t < rows[0][0] || t > rows[n - 1][0]) return null;
  var lo = 0, hi = n - 1, mid;
  while (hi - lo > 1) {
    mid = (lo + hi) >> 1;
    if (rows[mid][0] <= t) lo = mid; else hi = mid;
  }
  var t0 = rows[lo][0], t1 = rows[hi][0];
  if (t1 === t0) return rows[lo][col];
  return rows[lo][col] + (rows[hi][col] - rows[lo][col]) * (t - t0) / (t1 - t0);
};

/* 'su_dens' -> [1, 1, fn] (FXFUN bicimi) ya da null */
window.fxMatSpec = function (key) {
  var m = window.MATRE.exec(String(key || '').toLowerCase());
  if (!m) return null;
  var mat = window.matFind(m[1]);
  if (!mat) return null;
  var col = window.MATPROPS[m[2]];
  return [1, 1, function (a) { return window.matInterp(mat.rows, col, a[0]); }];
};

window.applyMaterials = function (mats) {
  window.MATERIALS = mats || {};
  window.FXCACHE = {};                 /* "malzeme bulunamadi" derlemeleri tazelensin */
  window.renderMatList();
  if (window.CHARTS && window.CHARTS.length) {      /* acilista grafikler henuz kurulmamis olabilir */
    window.renderPool(true);
    window.refreshAll(false);
    window.alarmWatch();
  }
};
window.FXCONST = { pi: Math.PI, e: Math.E };      /* sensor adi bunlari golgeleyebilir */
window.SIDRE   = /^[A-Za-z0-9_.-]{1,128}$/;       /* backend valid_sensor ile ayni */

/* --- Fonksiyon tablosu:  ad -> [en az, en cok (-1 = sinirsiz), govde] ------------- */
window.FXFUN = {
  sqrt:  [1,  1, function (a) { return Math.sqrt(a[0]); }],
  abs:   [1,  1, function (a) { return Math.abs(a[0]); }],
  sign:  [1,  1, function (a) { return a[0] > 0 ? 1 : (a[0] < 0 ? -1 : 0); }],
  exp:   [1,  1, function (a) { return Math.exp(a[0]); }],
  ln:    [1,  1, function (a) { return Math.log(a[0]); }],
  log:   [1,  2, function (a) {
           return a.length < 2 ? Math.log(a[0]) / Math.LN10 : Math.log(a[0]) / Math.log(a[1]); }],
  log10: [1,  1, function (a) { return Math.log(a[0]) / Math.LN10; }],
  log2:  [1,  1, function (a) { return Math.log(a[0]) / Math.LN2; }],
  pow:   [2,  2, function (a) { return Math.pow(a[0], a[1]); }],
  mod:   [2,  2, function (a) { return a[1] === 0 ? null : a[0] % a[1]; }],
  hypot: [2, -1, function (a) { var s = 0, i;
           for (i = 0; i < a.length; i++) s += a[i] * a[i]; return Math.sqrt(s); }],
  round: [1,  2, function (a) {
           var d = a.length > 1 ? Math.max(0, Math.min(10, Math.round(a[1]))) : 0;
           var m = Math.pow(10, d); return Math.round(a[0] * m) / m; }],
  floor: [1,  1, function (a) { return Math.floor(a[0]); }],
  ceil:  [1,  1, function (a) { return Math.ceil(a[0]); }],
  trunc: [1,  1, function (a) { return a[0] < 0 ? Math.ceil(a[0]) : Math.floor(a[0]); }],
  min:   [1, -1, function (a) { return Math.min.apply(null, a); }],
  max:   [1, -1, function (a) { return Math.max.apply(null, a); }],
  sum:   [1, -1, function (a) { var s = 0, i; for (i = 0; i < a.length; i++) s += a[i]; return s; }],
  avg:   [1, -1, function (a) { var s = 0, i; for (i = 0; i < a.length; i++) s += a[i];
           return s / a.length; }],
  clamp: [3,  3, function (a) { return Math.min(Math.max(a[0], a[1]), a[2]); }],
  sin:   [1,  1, function (a) { return Math.sin(a[0]); }],
  cos:   [1,  1, function (a) { return Math.cos(a[0]); }],
  tan:   [1,  1, function (a) { return Math.tan(a[0]); }],
  asin:  [1,  1, function (a) { return Math.asin(a[0]); }],
  acos:  [1,  1, function (a) { return Math.acos(a[0]); }],
  atan:  [1,  1, function (a) { return Math.atan(a[0]); }],
  atan2: [2,  2, function (a) { return Math.atan2(a[0], a[1]); }],
  deg:   [1,  1, function (a) { return a[0] * 180 / Math.PI; }],
  rad:   [1,  1, function (a) { return a[0] * Math.PI / 180; }]
};
window.FXFUN.mean = window.FXFUN.avg;
window.FXFUN["if"] = [3, 3, null];          /* tembel: govdesi fxRun icinde */
window.FXFUN.coalesce = [1, -1, null];      /* tembel: ilk BOS OLMAYAN degeri verir */
window.FXFUN.ma = [2, 2, null];             /* GECMISE BAKAN: govdesi fxRun icinde */
window.FXMA_MAX = 5000;                     /* ma() icin en buyuk pencere */

/* --- 1) Sozcuk cozumleyici -------------------------------------------------------- */
window.fxLex = function (src) {
  var toks = [], i = 0, n = src.length, c, two, st, w, end, nm;
  var dig = function (ch) { return ch >= '0' && ch <= '9'; };
  while (i < n) {
    c = src.charAt(i);
    if (c === ' ' || c === '\t' || c === '\n' || c === '\r') { i++; continue; }

    if (dig(c) || (c === '.' && dig(src.charAt(i + 1)))) {
      st = i;
      while (i < n && dig(src.charAt(i))) i++;
      if (src.charAt(i) === '.') { i++; while (i < n && dig(src.charAt(i))) i++; }
      if (src.charAt(i) === 'e' || src.charAt(i) === 'E') {
        var save = i; i++;
        if (src.charAt(i) === '+' || src.charAt(i) === '-') i++;
        if (dig(src.charAt(i))) { while (i < n && dig(src.charAt(i))) i++; } else { i = save; }
      }
      toks.push({ t: 'num', v: parseFloat(src.slice(st, i)) });
      continue;
    }

    if (c === '[') {                                  /* [ADI-BOYLE] -> her zaman sensor */
      end = src.indexOf(']', i + 1);
      if (end < 0) throw new Error('Kapanmamis koseli parantez');
      nm = src.slice(i + 1, end).trim();
      if (!nm) throw new Error('Bos koseli parantez');
      toks.push({ t: 'name', v: nm, br: true });
      i = end + 1; continue;
    }

    if (/[A-Za-z_]/.test(c)) {
      st = i;
      while (i < n && /[A-Za-z0-9_.]/.test(src.charAt(i))) i++;
      w = src.slice(st, i);
      while (w.length > 1 && w.charAt(w.length - 1) === '.') { w = w.slice(0, -1); i--; }
      toks.push({ t: 'name', v: w, br: false });
      continue;
    }

    two = src.substr(i, 2);
    if (two === '**') { toks.push({ t: 'op', v: '^' }); i += 2; continue; }
    if (two === '<=' || two === '>=' || two === '==' || two === '!=') {
      toks.push({ t: 'op', v: two }); i += 2; continue;
    }
    if ('+-*/%^(),<>'.indexOf(c) >= 0) { toks.push({ t: 'op', v: c }); i++; continue; }
    if (c === '=') { toks.push({ t: 'op', v: '==' }); i++; continue; }
    throw new Error("Taninmayan karakter: '" + c + "'");
  }
  return toks;
};

/* --- 2) Ayristirici (ozyinelemeli inis) ------------------------------------------- */
window.fxParse = function (src) {
  var toks = window.fxLex(src), pos = 0;
  var peek = function () { return toks[pos] || null; };
  var eat = function (v) {
    var t = toks[pos];
    if (t && t.t === 'op' && t.v === v) { pos++; return true; }
    return false;
  };
  var need = function (v) { if (!eat(v)) throw new Error("'" + v + "' bekleniyordu"); };

  var pExpr = function () { return pCmp(); };

  var pCmp = function () {
    var a = pAdd(), t = peek();
    while (t && t.t === 'op' && ['<', '>', '<=', '>=', '==', '!='].indexOf(t.v) >= 0) {
      pos++; a = { t: 'b', op: t.v, a: a, b: pAdd() }; t = peek();
    }
    return a;
  };
  var pAdd = function () {
    var a = pMul(), t = peek();
    while (t && t.t === 'op' && (t.v === '+' || t.v === '-')) {
      pos++; a = { t: 'b', op: t.v, a: a, b: pMul() }; t = peek();
    }
    return a;
  };
  var pMul = function () {
    var a = pUn(), t = peek();
    while (t && t.t === 'op' && (t.v === '*' || t.v === '/' || t.v === '%')) {
      pos++; a = { t: 'b', op: t.v, a: a, b: pUn() }; t = peek();
    }
    return a;
  };
  var pUn = function () {
    var t = peek();
    if (t && t.t === 'op' && (t.v === '-' || t.v === '+')) {
      pos++; return { t: 'u', op: t.v, a: pUn() };
    }
    return pPow();
  };
  var pPow = function () {
    var a = pAtom();
    if (eat('^')) return { t: 'b', op: '^', a: a, b: pUn() };   /* saga baglanir */
    return a;
  };
  var pAtom = function () {
    var t = peek(), key, args, spec, e;
    if (!t) throw new Error('Formul yarim kaldi');
    if (t.t === 'num') { pos++; return { t: 'n', v: t.v }; }
    if (t.t === 'name') {
      pos++;
      var nxt = peek();
      if (!t.br && nxt && nxt.t === 'op' && nxt.v === '(') {
        key = t.v.toLowerCase();
        spec = window.FXFUN[key] || window.fxMatSpec(key);
        if (!spec) {
          if (window.MATRE.test(key)) {
            throw new Error("Malzeme bulunamadi: '" + t.v.replace(window.MATRE, '$1') +
                            "' (Malzemeler penceresinden ekleyin)");
          }
          throw new Error("Bilinmeyen fonksiyon: '" + t.v + "'");
        }
        pos++; args = [];
        if (!eat(')')) {
          do { args.push(pExpr()); } while (eat(','));
          need(')');
        }
        if (args.length < spec[0] || (spec[1] >= 0 && args.length > spec[1])) {
          throw new Error(key + '() icin arguman sayisi hatali');
        }
        if (key === 'ma') {
          /* Pencere SABIT olmali: her noktada degisen bir pencere ne grafikte
             ne de alarmda anlamli olurdu; hatayi kullaniciya burada soyleriz. */
          if (!args[1] || args[1].t !== 'n' || !(args[1].v >= 1) || args[1].v > window.FXMA_MAX) {
            throw new Error('ma() penceresi 1 ile ' + window.FXMA_MAX +
                            ' arasi SABIT bir sayi olmali (ornek: ma(P1, 5))');
          }
          args[1] = { t: 'n', v: Math.floor(args[1].v) };
        }
        return { t: 'f', f: key, args: args };
      }
      return { t: 'r', id: t.v, br: !!t.br };
    }
    if (t.t === 'op' && t.v === '(') { pos++; e = pExpr(); need(')'); return e; }
    throw new Error("Beklenmeyen: '" + t.v + "'");
  };

  var ast = pExpr();
  if (pos < toks.length) throw new Error("Fazladan girdi: '" + toks[pos].v + "'");
  return ast;
};

/* --- 3) Bagimlilik cikarimi -------------------------------------------------------- */
window.fxDeps = function (nd, out) {
  out = out || [];
  if (!nd) return out;
  if (nd.t === 'r') { if (out.indexOf(nd.id) < 0) out.push(nd.id); return out; }
  if (nd.t === 'u') return window.fxDeps(nd.a, out);
  if (nd.t === 'b') { window.fxDeps(nd.a, out); return window.fxDeps(nd.b, out); }
  if (nd.t === 'f') { nd.args.forEach(function (x) { window.fxDeps(x, out); }); return out; }
  return out;
};

/* --- 4) Derleme (onbellekli) ------------------------------------------------------- */
window.fxCompile = function (src) {
  var key = String(src == null ? '' : src);
  if (window.FXCACHE[key]) return window.FXCACHE[key];
  var out;
  try {
    var ast = window.fxParse(key);
    out = { ast: ast, deps: window.fxDeps(ast, []), err: null };
  } catch (e) {
    out = { ast: null, deps: [], err: (e && e.message) ? e.message : String(e) };
  }
  window.FXCACHE[key] = out;
  return out;
};

/* --- 5) Degerlendirme --------------------------------------------------------------
   BOS (null) yayilir: bir bagimlilik o anda okunamadiysa sonuc da bostur, boylece
   grafikte yanlis sayi degil BOSLUK gorunur.  0'a bolme ve NaN de bosa dusurulur. */
window.fxRun = function (ast, get, ctx) {
  var ev = function (nd) {
    var i, v, x, y, r, vals, w, cur, sum, cnt, spec;
    switch (nd.t) {
      case 'n': return nd.v;
      case 'r':
        v = get(nd.id);
        if (v === undefined) {
          if (!nd.br && Object.prototype.hasOwnProperty.call(window.FXCONST, nd.id)) {
            return window.FXCONST[nd.id];
          }
          return null;
        }
        return (v == null || !isFinite(v)) ? null : v;
      case 'u':
        x = ev(nd.a);
        if (x == null) return null;
        return nd.op === '-' ? -x : x;
      case 'b':
        x = ev(nd.a); if (x == null) return null;
        y = ev(nd.b); if (y == null) return null;
        switch (nd.op) {
          case '+':  r = x + y; break;
          case '-':  r = x - y; break;
          case '*':  r = x * y; break;
          case '/':  if (y === 0) return null; r = x / y; break;
          case '%':  if (y === 0) return null; r = x % y; break;
          case '^':  r = Math.pow(x, y); break;
          case '<':  return x <  y ? 1 : 0;
          case '>':  return x >  y ? 1 : 0;
          case '<=': return x <= y ? 1 : 0;
          case '>=': return x >= y ? 1 : 0;
          case '==': return x === y ? 1 : 0;
          case '!=': return x !== y ? 1 : 0;
          default:   return null;
        }
        return isFinite(r) ? r : null;
      case 'f':
        if (nd.f === 'if') {                       /* tembel: yalnizca secilen dal */
          x = ev(nd.args[0]);
          if (x == null) return null;
          return ev(nd.args[x !== 0 ? 1 : 2]);
        }
        if (nd.f === 'coalesce') {                 /* tembel: ilk dolu deger */
          for (i = 0; i < nd.args.length; i++) {
            v = ev(nd.args[i]);
            if (v != null) return v;
          }
          return null;
        }
        if (nd.f === 'ma') {                       /* GECMISE BAKAN: hareketli ortalama */
          /* ma(ifade, N) = ifadenin SON N ornegindeki ortalamasi.
             ctx yoksa (ornegin kart onizlemesi) gecmis de yoktur -> bos. */
          if (!ctx) return null;
          w = nd.args[1].v | 0;                    /* derlemede sabit oldugu dogrulandi */
          cur = ctx.index();
          if (cur + 1 < w) return null;            /* pencere henuz dolmadi */
          sum = 0; cnt = 0;
          for (i = cur - w + 1; i <= cur; i++) {
            v = ctx.seek(i, ev, nd.args[0]);
            if (v != null) { sum += v; cnt++; }
          }
          /* penceredeki BOS ornekler atlanir: tek bir veri boslugu N noktalik
             ortalamayi topyekun silmesin. Hicbiri okunamadiysa sonuc bostur. */
          return cnt ? sum / cnt : null;
        }
        vals = [];
        for (i = 0; i < nd.args.length; i++) {
          v = ev(nd.args[i]);
          if (v == null) return null;
          vals.push(v);
        }
        spec = window.FXFUN[nd.f] || window.fxMatSpec(nd.f);
        if (!spec) return null;                  /* malzeme sonradan silindi */
        r = spec[2](vals);
        return (r == null || !isFinite(r)) ? null : r;
    }
    return null;
  };
  try { return ev(ast); } catch (e) { return null; }
};

/* --- 6) Havuz baglantisi ----------------------------------------------------------- */
window.exprOf = function (sid) {
  var s = window.poolFind(sid);
  var e = s && s.expr;
  return (typeof e === 'string' && e.trim()) ? e.trim() : null;
};
window.isCalc = function (sid) { return !!window.exprOf(sid); };
window.isManual = function (sid) {
  var s = window.poolFind(sid);
  return !!(s && (s.manual || (typeof s.expr === 'string' && s.expr.trim())));
};
window.validSid = function (txt) { return window.SIDRE.test(String(txt || '')); };
window.fxNum = function (v) {
  if (v == null || !isFinite(v)) return '-';
  var a = Math.abs(v);
  if (a !== 0 && (a < 0.001 || a >= 1e9)) return v.toExponential(3);
  return String(Math.round(v * 1000) / 1000);
};

/* Bir id'den baslayip hedefe ULASILIYOR mu (dongu tespiti icin) */
window.fxReaches = function (from, target, seen) {
  if (from === target) return true;
  seen = seen || {};
  if (seen[from]) return false;
  seen[from] = 1;
  var ex = window.exprOf(from);
  if (!ex) return false;
  var c = window.fxCompile(ex);
  if (c.err) return false;
  for (var i = 0; i < c.deps.length; i++) {
    if (window.fxReaches(c.deps[i], target, seen)) return true;
  }
  return false;
};
window.fxWouldCycle = function (id, deps) {
  for (var i = 0; i < (deps || []).length; i++) {
    if (window.fxReaches(deps[i], id, {})) return true;
  }
  return false;
};

/* Istenen id listesi icin:  sunucudan CEKILECEK yaprak id'ler + HESAP SIRASI.
   Hesap sensorleri ic ice olabilir; siralama topolojiktir (once bagimliliklar). */
window.fxPlan = function (ids) {
  var leaf = [], order = [], mark = {}, err = null;
  var visit = function (id, stack) {
    if (mark[id] === 2) return;
    if (mark[id] === 1) {
      err = err || ('Dongusel formul: ' + stack.concat(id).join(' -> '));
      mark[id] = 2; return;
    }
    var ex = window.exprOf(id);
    if (!ex) {
      if (leaf.indexOf(id) < 0) leaf.push(id);
      mark[id] = 2; return;
    }
    var c = window.fxCompile(ex);
    if (c.err) { err = err || (id + ': ' + c.err); mark[id] = 2; return; }
    mark[id] = 1;
    c.deps.forEach(function (d) {
      /* pi / e gibi sabitler havuzda bir sensorle golgelenmediyse cekilmez */
      if (Object.prototype.hasOwnProperty.call(window.FXCONST, d) && !window.poolFind(d)) return;
      visit(d, stack.concat(id));
    });
    mark[id] = 2;
    if (order.indexOf(id) < 0) order.push(id);
  };
  (ids || []).forEach(function (id) { visit(id, []); });
  return { fetch: leaf, order: order, err: err };
};

/* /api/data yanitina hesap serilerini EKLER (yerinde) */
window.fxFillSeries = function (j, order) {
  if (!order || !order.length) return;
  var t = (j && j.t) || [], n = t.length;
  j.series = j.series || {};
  order.forEach(function (id) {
    var ex = window.exprOf(id);
    var c = ex ? window.fxCompile(ex) : null;
    var arr = new Array(n), k;
    if (!c || c.err || !c.ast) {
      for (k = 0; k < n; k++) arr[k] = null;
      j.series[id] = arr; return;
    }
    var at = 0;
    var get = function (name) {
      var s = j.series[name];
      if (!s) return undefined;
      var v = s[at];
      return v === undefined ? null : v;
    };
    /* Gecmise bakan fonksiyonlar (ma) icin: degerlendirmeyi gecici olarak daha
       eski bir zaman noktasina tasir, sonra yerine birakir. Boylece ma()'nin
       ilk argumani ISTEDIGI KADAR karmasik olabilir -- ma(P1*2 + T3, 5) gibi. */
    var ctx = {
      index: function () { return at; },
      seek: function (i, ev, node) {
        var save = at;
        at = i;
        try { return ev(node); } finally { at = save; }
      }
    };
    for (at = 0; at < n; at++) arr[at] = window.fxRun(c.ast, get, ctx);
    j.series[id] = arr;
  });
};

/* --- CANLI GECMIS: ma() alarm izleyicisinde de calissin diye ------------------------
   Grafikte gecmis zaten elimizdedir (seri dizisi); canli yolda ise yalnizca SON
   deger gelir. Bu yuzden her yoklamanin sonucunu kucuk bir tamponda saklariz.
   DIKKAT: buradaki pencere YOKLAMA sayisidir (refresh_ms), grafikteki ise GROUP BY
   kovasi sayisidir -- ayni formul iki yerde farkli sure boyunca yumusatir. */
window.FXSNAP     = [];        /* [{ts, v:{sid:deger}}]  en YENI sonda */
window.FXSNAP_MAX = 1200;      /* en fazla kac yoklama saklanir */
window.FXSNAP_TTL = 600000;    /* 10 dk'dan eski goruntuler atilir (duraklatma boslugu) */

window.fxSnapPush = function (values) {
  var now = Date.now(), snap = {}, k;
  for (k in values) {
    if (Object.prototype.hasOwnProperty.call(values, k)) snap[k] = values[k];
  }
  window.FXSNAP.push({ ts: now, v: snap });
  /* Uzun duraklatmalardan sonraki eski ornekler ortalamayi kirletmesin */
  while (window.FXSNAP.length && (now - window.FXSNAP[0].ts) > window.FXSNAP_TTL) {
    window.FXSNAP.shift();
  }
  while (window.FXSNAP.length > window.FXSNAP_MAX) window.FXSNAP.shift();
};

/* /api/last yanitina hesap sensorlerinin SON degerini ekler (yerinde) */
window.fxFillValues = function (values, order) {
  var snaps = window.FXSNAP;
  var cur = snaps.length;                 /* simdiki an = tamponun bir sonrasi */
  var get = function (name) {
    var src = (cur >= snaps.length) ? values : (snaps[cur] && snaps[cur].v);
    if (!src) return undefined;
    return Object.prototype.hasOwnProperty.call(src, name) ? src[name] : undefined;
  };
  var ctx = {
    index: function () { return cur; },
    seek: function (i, ev, node) {
      var save = cur;
      cur = i;
      try { return ev(node); } finally { cur = save; }
    }
  };
  (order || []).forEach(function (id) {
    var ex = window.exprOf(id);
    var c = ex ? window.fxCompile(ex) : null;
    if (!c || c.err || !c.ast) { values[id] = null; return; }
    values[id] = window.fxRun(c.ast, get, ctx);
  });
  /* Hesap sonuclari da dahil TAM goruntuyu sakla: ic ice ma() boylece calisir */
  window.fxSnapPush(values);
};

/* Formulde gecen ama havuzda KART OLMAYAN adlar: InfluxDB alani sayilir ve dogrudan
   okunur (fxPlan bunlari yaprak olarak ceker). Kart eklemek GEREKMEZ. */
window.fxExternal = function (deps) {
  return (deps || []).filter(function (d) {
    return !window.poolFind(d) && !Object.prototype.hasOwnProperty.call(window.FXCONST, d);
  });
};
/* Havuzda olmayan ve InfluxDB alan adi olarak da gecersiz adlar (ornek: [A B]) */
window.fxBadNames = function (deps) {
  return window.fxExternal(deps).filter(function (d) { return !window.validSid(d); });
};

/* Formul metninde bir degisken adini (oldId) yenisiyle degistirir. Sozcuk
   cozumleyiciyle ayni kurallar: fonksiyon adlarina, sayilara ve baska adlarin
   PARCALARINA dokunulmaz; [KOSELI] yazimi da yakalanir. */
window.fxRename = function (src, oldId, newId) {
  src = String(src || '');
  var out = '', i = 0, n = src.length, c, st, w, end, j;
  var dig = function (ch) { return ch >= '0' && ch <= '9'; };
  var plain = /^[A-Za-z_][A-Za-z0-9_]*$/.test(newId);
  while (i < n) {
    c = src.charAt(i);
    if (dig(c) || (c === '.' && dig(src.charAt(i + 1)))) {
      st = i;
      while (i < n && dig(src.charAt(i))) i++;
      if (src.charAt(i) === '.') { i++; while (i < n && dig(src.charAt(i))) i++; }
      if (src.charAt(i) === 'e' || src.charAt(i) === 'E') {
        var save = i; i++;
        if (src.charAt(i) === '+' || src.charAt(i) === '-') i++;
        if (dig(src.charAt(i))) { while (i < n && dig(src.charAt(i))) i++; } else { i = save; }
      }
      out += src.slice(st, i);
      continue;
    }
    if (c === '[') {
      end = src.indexOf(']', i + 1);
      if (end < 0) { out += src.slice(i); break; }
      out += (src.slice(i + 1, end).trim() === oldId) ? ('[' + newId + ']') : src.slice(i, end + 1);
      i = end + 1;
      continue;
    }
    if (/[A-Za-z_]/.test(c)) {
      st = i;
      while (i < n && /[A-Za-z0-9_.]/.test(src.charAt(i))) i++;
      w = src.slice(st, i);
      while (w.length > 1 && w.charAt(w.length - 1) === '.') { w = w.slice(0, -1); i--; }
      j = i;
      while (j < n && /\s/.test(src.charAt(j))) j++;
      var isCall = src.charAt(j) === '(';
      out += (w === oldId && !isCall) ? (plain ? newId : '[' + newId + ']') : w;
      continue;
    }
    out += c;
    i++;
  }
  return out;
};

/* Kart altinda gosterilecek durum satiri */
window.fxStatus = function (sid) {
  var ex = window.exprOf(sid);
  if (!ex) return { txt: '', cls: '' };
  var c = window.fxCompile(ex);
  if (c.err) return { txt: '! ' + c.err, cls: 'bad' };
  var bad = window.fxBadNames(c.deps);
  if (bad.length) return { txt: '! gecersiz ad: ' + bad.join(', '), cls: 'bad' };
  if (window.fxWouldCycle(sid, c.deps)) return { txt: '! dongusel formul', cls: 'bad' };
  var v = window.LASTVAL[sid];
  if (v == null || !isFinite(v)) return { txt: sid + ' = (veri bekleniyor)', cls: '' };
  return { txt: sid + ' = ' + window.fxNum(v), cls: 'ok' };
};

/* ======================================================================================
   3d) ALARM / ESIK SISTEMI
   --------------------------------------------------------------------------------------
   * Esikler sensore aittir (grafige degil): webhook sensorlerinde SUNUCUDA saklanir
     ve tum ekranlara yayilir; preset havuzu sensorlerinde yalnizca bu ekranda kalir.
   * Izleme grafiklerden BAGIMSIZDIR: /api/last ile esigi olan her sensor duzenli
     yoklanir, boylece grafikte olmayan veya PAUSE edilmis sensorler de korunur.
   ====================================================================================== */
window.ALARM     = {};    /* sensor_id -> true  (su an ALARM gosteriliyor) */
window.ALARM_TO  = {};    /* sensor_id -> alarmin en erken susabilecegi an (ms)   */
window.LASTVAL   = {};    /* sensor_id -> son okunan deger */
window.MUTED     = false;
window.ACTX      = null;
window.ALTIMER   = null;
window.ALSIG     = '';    /* ekrana uygulanmis alarm kumesi -- gereksiz DOM dokunusunu onler */
window.ALARM_HOLD_MS = 3000;   /* esik asildiginda alarm EN AZ bu kadar surer */
window.ALARM_VOL     = 0.55;   /* 0..1  (Ayarlar penceresinden degistirilebilir) */

/* Bir sensorun gecerli esikleri (webhook kaydi veya preset kaydi fark etmez) */
window.limitsOf = function (sensorId) {
  var s = window.poolFind(sensorId);
  if (!s) return { min: null, max: null };
  var lo = (s.amin === null || s.amin === undefined || s.amin === '') ? null : parseFloat(s.amin);
  var hi = (s.amax === null || s.amax === undefined || s.amax === '') ? null : parseFloat(s.amax);
  return {
    min: (lo == null || !isFinite(lo)) ? null : lo,
    max: (hi == null || !isFinite(hi)) ? null : hi
  };
};

window.hasLimits = function (sensorId) {
  var l = window.limitsOf(sensorId);
  return l.min != null || l.max != null;
};

/* Esik yaz: webhook sensoru -> SUNUCU (tum ekranlar), preset sensoru -> yalnizca bu ekran */
window.setLimit = function (sensorId, side, raw) {
  var txt = (raw == null) ? '' : String(raw).trim();
  var val = (txt === '') ? null : parseFloat(txt.replace(',', '.'));
  if (val != null && !isFinite(val)) val = null;
  var key = (side === 'min') ? 'amin' : 'amax';

  if (window.isPresetOnly(sensorId)) {
    window.PPOOL.forEach(function (s) { if (s.sensor_id === sensorId) s[key] = val; });
    window.renderPool();
    window.refreshLimitLines();
    window.evalAlarms();
    return;
  }

  var qs = (side === 'min' ? 'min=' : 'max=') + (val == null ? '' : encodeURIComponent(val));
  fetch('/api/sensors/' + encodeURIComponent(sensorId) + '/limits?' + qs, { method: 'POST' })
    .then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(function (j) { if (j && j.sensors) window.applyPool(j.sensors); })
    .catch(function (e) { window.showError('Esik kaydedilemedi: ' + e.message); });
};

/* Bir degerin esik disinda olup olmadigi */
window.isOut = function (sensorId, v) {
  if (v == null || !isFinite(v)) return false;
  var l = window.limitsOf(sensorId);
  if (l.min != null && v < l.min) return true;
  if (l.max != null && v > l.max) return true;
  return false;
};

/* Son degerleri esiklerle karsilastirir.
   TUTMA (latch): esik bir kez asildiginda alarm EN AZ ALARM_HOLD_MS surer.
   Deger hemen normale donse bile alarm erken sonmez -- anlik sicramalar
   gozden kacmaz. Her sensorun kendi sayaci vardir; biri sonerken digerini
   etkilemez. */
window.evalAlarms = function () {
  var now = Date.now(), live = {};

  window.allSensors().forEach(function (s) { live[s.sensor_id] = true; });
  /* havuzdan silinen sensorun alarmi takili kalmasin */
  Object.keys(window.ALARM_TO).forEach(function (sid) {
    if (!live[sid]) delete window.ALARM_TO[sid];
  });

  window.allSensors().forEach(function (s) {
    var sid = s.sensor_id;
    if (!window.hasLimits(sid)) { delete window.ALARM_TO[sid]; return; }
    /* esik disindaysa sayaci HER seferinde tazele -> deger disarida kaldigi
       surece alarm devam eder, normale donunce 3 sn daha surer */
    if (window.isOut(sid, window.LASTVAL[sid])) {
      window.ALARM_TO[sid] = now + window.ALARM_HOLD_MS;
    }
  });

  window.alarmTick();
};

/* Sayaclari suzer ve ekrani gunceller. Veri CEKMEZ; sik ve ucuzdur. */
window.alarmTick = function () {
  var now = Date.now(), active = {}, ids = [];

  Object.keys(window.ALARM_TO).forEach(function (sid) {
    if (window.ALARM_TO[sid] > now) { active[sid] = true; ids.push(sid); }
    else delete window.ALARM_TO[sid];            /* tutma suresi doldu */
  });

  window.ALARM = active;

  /* Alarm kumesi degismediyse DOM'a HIC dokunma: boylece hali hazirda
     yanip sonen kartlarin animasyon fazi SIFIRLANMAZ (her sensor kendi
     ritminde yanar, birbirini etkilemez). */
  var sig = ids.sort().join(',');
  if (sig === window.ALSIG) {
    if (ids.length) window.startBeep(); else window.stopBeep();
    return;
  }
  window.ALSIG = sig;

  window.applyAlarmClasses();

  var names = ids.map(function (sid) {
    var s = window.poolFind(sid);
    return (s && (s.name || '').trim()) || sid;
  });
  var bar = document.getElementById('albar');
  var txt = document.getElementById('altxt');
  if (bar) bar.className = names.length ? 'on' : '';
  if (txt) txt.textContent = names.length
    ? (names.length === 1 ? names[0] : names.length + ' sensor esik disi')
    : '';
};

/* Havuz kartlari + ilgili grafik cerceveleri.
   classList.toggle zaten var olan sinifi yeniden EKLEMEZ, bu yuzden
   devam eden yanip sonme kesintiye ugramaz. */
window.applyAlarmClasses = function () {
  var any = false, k;
  for (k in window.ALARM) { if (window.ALARM[k]) { any = true; break; } }

  var cards = document.querySelectorAll('#poolList .scard');
  for (var i = 0; i < cards.length; i++) {
    var sid = cards[i].getAttribute('data-sid');
    cards[i].classList.toggle('alarm', !!window.ALARM[sid]);
  }

  window.CHARTS.forEach(function (C) {
    if (!C) return;
    var el = document.getElementById('card' + C.i);
    if (!el) return;
    var hit = C.series.some(function (s) { return !!window.ALARM[s.sensor_id]; });
    el.classList.toggle('alarm', hit);
  });

  if (any) window.startBeep(); else window.stopBeep();
};

/* --- SESLI IKAZ ------------------------------------------------------------------- */
window.audioCtx = function () {
  if (window.ACTX) return window.ACTX;
  var AC = window.AudioContext || window.webkitAudioContext;
  if (!AC) return null;
  try { window.ACTX = new AC(); } catch (e) { return null; }
  return window.ACTX;
};

/* UC DARBELI IKI TONLU SIREN -- fabrika gurultusunde bile fark edilir.
   Kare dalga + hafif detune ikinci osilator sert/tiz bir tini verir;
   yuksek frekanslar (1050/1480 Hz) kulagin en duyarli oldugu banttadir. */
window.beep = function () {
  if (window.MUTED) return;
  var ctx = window.audioCtx();
  if (!ctx) return;
  if (ctx.state === 'suspended') { try { ctx.resume(); } catch (e) {} }
  try {
    var vol = Math.max(0, Math.min(1, window.ALARM_VOL));
    if (vol <= 0) return;

    var master = ctx.createGain();
    master.gain.value = vol;
    master.connect(ctx.destination);

    var PULSES = [{ f: 1046, d: 0.14 }, { f: 1480, d: 0.14 }, { f: 1046, d: 0.16 }];
    var t = ctx.currentTime + 0.02;

    PULSES.forEach(function (p) {
      [0, 9].forEach(function (detune) {          /* 2 osilator -> daha sert tini */
        var o = ctx.createOscillator(), g = ctx.createGain();
        o.type = 'square';
        o.frequency.value = p.f + detune;
        g.gain.setValueAtTime(0.0001, t);
        g.gain.exponentialRampToValueAtTime(0.85, t + 0.006);
        g.gain.setValueAtTime(0.85, t + p.d - 0.015);
        g.gain.exponentialRampToValueAtTime(0.0001, t + p.d);
        o.connect(g); g.connect(master);
        o.start(t);
        o.stop(t + p.d + 0.02);
      });
      t += p.d + 0.05;                            /* darbeler arasi kisa sessizlik */
    });
  } catch (e) {}
};

window.startBeep = function () {
  if (window.ALTIMER) return;
  window.beep();
  window.ALTIMER = setInterval(window.beep, 1000);   /* israrli tekrar */
};

window.stopBeep = function () {
  if (!window.ALTIMER) return;
  clearInterval(window.ALTIMER);
  window.ALTIMER = null;
};

window.toggleMute = function () {
  window.MUTED = !window.MUTED;
  try { localStorage.setItem('tbm_muted', window.MUTED ? '1' : '0'); } catch (e) {}
  window.syncMuteBtn();
};

window.syncMuteBtn = function () {
  var b = document.getElementById('muteBtn');
  if (!b) return;
  b.className = 'btn sm mute' + (window.MUTED ? ' on' : '');
  b.innerHTML = window.MUTED ? '&#128263;' : '&#128266;';
  b.title = window.MUTED ? 'Sesli ikaz KAPALI -- acmak icin tiklayin'
                         : 'Sesli ikaz ACIK -- susturmak icin tiklayin';
};

/* --- IZLEYICI: esigi olan TUM sensorlerin son degeri (grafikten bagimsiz) ----------- */
window.alarmWatch = function () {
  /* Esigi olanlar + TUM hesap kartlari (hesap kartinin kendi okumasi yoktur,
     kartin altindaki canli sonuc satiri bu yoklamadan beslenir). */
  var ids = window.allSensors().filter(function (s) {
    return window.hasLimits(s.sensor_id) || window.isCalc(s.sensor_id);
  }).map(function (s) { return s.sensor_id; });
  if (!ids.length) { window.evalAlarms(); return; }

  var plan = window.fxPlan(ids);
  var done = function (values) {
    window.fxFillValues(values, plan.order);        /* hesap sensorlerini uret */
    Object.keys(values).forEach(function (k) { window.LASTVAL[k] = values[k]; });
    window.evalAlarms();
    window.syncLimitInputs();                       /* fx sonuc satirlarini tazele */
  };

  if (!plan.fetch.length) { done({}); return; }

  fetch('/api/last?sensors=' + encodeURIComponent(plan.fetch.join(',')))
    .then(function (r) { return r.json(); })
    .then(function (j) { done((j && j.values) || {}); })
    .catch(function () { /* izleyici sessizdir: DB hatasi zaten durum cubugunda */ });
};

/* Esik cizgileri degistiginde tum grafikleri yeniden ciz */
window.refreshLimitLines = function () {
  window.CHARTS.forEach(function (C) {
    if (C && C.u && typeof C.u.redraw === 'function') { try { C.u.redraw(); } catch (e) {} }
  });
};

/* Grafige yatay esik cizgileri (seri rengiyle, kesikli) */
window.drawLimits = function (C, u) {
  if (!u || !u.ctx || !u.bbox) return;
  var lines = [];
  C.series.forEach(function (s) {
    var l = window.limitsOf(s.sensor_id);
    if (l.min == null && l.max == null) return;
    var col = window.colorFor(s.sensor_id);
    if (l.min != null) lines.push({ v: l.min, k: s.axis, c: col });
    if (l.max != null) lines.push({ v: l.max, k: s.axis, c: col });
  });
  if (!lines.length) return;

  var ctx = u.ctx, bb = u.bbox, d = window.devicePixelRatio || 1;
  ctx.save();
  try {
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.beginPath();
    ctx.rect(bb.left, bb.top, bb.width, bb.height);
    ctx.clip();
    ctx.lineWidth = Math.max(1, Math.round(1.3 * d));
    ctx.setLineDash([Math.round(8 * d), Math.round(5 * d)]);
    ctx.globalAlpha = 0.9;
    lines.forEach(function (L) {
      var y = u.valToPos(L.v, L.k, true);
      if (y == null || !isFinite(y)) return;
      if (y < bb.top - 1 || y > bb.top + bb.height + 1) return;
      ctx.strokeStyle = L.c;
      ctx.beginPath();
      ctx.moveTo(bb.left, y);
      ctx.lineTo(bb.left + bb.width, y);
      ctx.stroke();
    });
  } catch (e) {}
  ctx.setLineDash([]);
  ctx.globalAlpha = 1;
  ctx.restore();
};

/* ======================================================================================
   3d) CSV / PNG DISA AKTARIM  (her grafik icin ayri)
   ====================================================================================== */
window.downloadBlob = function (blob, filename) {
  var url = URL.createObjectURL(blob);
  var a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  setTimeout(function () {
    try { document.body.removeChild(a); } catch (e) {}
    URL.revokeObjectURL(url);
  }, 0);
};

window.stampName = function (chartIdx, ext) {
  var d = new Date();
  return 'grafik' + (chartIdx + 1) + '_' +
    d.getFullYear() + window.pad(d.getMonth() + 1, 2) + window.pad(d.getDate(), 2) + '_' +
    window.pad(d.getHours(), 2) + window.pad(d.getMinutes(), 2) + window.pad(d.getSeconds(), 2) +
    '.' + ext;
};

/* Ekranda GORUNEN X araligindaki noktalari CSV'ye yazar.
   Ayrac ';' ve ondalik ',' -- Turkce Excel dogrudan acar. */
window.exportCsv = function (chartIdx) {
  var C = window.CHARTS[chartIdx];
  if (!C || !C.u || !C.series.length) { window.showError('Bu grafikte seri yok.'); return; }
  var u = C.u, xs = u.data[0] || [];
  if (!xs.length) { window.showError('Indirilecek veri yok.'); return; }

  var xmin = u.scales.x.min, xmax = u.scales.x.max;
  /* Baslik: "Arayuz Adi (INFLUXDB_ADI) [Birim]" -- InfluxDB alan adi
     her zaman parantez icinde yer alir, boylece sutun hangi olcume ait
     oldugu sonradan da kesin olarak anlasilir. */
  var head = ['Zaman', 'Epoch'];
  C.series.forEach(function (s) {
    var nm = (s.name || '').trim();
    var ex = window.exprOf(s.sensor_id);
    var col = nm ? nm + ' (' + s.sensor_id + ')' : s.sensor_id;
    if (ex) col += ' {=' + ex + '}';      /* hesap sutununda formul de yazili kalsin */
    head.push(col + (s.unit ? ' [' + s.unit + ']' : ''));
  });

  var rows = [head.join(';')], n = 0;
  for (var i = 0; i < xs.length; i++) {
    if (xmin != null && xmax != null && (xs[i] < xmin || xs[i] > xmax)) continue;
    var cells = [window.fmtDateTimeMs(xs[i]), String(xs[i]).replace('.', ',')];
    for (var k = 0; k < C.series.length; k++) {
      var arr = u.data[k + 1] || [];
      var v = arr[i];
      cells.push((v == null || !isFinite(v)) ? '' : String(v).replace('.', ','));
    }
    rows.push(cells.join(';'));
    n++;
  }
  if (!n) { window.showError('Gorunen aralikta veri yok.'); return; }

  /* BOM: Excel'in UTF-8'i dogru okumasi icin */
  var blob = new Blob(['﻿' + rows.join('\r\n') + '\r\n'],
                      { type: 'text/csv;charset=utf-8;' });
  window.downloadBlob(blob, window.stampName(chartIdx, 'csv'));
};

/* Grafik KARTININ TAMAMINI (baslik, seri etiketleri, zaman/GROUP BY ayarlari,
   imlec satiri ve cizim alani) bir goruntuye cevirir.
   Yontem: kartin kopyasi bir SVG <foreignObject> icine gomulur; sayfanin kendi
   CSS'i de icine alinir, boylece DIS BAGIMLILIK GEREKMEZ. Canvas serilestirilemedigi
   icin once PNG'ye, form ogeleri de serilestirmede bos ciktigi icin metin
   kutucuklarina cevrilir. */
window.cardToImage = function (card, done) {
  var rect = card.getBoundingClientRect();
  var W = Math.max(1, Math.round(rect.width));
  var H = Math.max(1, Math.round(rect.height));

  var clone = card.cloneNode(true);
  clone.classList.remove('alarm', 'dragover', 'full');
  clone.setAttribute('style', 'width:' + W + 'px;height:' + H + 'px;margin:0;display:flex;' +
                              'flex-direction:column;overflow:hidden');

  var i, j;

  /* 1) <canvas> -> <img>  (canvas icerigi XML'e serilestirilemez) */
  var sc = card.querySelectorAll('canvas'), dc = clone.querySelectorAll('canvas');
  for (i = 0; i < dc.length && i < sc.length; i++) {
    var cr = sc[i].getBoundingClientRect();
    var cw = Math.round(cr.width), ch = Math.round(cr.height);
    var img = document.createElement('img');
    img.setAttribute('width', cw);
    img.setAttribute('height', ch);
    img.setAttribute('style', 'display:block;width:' + cw + 'px;height:' + ch + 'px');
    try { img.setAttribute('src', sc[i].toDataURL('image/png')); } catch (e) {}
    dc[i].parentNode.replaceChild(img, dc[i]);
  }

  /* 2) <select> -> secili metni gosteren kutucuk */
  var ss = card.querySelectorAll('select'), ds = clone.querySelectorAll('select');
  for (i = 0; i < ds.length && i < ss.length; i++) {
    var sp = document.createElement('span');
    sp.className = 'pv pv-sel';
    sp.textContent = (ss[i].options[ss[i].selectedIndex] || {}).text || '';
    ds[i].parentNode.replaceChild(sp, ds[i]);
  }

  /* 3) <input> -> degerini gosteren kutucuk (renk kutusu ise renk orneği) */
  var si = card.querySelectorAll('input'), di = clone.querySelectorAll('input');
  for (i = 0; i < di.length && i < si.length; i++) {
    var box = document.createElement('span');
    if (si[i].type === 'color') {
      box.className = 'pv pv-col';
      box.setAttribute('style', 'background:' + si[i].value);
    } else {
      box.className = 'pv' + (si[i].classList.contains('man') ? ' man' : '') +
                             (si[i].classList.contains('set') ? ' set' : '');
      box.textContent = si[i].value || si[i].placeholder || '';
      if (!si[i].value) box.className += ' pv-dim';
    }
    di[i].parentNode.replaceChild(box, di[i]);
  }

  /* 4) Cizim alani esnek yerine SABIT yukseklik (flex kopyada birebir oturmayabilir) */
  var sp2 = card.querySelector('.plot'), dp = clone.querySelector('.plot');
  if (sp2 && dp) {
    dp.setAttribute('style', 'flex:none;height:' + Math.round(sp2.getBoundingClientRect().height) +
                             'px;position:relative;overflow:hidden');
  }

  /* 5) Sayfanin kendi CSS'i ile birlikte SVG'ye goem */
  var css = '';
  var styles = document.getElementsByTagName('style');
  for (j = 0; j < styles.length; j++) css += styles[j].textContent + '\n';

  var SVGNS = 'http://www.w3.org/2000/svg';
  var XHTML = 'http://www.w3.org/1999/xhtml';

  var svg = document.createElementNS(SVGNS, 'svg');
  svg.setAttribute('xmlns', SVGNS);
  svg.setAttribute('width', W);
  svg.setAttribute('height', H);
  svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);

  var fo = document.createElementNS(SVGNS, 'foreignObject');
  fo.setAttribute('x', '0'); fo.setAttribute('y', '0');
  fo.setAttribute('width', W); fo.setAttribute('height', H);

  /* CSS degiskenleri :root (yani <html>) uzerinde tanimli; kopyanin koku artik
     <html> olmadigi icin var(--...) cozulmez. Gercek degerleri okuyup sarmalayiciya
     satir ici yaziyoruz -- boylece tum renkler birebir korunur. */
  var rootStyle = getComputedStyle(document.documentElement);
  var vars = {}, mv;
  var re = /--[A-Za-z0-9-]+/g;
  while ((mv = re.exec(css)) !== null) {
    if (!vars[mv[0]]) vars[mv[0]] = (rootStyle.getPropertyValue(mv[0]) || '').trim();
  }
  var varCss = Object.keys(vars).filter(function (k) { return vars[k]; })
    .map(function (k) { return k + ':' + vars[k]; }).join(';');

  var wrap = document.createElementNS(XHTML, 'div');
  wrap.setAttribute('style', varCss + ';width:' + W + 'px;height:' + H + 'px;' +
    'background:' + (vars['--bg'] || '#0d1017') + ';color:' + (vars['--txt'] || '#dfe4ee') + ';' +
    'font:13px/1.35 "Segoe UI",Roboto,Arial,sans-serif');
  var st = document.createElementNS(XHTML, 'style');
  st.textContent = css;
  wrap.appendChild(st);
  wrap.appendChild(clone);
  fo.appendChild(wrap);
  svg.appendChild(fo);

  var xml;
  try { xml = new XMLSerializer().serializeToString(svg); }
  catch (e) { done(null, W, H); return; }

  var im = new Image();
  im.onload = function () { done(im, W, H); };
  im.onerror = function () { done(null, W, H); };
  im.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(xml);
};

window.exportPng = function (chartIdx) {
  var C = window.CHARTS[chartIdx];
  if (!C || !C.u) { window.showError('Grafik hazir degil.'); return; }
  var card = document.getElementById('card' + chartIdx);
  if (!card) { window.showError('Grafik penceresi bulunamadi.'); return; }

  /* alt bilgi seridi: disa aktarma ani + gorunen zaman penceresi */
  var u = C.u;
  var ts = function (sec) {                     /* milisaniyesiz zaman damgasi */
    if (sec == null || !isFinite(sec)) return '--';
    var d = new Date(sec * 1000);
    return window.pad(d.getDate(), 2) + '.' + window.pad(d.getMonth() + 1, 2) + ' ' +
           window.pad(d.getHours(), 2) + ':' + window.pad(d.getMinutes(), 2) + ':' +
           window.pad(d.getSeconds(), 2);
  };
  var win = '';
  if (u.scales && u.scales.x && u.scales.x.min != null) {
    win = '  |  ' + ts(u.scales.x.min) + '  ->  ' + ts(u.scales.x.max);
  }
  var d0 = new Date();
  var foot = 'CH' + (chartIdx + 1) + '  |  ' + window.pad(d0.getDate(), 2) + '.' +
             window.pad(d0.getMonth() + 1, 2) + '.' + d0.getFullYear() + ' ' +
             window.pad(d0.getHours(), 2) + ':' + window.pad(d0.getMinutes(), 2) + ':' +
             window.pad(d0.getSeconds(), 2) +
             '  |  GROUP BY ' + (C.interval || '?') + win;

  var fname = window.stampName(chartIdx, 'png');
  var save = function (cv) {
    if (cv.toBlob) {
      cv.toBlob(function (b) {
        if (b) window.downloadBlob(b, fname);
        else window.showError('PNG olusturulamadi.');
      }, 'image/png');
    } else {
      var a = document.createElement('a');
      a.href = cv.toDataURL('image/png');
      a.download = fname;
      a.click();
    }
  };

  window.cardToImage(card, function (im, W, H) {
    var dpr = window.devicePixelRatio || 1;
    var FOOT = 20;

    if (!im) {
      /* SVG rasterlestirmesi engellendiyse en azindan cizim alanini ver */
      var src = u.canvas || (u.ctx && u.ctx.canvas);
      if (!src) { window.showError('PNG olusturulamadi.'); return; }
      var fb = document.createElement('canvas');
      fb.width = src.width; fb.height = src.height;
      var fc = fb.getContext('2d');
      fc.fillStyle = '#0d1017';
      fc.fillRect(0, 0, fb.width, fb.height);
      fc.drawImage(src, 0, 0);
      save(fb);
      return;
    }

    var out = document.createElement('canvas');
    out.width = Math.round(W * dpr);
    out.height = Math.round((H + FOOT) * dpr);
    var c = out.getContext('2d');
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
    c.fillStyle = '#0d1017';
    c.fillRect(0, 0, W, H + FOOT);
    c.drawImage(im, 0, 0, W, H);

    c.fillStyle = '#131824';
    c.fillRect(0, H, W, FOOT);
    c.fillStyle = '#8a93a6';
    c.font = '11px Consolas, "Courier New", monospace';
    c.textBaseline = 'middle';
    c.textAlign = 'left';
    c.fillText(foot, 8, H + FOOT / 2 + 1);
    save(out);
  });
};

/* ======================================================================================
   4) WEBSOCKET  --  SIFIR F5 SENKRONIZASYON
   ====================================================================================== */
window.connectWS = function () {
  var proto = (location.protocol === 'https:') ? 'wss://' : 'ws://';
  var ws;
  try { ws = new WebSocket(proto + location.host + '/ws'); }
  catch (e) { window.showError('WebSocket acilamadi: ' + e.message); return; }
  window.WS = ws;
  ws.onmessage = function (ev) {
    var msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    if (msg.type === 'pool') window.applyPool(msg.sensors || []);
    /* preset listesi GLOBAL: baska bir PC preset kaydederse liste aninda tazelenir */
    else if (msg.type === 'materials') window.applyMaterials(msg.materials || {});
    else if (msg.type === 'presets') {
      window.PRESETS = msg.names || [];
      var sel = document.getElementById('presetSel');
      window.renderPresetList(sel ? sel.value : '');
    }
  };
  ws.onclose = function () { setTimeout(window.connectWS, 2000); };
  ws.onerror = function () { try { ws.close(); } catch (e) {} };
};

/* ======================================================================================
   5) GRAFIK ISKELETI
   ====================================================================================== */
window.newChartState = function (i) {
  return {
    i: i,
    range: '5m',
    interval: '1s',
    fine: 0,                                     /* 0 = varsayilan, 1 = M, 2 = F */
    series: [],                                  /* [{sensor_id, name, unit, axis:'y1'|'y2', color}] */
    yman: { y1: { min: null, max: null }, y2: { min: null, max: null } },
    live: false,                                 /* HER GRAFIK KENDI canli akisini yonetir */
    auto: { y1: true, y2: true },                /* HER EKSEN icin AYRI otomatik genisleme */
    timer: null,
    u: null, busy: false, ro: null
  };
};

window.rangeOptionsHtml = function () {
  return window.RANGES.map(function (r) {
    return '<option value="' + r.key + '">' + r.label + '</option>';
  }).join('');
};

window.yGroupHtml = function (i, key) {
  var K = key.toUpperCase();
  return '<span class="ygrp"><b>' + K + '</b>' +
    '<input type="number" step="any" placeholder="min" title="' + K + ' Min (bos = otomatik)" id="' + key + 'min' + i + '" ' +
      'onchange="window.onYInput(' + i + ',\'' + key + '\',\'min\',this.value)">' +
    '<input type="number" step="any" placeholder="max" title="' + K + ' Max (bos = otomatik)" id="' + key + 'max' + i + '" ' +
      'onchange="window.onYInput(' + i + ',\'' + key + '\',\'max\',this.value)">' +
    '<button class="btn sm" title="' + K + ': gosterilen verinin min-max araligina otur (%10 pay + duzgun yuvarlama)" ' +
      'onclick="window.fitAxis(' + i + ',\'' + key + '\')">FIT</button>' +
    '<button class="btn sm tg on" id="auto' + key + i + '" ' +
      'title="' + K + ' AUTO: canli akista veri ' + K + ' limitini asarsa SADECE ' + K + ' ekseni genisler" ' +
      'onclick="window.toggleAuto(' + i + ',\'' + key + '\')">A</button>' +
    '</span>';
};

/* GROUP BY rozeti + 2 kademe cozunurluk artisi (M = yarisi, F = ceyregi) */
window.intervalGroupHtml = function (i) {
  return '<span class="igrp">' +
    '<span class="ivl" id="ivl' + i + '" title="InfluxQL GROUP BY araligi">1s</span>' +
    '<button class="btn sm tg" id="fm' + i + '" onclick="window.setFine(' + i + ',1)" ' +
      'title="M (Medium): varsayilan GROUP BY degerinin YARISI">M</button>' +
    '<button class="btn sm tg" id="ff' + i + '" onclick="window.setFine(' + i + ',2)" ' +
      'title="F (Fine): varsayilan GROUP BY degerinin CEYREGI">F</button>' +
    '</span>';
};

window.buildLayout = function () {
  var grid = document.getElementById('grid');
  grid.innerHTML = '';
  grid.classList.remove('solo');
  window.FULL = -1;
  for (var i = 0; i < window.CHART_COUNT; i++) {
    if (window.CHARTS[i] && window.CHARTS[i].timer) clearInterval(window.CHARTS[i].timer);
    window.CHARTS[i] = window.newChartState(i);
    var card = document.createElement('div');
    card.className = 'card';
    card.id = 'card' + i;
    card.innerHTML =
      '<div class="chead">' +
        '<span class="ctitle">CH' + (i + 1) + '</span>' +
        '<button class="btn sm live off" id="live' + i + '" onclick="window.toggleChartLive(' + i + ')" ' +
          'title="Bu grafigin canli akisi (X eksenini de sifirlar)">&#9646;&#9646; PAUSE</button>' +
        '<select id="rng' + i + '" onchange="window.onRangeChange(' + i + ', this.value)">' + window.rangeOptionsHtml() + '</select>' +
        window.intervalGroupHtml(i) +
        window.yGroupHtml(i, 'y1') +
        window.yGroupHtml(i, 'y2') +
        '<button class="btn sm ex" style="margin-left:auto" onclick="window.exportCsv(' + i + ')" ' +
          'title="Gorunen araligi CSV olarak indir (Excel uyumlu)">CSV</button>' +
        '<button class="btn sm ex" onclick="window.exportPng(' + i + ')" ' +
          'title="Bu grafigi PNG resim olarak indir">PNG</button>' +
        '<button class="btn sm fs" id="fs' + i + '" onclick="window.toggleFull(' + i + ')" ' +
          'title="Tam ekran: bu grafigi tek basina goster (GROUP BY bir kademe incelir)">&#9633;</button>' +
        '<button class="btn sm" onclick="window.zoomX(' + i + ', 0.7)" title="Zoom In">+</button>' +
        '<button class="btn sm" onclick="window.zoomX(' + i + ', 1.45)" title="Zoom Out">&#8722;</button>' +
      '</div>' +
      '<div class="chips" id="chips' + i + '"></div>' +
      '<div class="infobar" id="info' + i + '"><span class="k">X:</span> --  <span class="k">Y1:</span> --  <span class="k">Y2:</span> --</div>' +
      '<div class="plot" id="plot' + i + '"></div>';
    grid.appendChild(card);

    (function (idx, cardEl) {
      cardEl.addEventListener('dragover', function (ev) { ev.preventDefault(); cardEl.classList.add('dragover'); });
      cardEl.addEventListener('dragleave', function () { cardEl.classList.remove('dragover'); });
      cardEl.addEventListener('drop', function (ev) {
        ev.preventDefault();
        cardEl.classList.remove('dragover');
        var sid = ev.dataTransfer.getData('text/plain');
        if (sid) window.addSeries(idx, sid);
      });
    })(i, card);

    window.renderChips(window.CHARTS[i]);
    window.buildChart(window.CHARTS[i]);
  }
};

/* ======================================================================================
   6) SERI YONETIMI (Drag&Drop, Y1<->Y2, tekil silme)
   ====================================================================================== */
window.addSeries = function (chartIdx, sensorId) {
  var C = window.CHARTS[chartIdx];
  if (!C) return;
  for (var k = 0; k < C.series.length; k++) {
    if (C.series[k].sensor_id === sensorId) return;   /* ayni grafikte tekrar etmesin */
  }
  C.series.push({
    sensor_id: sensorId,
    name: window.poolName(sensorId),
    unit: window.poolUnit(sensorId),
    axis: 'y1',
    color: window.colorFor(sensorId)      /* GLOBAL renk -- tum grafiklerde ayni */
  });
  window.renderChips(C);
  window.buildChart(C);
  window.fetchChart(C, true);
};

window.removeSeries = function (chartIdx, sensorId) {
  var C = window.CHARTS[chartIdx];
  if (!C) return;
  C.series = C.series.filter(function (s) { return s.sensor_id !== sensorId; });
  window.renderChips(C);
  window.buildChart(C);
  window.fetchChart(C, true);
};

window.toggleAxis = function (chartIdx, sensorId) {
  var C = window.CHARTS[chartIdx];
  if (!C) return;
  C.series.forEach(function (s) { if (s.sensor_id === sensorId) s.axis = (s.axis === 'y1' ? 'y2' : 'y1'); });
  window.renderChips(C);
  window.buildChart(C);
  window.fetchChart(C, false);
};

window.renderChips = function (C) {
  var box = document.getElementById('chips' + C.i);
  if (!box) return;
  box.innerHTML = '';
  /* Seri kumesi degisti: alarm cercevesi bayatlamasin. alarmTick alarm KUMESI
     degismediginde DOM'a dokunmaz, bu yuzden cerceveyi burada tazeliyoruz.
     classList.toggle var olan sinifi yeniden eklemedigi icin yanip sonen
     kartlarin animasyon fazi bundan etkilenmez. */
  if (typeof window.applyAlarmClasses === 'function') window.applyAlarmClasses();
  if (!C.series.length) {
    box.innerHTML = '<span class="chipsEmpty">Sensor karti surukleyip birakin...</span>';
    return;
  }
  C.series.forEach(function (s) {
    var chip = document.createElement('span');
    chip.className = 'chip';
    chip.innerHTML =
      '<span class="dot" style="background:' + window.colorFor(s.sensor_id) + '"></span>' +
      '<span>' + window.sensorLabel(s) + '</span>' +
      '<button class="ax' + (s.axis === 'y2' ? ' a2' : '') + '" title="Ekseni degistir">' +
        (s.axis === 'y1' ? 'Y1' : 'Y2') + '</button>' +
      '<button class="xbtn" title="Seriyi kaldir">&#10005;</button>';
    chip.querySelector('.ax').addEventListener('click', function () { window.toggleAxis(C.i, s.sensor_id); });
    chip.querySelector('.xbtn').addEventListener('click', function () { window.removeSeries(C.i, s.sensor_id); });
    box.appendChild(chip);
  });
};

/* ======================================================================================
   7) uPLOT INSASI  --  BELLEK DONGUSU: her yapi degisiminde destroy()
   ====================================================================================== */
/* Eksen araligi: elle girilen min/max HER ZAMAN kazanir; sadece biri girildiyse
   digeri veriden otomatik hesaplanir. Bu fonksiyon setData sonrasi da calistigi
   icin canli akista elle girilen limitler KORUNUR. */
window.mkRange = function (C, key) {
  return function (u, dataMin, dataMax) {
    var m = C.yman[key];
    var lo, hi;
    if (dataMin == null || dataMax == null || !isFinite(dataMin) || !isFinite(dataMax)) {
      lo = 0; hi = 1;
    } else if (dataMin === dataMax) {
      lo = dataMin - 1; hi = dataMax + 1;
    } else {
      /* AUTO (elle limit girilmemis eksen) FIT ile TAMAMEN AYNI formulu kullanir:
         alt = min - 0.1*(max-min) asagiya, ust = max + 0.1*(max-min) yukariya yuvarlanir. */
      var r = window.fitRange(dataMin, dataMax) || uPlot.rangeNum(dataMin, dataMax, 0.1, true);
      lo = r[0]; hi = r[1];
    }
    if (m.min != null && isFinite(m.min)) lo = m.min;
    if (m.max != null && isFinite(m.max)) hi = m.max;
    if (!(hi > lo)) hi = lo + 1e-6;
    return [lo, hi];
  };
};

window.wheelPlugin = function (C) {
  return {
    hooks: {
      ready: function (u) {
        var over = u.over;
        over.addEventListener('wheel', function (e) {
          e.preventDefault();
          var factor = e.deltaY < 0 ? 0.78 : 1.28;
          if (e.ctrlKey || e.metaKey) {
            window.zoomYBy(C, factor);
          } else {
            var rect = over.getBoundingClientRect();
            var cursorVal = u.posToVal(e.clientX - rect.left, 'x');
            var xmin = u.scales.x.min, xmax = u.scales.x.max;
            if (xmin == null || xmax == null) return;
            window.pauseChart(C);
            window.SUPPRESS++;
            u.setScale('x', {
              min: cursorVal - (cursorVal - xmin) * factor,
              max: cursorVal + (xmax - cursorVal) * factor
            });
            window.SUPPRESS--;
          }
        }, { passive: false });

        /* Shift + surukle (veya orta tus) = PAN. Global dinleyiciler tek sefer
           baglanir (window.initGlobalPan), burada sadece aktif grafik isaretlenir. */
        over.addEventListener('mousedown', function (e) {
          if (!e.shiftKey && e.button !== 1) return;
          e.preventDefault();
          window.PAN.u = u;
          window.PAN.C = C;
          window.PAN.lastX = e.clientX;
        });
      }
    }
  };
};

/* Pan icin TEK global dinleyici -- grafik yeniden insa edilse de birikmez */
window.PAN = { u: null, C: null, lastX: 0 };

window.initGlobalPan = function () {
  if (window.PAN.ready) return;
  window.PAN.ready = true;
  document.addEventListener('mousemove', function (e) {
    var u = window.PAN.u;
    if (!u) return;
    var dxPix = e.clientX - window.PAN.lastX;
    window.PAN.lastX = e.clientX;
    var xmin = u.scales.x.min, xmax = u.scales.x.max;
    if (xmin == null || xmax == null || !u.bbox || !u.bbox.width) return;
    var perPix = (xmax - xmin) / u.bbox.width * (window.devicePixelRatio || 1);
    window.pauseChart(window.PAN.C);
    window.SUPPRESS++;
    try { u.setScale('x', { min: xmin - dxPix * perPix, max: xmax - dxPix * perPix }); } catch (err) {}
    window.SUPPRESS--;
  });
  document.addEventListener('mouseup', function () { window.PAN.u = null; window.PAN.C = null; });
};

window.buildChart = function (C) {
  var host = document.getElementById('plot' + C.i);
  if (!host) return;

  /* --- BELLEK TEMIZLIGI --- */
  if (C.u) { try { C.u.destroy(); } catch (e) {} C.u = null; }
  if (C.ro) { try { C.ro.disconnect(); } catch (e) {} C.ro = null; }
  host.innerHTML = '';

  var w = Math.max(120, host.clientWidth);
  var h = Math.max(80, host.clientHeight);

  var series = [{}];
  C.series.forEach(function (s) {
    s.color = window.colorFor(s.sensor_id);    /* renk daima global haritadan gelir */
    series.push({
      label: s.sensor_id,
      scale: s.axis,
      stroke: s.color,
      width: 1.6,
      spanGaps: true,
      points: { show: false }
    });
  });

  var opts = {
    width: w,
    height: h,
    legend: { show: false },
    padding: [8, 8, 0, 0],
    cursor: {
      drag: { x: true, y: false, setScale: true },
      focus: { prox: 24 }
    },
    scales: {
      x: { time: true },
      y1: { auto: true, range: window.mkRange(C, 'y1') },
      y2: { auto: true, range: window.mkRange(C, 'y2') }
    },
    axes: [
      { scale: 'x', stroke: '#8a93a6', font: '11px Consolas, monospace',
        grid: { stroke: '#222a3b', width: 1 }, ticks: { stroke: '#2b3448' } },
      { scale: 'y1', side: 3, stroke: '#8a93a6', size: 58, font: '11px Consolas, monospace',
        grid: { stroke: '#222a3b', width: 1 }, ticks: { stroke: '#2b3448' } },
      { scale: 'y2', side: 1, stroke: '#8a93a6', size: 58, font: '11px Consolas, monospace',
        grid: { show: false }, ticks: { stroke: '#2b3448' } }
    ],
    series: series,
    plugins: [window.wheelPlugin(C)],
    hooks: {
      draw: [function (u) { window.drawLimits(C, u); }],
      setCursor: [function (u) { window.updateInfo(C, u); }],
      setScale: [function (u, key) {
        if (key === 'y1' || key === 'y2') window.syncYInputs(C);
        if (key === 'x' && window.SUPPRESS === 0) window.pauseChart(C);
      }],
      setSelect: [function () { window.pauseChart(C); }]
    }
  };

  /* SUPPRESS: uPlot ilk kurulumda setScale kancalarini tetikler,
     bu tetikleme canli akisi yanlislikla PAUSE etmemeli. */
  window.SUPPRESS++;
  try {
    C.u = new uPlot(opts, window.emptyData(C), host);
  } catch (e) {
    window.SUPPRESS--;
    window.showError('uPlot olusturulamadi (Chart ' + (C.i + 1) + '): ' + e.message);
    return;
  }
  window.SUPPRESS--;

  C.ro = new ResizeObserver(function () {
    if (!C.u) return;
    var nw = Math.max(120, host.clientWidth), nh = Math.max(80, host.clientHeight);
    window.SUPPRESS++;
    try { C.u.setSize({ width: nw, height: nh }); } catch (e) {}
    window.SUPPRESS--;
  });
  C.ro.observe(host);
  window.syncYInputs(C);
};

window.emptyData = function (C) {
  var d = [[]];
  for (var k = 0; k < C.series.length; k++) d.push([]);
  return d;
};

/* ======================================================================================
   8) IMLEC OKUMALARI  (X / Y1 / Y2 -- milisaniye hassasiyetli)
   ====================================================================================== */
window.updateInfo = function (C, u) {
  var bar = document.getElementById('info' + C.i);
  if (!bar) return;
  var left = u.cursor.left, top = u.cursor.top;
  if (left == null || left < 0 || top == null || top < 0) {
    bar.innerHTML = '<span class="k">X:</span> --  <span class="k">Y1:</span> --  <span class="k">Y2:</span> --';
    return;
  }
  var xv = u.posToVal(left, 'x');
  var y1 = u.posToVal(top, 'y1');
  var y2 = u.posToVal(top, 'y2');
  var html = '<span class="k">X:</span> ' + window.fmtDateTimeMs(xv) +
             '   <span class="k">Y1:</span> ' + window.fmtNum(y1) +
             '   <span class="k">Y2:</span> ' + window.fmtNum(y2);
  var idx = u.cursor.idx;
  if (idx != null && u.data[0] && u.data[0].length) {
    C.series.forEach(function (s, k) {
      var arr = u.data[k + 1];
      var v = arr ? arr[idx] : null;
      var nm = (s.name || '').trim() || s.sensor_id;
      html += '   <span style="color:' + window.colorFor(s.sensor_id) + '">&#9679; ' + window.esc(nm) +
              ' ' + s.axis.toUpperCase() + ': ' + window.fmtNum(v) + (s.unit ? ' ' + window.esc(s.unit) : '') + '</span>';
    });
  }
  bar.innerHTML = html;
};

/* ======================================================================================
   9) Y1 / Y2  MIN-MAX KUTULARI
   ====================================================================================== */
/* Elle girilen limitleri uygula: setData -> _rangeY -> mkRange zincirini tetikler.
   resetScales=false verildigi icin X ekseni ve dolayisiyla LIVE durumu BOZULMAZ. */
window.applyY = function (C) {
  if (!C || !C.u) return;
  window.SUPPRESS++;
  try { C.u.setData(C.u.data, false); } catch (e) {}
  window.SUPPRESS--;
  window.syncYInputs(C);
};

window.onYInput = function (chartIdx, axisKey, side, raw) {
  var C = window.CHARTS[chartIdx];
  if (!C) return;
  var v = (raw === '' || raw == null || String(raw).trim() === '') ? null : parseFloat(raw);
  C.yman[axisKey][side] = (v == null || !isFinite(v)) ? null : v;

  /* min > max girildiyse sessizce yer degistir */
  var m = C.yman[axisKey];
  if (m.min != null && m.max != null && m.min > m.max) {
    var t = m.min; m.min = m.max; m.max = t;
  }
  window.applyY(C);
};

/* Elle kilitli kutulari vurgula; kilitli olmayanlara guncel otomatik degeri yaz */
window.syncYInputs = function (C) {
  if (!C || !C.u) return;
  ['y1', 'y2'].forEach(function (k) {
    var sc = C.u.scales[k];
    if (!sc) return;
    var hasData = window.axisDataRange(C, k) !== null;
    ['min', 'max'].forEach(function (side) {
      var el = document.getElementById(k + side + C.i);
      if (!el) return;
      var locked = C.yman[k][side] != null;
      el.classList.toggle('man', locked);
      if (document.activeElement === el) return;
      if (locked) el.value = window.fmtBox(C.yman[k][side]);
      else el.value = hasData ? window.fmtBox(sc[side]) : '';   /* veri yoksa bos birak */
    });
  });
};

/* Verinin (gorunur X penceresindeki) gercek min/max degeri */
window.axisDataRange = function (C, key) {
  if (!C || !C.u || !C.u.data) return null;
  var xs = C.u.data[0] || [];
  var xmin = C.u.scales.x.min, xmax = C.u.scales.x.max;
  var lo = null, hi = null;
  C.series.forEach(function (s, k) {
    if (s.axis !== key) return;
    var arr = C.u.data[k + 1];
    if (!arr) return;
    for (var i = 0; i < arr.length; i++) {
      if (xmin != null && (xs[i] < xmin || xs[i] > xmax)) continue;
      var v = arr[i];
      if (v == null || !isFinite(v)) continue;
      if (lo === null || v < lo) lo = v;
      if (hi === null || v > hi) hi = v;
    }
  });
  return (lo === null) ? null : [lo, hi];
};

/* FIT: ekseni  [min - 0.1*(max-min) , max + 0.1*(max-min)]  araligina kilitler,
   alt sinir asagiya / ust sinir yukariya duzgun bir degere yuvarlanir. */
window.fitAxis = function (chartIdx, key) {
  var C = window.CHARTS[chartIdx];
  if (!C || !C.u) return;
  var r = window.axisDataRange(C, key);
  if (!r) {   /* o eksende veri yok -> otomatige don */
    C.yman[key] = { min: null, max: null };
    window.applyY(C);
    return;
  }
  var pr = window.fitRange(r[0], r[1]);
  if (!pr) return;
  C.yman[key] = { min: pr[0], max: pr[1] };
  window.applyY(C);
};

/* AUTO: canli akista veri o EKSENIN limitini asarsa SADECE o ekseni genislet.
   Y1 ve Y2 birbirinden tamamen bagimsizdir. */
window.autoExpand = function (C) {
  if (!C || !C.u) return false;
  var changed = false;
  ['y1', 'y2'].forEach(function (key) {
    if (!C.auto || !C.auto[key]) return;            /* bu eksende AUTO kapali */
    var m = C.yman[key];
    if (m.min == null && m.max == null) return;     /* zaten tam otomatik */
    var r = window.axisDataRange(C, key);
    if (!r) return;
    var lo = (m.min != null) ? m.min : r[0];
    var hi = (m.max != null) ? m.max : r[1];
    if (r[0] >= lo && r[1] <= hi) return;           /* limit asilmamis */
    var pr = window.fitRange(Math.min(r[0], lo), Math.max(r[1], hi));
    if (!pr) return;
    if (m.min != null) m.min = pr[0];
    if (m.max != null) m.max = pr[1];
    changed = true;
  });
  return changed;
};

window.syncAutoBtns = function (C) {
  ['y1', 'y2'].forEach(function (key) {
    var b = document.getElementById('auto' + key + C.i);
    if (b) b.className = 'btn sm tg' + ((C.auto && C.auto[key]) ? ' on' : '');
  });
};

window.toggleAuto = function (chartIdx, key) {
  var C = window.CHARTS[chartIdx];
  if (!C) return;
  if (!C.auto) C.auto = { y1: true, y2: true };
  C.auto[key] = !C.auto[key];
  window.syncAutoBtns(C);
  if (C.auto[key] && window.autoExpand(C)) window.applyY(C);
};

window.zoomYBy = function (C, factor) {
  if (!C.u) return;
  window.pauseChart(C);
  ['y1', 'y2'].forEach(function (k) {
    var sc = C.u.scales[k];
    if (!sc || sc.min == null || sc.max == null) return;
    var c = (sc.min + sc.max) / 2, half = (sc.max - sc.min) / 2 * factor;
    C.yman[k].min = c - half;
    C.yman[k].max = c + half;
  });
  window.applyY(C);
};

/* ======================================================================================
   10) GRAFIK BUTON KONTROLLERI
   ====================================================================================== */
window.zoomX = function (chartIdx, factor) {
  var C = window.CHARTS[chartIdx];
  if (!C || !C.u) return;
  var sc = C.u.scales.x;
  if (sc.min == null || sc.max == null) return;
  var c = (sc.min + sc.max) / 2, half = (sc.max - sc.min) / 2 * factor;
  window.pauseChart(C);
  window.SUPPRESS++;
  try { C.u.setScale('x', { min: c - half, max: c + half }); } catch (e) {}
  window.SUPPRESS--;
};

window.onRangeChange = function (chartIdx, key) {
  var C = window.CHARTS[chartIdx];
  if (!C) return;
  C.range = key;
  window.fetchChart(C, true);
};

/* ---------------------------------------------------------------------------------------
   TAM EKRAN (SOLO)
   Secilen grafik 4'lu izgarayi kaplar; butonlar / kutular / davranis birebir aynidir.
   Ekran buyudugu icin GROUP BY bir kademe incelir (10 Saat: 30s -> 15s).
   --------------------------------------------------------------------------------------- */
window.resizeAllCharts = function () {
  window.CHARTS.forEach(function (C) {
    var host = document.getElementById('plot' + C.i);
    if (!C.u || !host) return;
    window.SUPPRESS++;
    try { C.u.setSize({ width: Math.max(120, host.clientWidth), height: Math.max(80, host.clientHeight) }); }
    catch (e) {}
    window.SUPPRESS--;
  });
};

window.toggleFull = function (chartIdx) {
  var grid = document.getElementById('grid');
  if (!grid) return;
  var on = (window.FULL !== chartIdx);
  window.FULL = on ? chartIdx : -1;

  for (var k = 0; k < window.CHART_COUNT; k++) {
    var card = document.getElementById('card' + k);
    if (card) card.classList.toggle('full', on && k === chartIdx);
    var b = document.getElementById('fs' + k);
    if (b) {
      var active = (on && k === chartIdx);
      b.className = 'btn sm fs' + (active ? ' on' : '');
      b.innerHTML = active ? '&#9635;' : '&#9633;';
      b.title = active ? 'Tam ekrandan cik (4 grafik gorunumu)'
                       : 'Tam ekran: bu grafigi tek basina goster (GROUP BY bir kademe incelir)';
    }
  }
  grid.classList.toggle('solo', on);

  /* yerlesim oturduktan sonra boyutlandir ve YENI cozunurlukle tazele */
  setTimeout(function () {
    window.resizeAllCharts();
    var C = window.CHARTS[chartIdx];
    if (C) window.fetchChart(C, false);
  }, 60);
};

/* ---------------------------------------------------------------------------------------
   COZUNURLUK KADEMESI
   Sunucudaki varsayilan GROUP BY degerinin uzerine 2 kademe inceltme:
     M = varsayilanin yarisi     F = varsayilanin ceyregi
   Tam ekran bunlarin uzerine 1 kademe daha ekler (toplam en fazla 3 kademe).
   --------------------------------------------------------------------------------------- */
window.effFine = function (C) {
  var f = (C.fine || 0) + (window.FULL === C.i ? 1 : 0);
  return Math.max(0, Math.min(3, f));
};

window.syncFineBtns = function (C) {
  var m = document.getElementById('fm' + C.i);
  var f = document.getElementById('ff' + C.i);
  if (m) m.className = 'btn sm tg' + (C.fine === 1 ? ' on' : '');
  if (f) f.className = 'btn sm tg' + (C.fine === 2 ? ' on' : '');
};

window.setFine = function (chartIdx, level) {
  var C = window.CHARTS[chartIdx];
  if (!C) return;
  C.fine = (C.fine === level) ? 0 : level;   /* ayni tusa tekrar basmak varsayilana dondurur */
  window.syncFineBtns(C);
  window.fetchChart(C, false);
};

/* ======================================================================================
   11) VERI CEKME  (InfluxQL -> [[ts...],[val...]])
   ====================================================================================== */
window.fetchChart = function (C, resetScales) {
  if (!C) return Promise.resolve();
  if (!C.series.length) {
    if (C.u) {
      window.SUPPRESS++;
      try { C.u.setData(window.emptyData(C), true); } catch (e) {}
      window.SUPPRESS--;
    }
    return Promise.resolve();
  }
  if (C.busy) return Promise.resolve();
  C.busy = true;

  /* HESAP sensorleri InfluxDB'de yoktur: sunucudan yalnizca BAGIMLILIKLARI istenir,
     formul sonucu yanit gelince tarayicida uretilir (window.fxFillSeries). */
  var plan = window.fxPlan(C.series.map(function (s) { return s.sensor_id; }));
  if (plan.err) window.showError('Formul: ' + plan.err);

  var url = '/api/data?range=' + encodeURIComponent(C.range) +
            '&fine=' + window.effFine(C) +
            '&sensors=' + encodeURIComponent(plan.fetch.join(','));

  return fetch(url).then(function (r) {
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }).then(function (j) {
    C.busy = false;
    window.fxFillSeries(j, plan.order);
    if (j.error) {
      window.setDbStatus(false, 'InfluxDB: ' + j.error.substring(0, 60));
    } else {
      window.setDbStatus(true, 'InfluxDB OK | ' + j.points + ' nokta / ' + j.interval);
    }
    C.interval = j.interval || C.interval;
    var ivl = document.getElementById('ivl' + C.i);
    if (ivl) {
      ivl.textContent = C.interval;
      var lvl = j.fine || 0;
      var tag = lvl === 0 ? 'varsayilan' : (lvl === 1 ? 'M (1 kademe ince)' :
                (lvl === 2 ? 'F (2 kademe ince)' : '3 kademe ince'));
      ivl.title = 'InfluxQL: GROUP BY time(' + C.interval + ')\n' +
                  'Varsayilan: ' + (j.base_interval || '?') + '  |  Su an: ' + tag +
                  (window.FULL === C.i ? '  (+1 kademe: tam ekran)' : '');
    }

    if (!C.u || (C.u.series.length - 1) !== C.series.length) window.buildChart(C);
    if (!C.u) return;

    var data = [j.t || []];
    C.series.forEach(function (s) { data.push((j.series && j.series[s.sensor_id]) || []); });

    window.SUPPRESS++;
    try { C.u.setData(data, resetScales !== false); } catch (e) { window.showError('setData: ' + e.message); }
    window.SUPPRESS--;

    /* AUTO: yeni veri elle girilen limiti astiysa ekseni genislet */
    if (window.autoExpand(C)) window.applyY(C);
    else window.syncYInputs(C);

    /* ALARM hizli yolu: bu grafikteki serilerin son degerini aninda degerlendir
       (izleyici zaten 3 sn'de bir tum sensorleri tarar) */
    var touched = false;
    C.series.forEach(function (s, k) {
      var arr = data[k + 1] || [];
      for (var q = arr.length - 1; q >= 0; q--) {
        if (arr[q] != null && isFinite(arr[q])) { window.LASTVAL[s.sensor_id] = arr[q]; touched = true; break; }
      }
    });
    if (touched) window.evalAlarms();
  }).catch(function (e) {
    C.busy = false;
    window.setDbStatus(false, 'Veri hatasi: ' + e.message);
  });
};

window.refreshAll = function (resetScales) {
  window.CHARTS.forEach(function (C) { window.fetchChart(C, resetScales); });
};

/* ======================================================================================
   12) LIVE / PAUSE  --  HER GRAFIK ICIN AYRI (kendi timer'i, kendi durumu)
   ====================================================================================== */
window.setChartLive = function (C, on) {
  if (!C) return;
  C.live = !!on;
  var b = document.getElementById('live' + C.i);
  if (b) {
    b.className = 'btn sm live ' + (C.live ? 'on' : 'off');
    b.innerHTML = C.live ? '&#9679; LIVE' : '&#9646;&#9646; PAUSE';
  }
  if (C.timer) { clearInterval(C.timer); C.timer = null; }
  if (!C.live) return;
  /* LIVE'a basmak ayni zamanda X eksenini tam araliga geri getirir (eski "Home") */
  window.fetchChart(C, true);
  C.timer = setInterval(function () {
    if (C.live) window.fetchChart(C, true);
  }, window.REFRESH);
};

window.pauseChart = function (C) { if (C && C.live) window.setChartLive(C, false); };

window.toggleChartLive = function (chartIdx) {
  var C = window.CHARTS[chartIdx];
  if (C) window.setChartLive(C, !C.live);
};

window.setAllLive = function (on) {
  window.CHARTS.forEach(function (C) { window.setChartLive(C, on); });
};

/* Yenileme periyodu degistiginde canli grafiklerin timer'larini yeniden kur */
window.restartLiveTimers = function () {
  window.CHARTS.forEach(function (C) { if (C.live) window.setChartLive(C, true); });
};

/* ======================================================================================
   13) PRESETS  --  GLOBAL (sunucuda presets.json)
   --------------------------------------------------------------------------------------
   * Preset LISTESI ve ICERIGI sunucuda durur: hangi PC / hangi tarayici olursa olsun
     ayni presetler gorunur. Yeni kayit WebSocket ile tum ekranlara aninda yayilir.
   * Preset icindeki SENSOR HAVUZU ise yalnizca preseti acan ekrana yuklenir (PPOOL).
     Sunucudaki webhook havuzuna dokunulmaz -> baska bir PC'de preset secen biri
     kimsenin ekranindaki havuzu degistiremez.
   ====================================================================================== */
window.PRESET_KEY = 'tbm_presets_v1';   /* yalnizca eski surumden tasima icin */

window.renderPresetList = function (selected) {
  var sel = document.getElementById('presetSel');
  if (!sel) return;
  var keep = (selected != null) ? selected : sel.value;
  sel.innerHTML = '<option value="">-- Secilmedi --</option>';
  (window.PRESETS || []).forEach(function (n) {
    var o = document.createElement('option');
    o.value = n; o.textContent = n;
    sel.appendChild(o);
  });
  sel.value = (keep && window.PRESETS.indexOf(keep) >= 0) ? keep : '';
};

window.fetchPresetList = function (selected) {
  return fetch('/api/presets').then(function (r) { return r.json(); }).then(function (j) {
    window.PRESETS = j.names || [];
    window.renderPresetList(selected);
  }).catch(function (e) { window.showError('Preset listesi alinamadi: ' + e.message); });
};

/* Eski surumden kalan yerel presetleri bir defaliga sunucuya tasi */
window.migrateLocalPresets = function () {
  var ps = null;
  try { ps = JSON.parse(localStorage.getItem(window.PRESET_KEY) || 'null'); } catch (e) { ps = null; }
  if (!ps || typeof ps !== 'object' || !Object.keys(ps).length) return Promise.resolve();
  return fetch('/api/presets_import', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ presets: ps })
  }).then(function (r) { return r.json(); }).then(function (j) {
    try { localStorage.removeItem(window.PRESET_KEY); } catch (e) {}
    window.PRESETS = j.names || [];
  }).catch(function () { /* tasima basarisizsa sessizce devam */ });
};

/* Preset ICERIGI: sensor havuzu + renkler + 4 grafigin tam yerlesimi */
window.savePreset = function () {
  var name = prompt('Preset adi (tum PC\'lerde gorunur):');
  if (!name) return;
  name = name.trim();
  if (!name) return;
  if (window.PRESETS.indexOf(name) >= 0 &&
      !confirm('"' + name + '" zaten var. Uzerine yazilsin mi?')) return;

  var colors = {};
  window.allSensors().forEach(function (s) { colors[s.sensor_id] = window.colorFor(s.sensor_id); });
  window.CHARTS.forEach(function (C) {
    C.series.forEach(function (s) { colors[s.sensor_id] = window.colorFor(s.sensor_id); });
  });

  var body = {
    v: 3,
    /* havuz = webhook + preset havuzu birlikte (grafiklerdeki her seri kapsansin) */
    pool: window.allSensors().map(function (s) {
      var l = window.limitsOf(s.sensor_id);
      return {
        influx_name: s.sensor_id, name: s.name || '', unit: s.unit || '',
        min: l.min, max: l.max,         /* alarm esikleri preset ile birlikte tasinir */
        expr: s.expr || '',             /* hesap kartlarinin formulu de tasinir */
        manual: !!s.manual,
        /* sensor bazli kaynak: bos birakilan alan genel ayardan gelir */
        db: s.db || '', measurement: s.meas || '', influx_url: s.url || ''
      };
    }),
    colors: colors,
    charts: window.CHARTS.map(function (C) {
      return {
        range: C.range,
        fine: C.fine || 0,
        yman: C.yman,
        auto: { y1: !!(C.auto && C.auto.y1), y2: !!(C.auto && C.auto.y2) },
        live: C.live,
        series: C.series.map(function (s) {
          return { sensor_id: s.sensor_id, name: s.name || '', unit: s.unit || '', axis: s.axis };
        })
      };
    })
  };

  fetch('/api/presets/' + encodeURIComponent(name), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  }).then(function (r) {
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }).then(function (j) {
    window.PRESETS = j.names || [];
    window.renderPresetList(name);
  }).catch(function (e) { window.showError('Preset kaydedilemedi: ' + e.message); });
};

window.applyPresetCharts = function (charts) {
  (charts || []).forEach(function (cc, i) {
    var C = window.CHARTS[i];
    if (!C) return;
    C.range = cc.range || '5m';
    C.fine = Math.max(0, Math.min(2, parseInt(cc.fine, 10) || 0));
    C.yman = cc.yman || { y1: { min: null, max: null }, y2: { min: null, max: null } };
    /* v2 presetlerinde auto tek bir boolean idi -> her iki eksene de uygula */
    if (cc.auto && typeof cc.auto === 'object') {
      C.auto = { y1: cc.auto.y1 !== false, y2: cc.auto.y2 !== false };
    } else {
      C.auto = { y1: cc.auto !== false, y2: cc.auto !== false };
    }
    C.series = (cc.series || []).map(function (s) {
      return {
        sensor_id: s.sensor_id,
        name: (s.name != null ? s.name : window.poolName(s.sensor_id)) || '',
        unit: (s.unit != null ? s.unit : window.poolUnit(s.sensor_id)) || '',
        axis: s.axis === 'y2' ? 'y2' : 'y1',
        color: window.colorFor(s.sensor_id)
      };
    });
    var rs = document.getElementById('rng' + i);
    if (rs) rs.value = C.range;
    window.syncAutoBtns(C);
    window.syncFineBtns(C);
    window.renderChips(C);
    window.buildChart(C);
    window.setChartLive(C, cc.live !== false);
    if (!C.live) window.fetchChart(C, true);
  });
};

window.loadPreset = function (name) {
  if (!name) return;
  fetch('/api/presets/' + encodeURIComponent(name)).then(function (r) {
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }).then(function (res) {
    var cfg = res.preset;
    if (!cfg) return;

    /* v1 (cok eski) preset: sadece grafik dizisi tutuyordu */
    if (Array.isArray(cfg)) { window.applyPresetCharts(cfg); return; }

    /* 1) Renkler */
    if (cfg.colors) {
      Object.keys(cfg.colors).forEach(function (k) {
        if (/^#[0-9a-fA-F]{6}$/.test(cfg.colors[k])) window.SCOLORS[k] = cfg.colors[k];
      });
      window.persistColors();
    }

    /* 2) Havuz -- SADECE bu ekrana, gecici olarak. Sunucuya ASLA yazilmaz. */
    window.setPresetPool(cfg.pool || []);

    /* 2b) Kayittaki KART SIRASI: preset kartlari zaten o sirada gelir; ayni kartlar
       webhook havuzunda da varsa onlar da kaydedildigi sirayla dizilir. */
    var inPool = {};
    window.POOL.forEach(function (s) { inPool[s.sensor_id] = 1; });
    var want = (cfg.pool || []).map(function (it) {
      return String(it.influx_name || it.sensor_id || '').trim();
    }).filter(function (id) { return inPool[id]; });
    var now = window.POOL.map(function (s) { return s.sensor_id; })
      .filter(function (id) { return want.indexOf(id) >= 0; });
    if (want.length > 1 && want.join(',') !== now.join(',')) window.saveOrder(want);

    /* 3) Grafik yerlesimi */
    window.applyPresetCharts(cfg.charts);
  }).catch(function (e) { window.showError('Preset yuklenemedi: ' + e.message); });
};

window.deletePreset = function () {
  var sel = document.getElementById('presetSel');
  if (!sel || !sel.value) { alert('Once bir preset secin.'); return; }
  var name = sel.value;
  if (!confirm('"' + name + '" preseti SUNUCUDAN silinsin mi?\nTum PC\'lerden kaybolur.')) return;
  fetch('/api/presets/' + encodeURIComponent(name), { method: 'DELETE' })
    .then(function (r) { return r.json(); })
    .then(function (j) { window.PRESETS = j.names || []; window.renderPresetList(''); })
    .catch(function (e) { window.showError('Preset silinemedi: ' + e.message); });
};

/* ======================================================================================
   13b) MALZEME PENCERESI
   ====================================================================================== */
window.fmtMat = function (v) {
  if (v == null || !isFinite(v)) return '-';
  var a = Math.abs(v);
  return (a !== 0 && (a < 0.01 || a >= 1e6)) ? v.toExponential(4) : String(Math.round(v * 10000) / 10000);
};

window.renderMatList = function () {
  var box = document.getElementById('matList');
  if (!box) return;
  var names = Object.keys(window.MATERIALS).sort(function (a, b) { return a.toLowerCase() < b.toLowerCase() ? -1 : 1; });
  if (!names.length) { box.innerHTML = '<div style="color:var(--dim)">Henuz malzeme yok.</div>'; return; }
  var h = '<table><tr><th>Ad</th><th>Satir</th><th>Sicaklik araligi [degC]</th><th>Ornek</th><th></th></tr>';
  names.forEach(function (n) {
    var r = window.MATERIALS[n].rows || [];
    h += '<tr><td><b>' + window.esc(n) + '</b></td><td>' + r.length + '</td><td>' +
         (r.length ? window.fmtMat(r[0][0]) + ' .. ' + window.fmtMat(r[r.length - 1][0]) : '-') + '</td>' +
         '<td><code>' + window.esc(n) + '_dens(25)</code></td>' +
         '<td style="white-space:nowrap"><button class="btn sm" data-edit="' + window.esc(n) + '">Duzenle</button> ' +
         '<button class="btn sm" data-del="' + window.esc(n) + '">Sil</button></td></tr>';
  });
  box.innerHTML = h + '</table>';
  Array.prototype.forEach.call(box.querySelectorAll('[data-del]'), function (b) {
    b.addEventListener('click', function () { window.deleteMaterial(b.getAttribute('data-del')); });
  });
  Array.prototype.forEach.call(box.querySelectorAll('[data-edit]'), function (b) {
    b.addEventListener('click', function () { window.editMaterial(b.getAttribute('data-edit')); });
  });
};

/* Kayitli tabloyu duzenlemek icin CSV'ye geri cevir */
window.editMaterial = function (name) {
  var m = window.MATERIALS[name];
  if (!m) return;
  var lines = ['temp [degC],dens [kg/m3],cp [J/kg\u00b7K],dvis [Pa\u00b7s],cond [W/m\u00b7K]'];
  (m.rows || []).forEach(function (r) { lines.push(r.join(',')); });
  document.getElementById('matName').value = name;
  document.getElementById('matCsv').value = lines.join('\n');
  window.previewMaterial();
};

window.matMsg = function (txt, cls) {
  var el = document.getElementById('matMsg');
  el.textContent = txt;
  el.className = cls || '';
};

window.postMaterial = function (dry) {
  var name = document.getElementById('matName').value.trim();
  var csv = document.getElementById('matCsv').value;
  return fetch('/api/materials/' + encodeURIComponent(name || 'X'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ csv: csv, dry: !!dry })
  }).then(function (r) {
    return r.json().catch(function () { return {}; }).then(function (j) {
      if (!r.ok) throw new Error(j.detail || ('HTTP ' + r.status));
      return j;
    });
  });
};

window.previewMaterial = function () {
  if (!document.getElementById('matCsv').value.trim()) { window.matMsg(''); return; }
  window.postMaterial(true).then(function (j) {
    var r = j.material.rows, last = r[r.length - 1];
    window.matMsg('Gecerli: ' + r.length + ' satir, ' + window.fmtMat(r[0][0]) + ' .. ' +
                  window.fmtMat(last[0]) + ' degC.  Ornek ilk satir: dens=' + window.fmtMat(r[0][1]) +
                  ' cp=' + window.fmtMat(r[0][2]) + ' dvis=' + window.fmtMat(r[0][3]) +
                  ' cond=' + window.fmtMat(r[0][4]), 'ok');
  }).catch(function (e) { window.matMsg('HATA: ' + e.message, 'bad'); });
};

window.saveMaterial = function () {
  var name = document.getElementById('matName').value.trim();
  if (!name) { window.matMsg('HATA: ad bos.', 'bad'); return; }
  var exists = Object.keys(window.MATERIALS).some(function (k) { return k.toLowerCase() === name.toLowerCase(); });
  if (exists && !confirm('"' + name + '" zaten var. Uzerine yazilsin mi? (tum PC\'leri etkiler)')) return;
  window.postMaterial(false).then(function (j) {
    window.applyMaterials(j.materials || {});
    window.matMsg('"' + j.name + '" kaydedildi. Formulde: ' + j.name + '_dens(x)', 'ok');
    document.getElementById('matCsv').value = '';
    document.getElementById('matFile').value = '';
  }).catch(function (e) { window.matMsg('HATA: ' + e.message, 'bad'); });
};

window.deleteMaterial = function (name) {
  if (!confirm('"' + name + '" malzemesi SUNUCUDAN silinsin mi?\nTum PC\'lerden kaybolur; onu kullanan formuller hata verir.')) return;
  fetch('/api/materials/' + encodeURIComponent(name), { method: 'DELETE' })
    .then(function (r) { return r.json(); })
    .then(function (j) { window.applyMaterials(j.materials || {}); window.matMsg('"' + name + '" silindi.', 'ok'); })
    .catch(function (e) { window.matMsg('HATA: ' + e.message, 'bad'); });
};

window.openMaterials = function () {
  window.renderMatList();
  window.matMsg('');
  document.getElementById('matMask').style.display = 'flex';
};
window.closeMaterials = function () { document.getElementById('matMask').style.display = 'none'; };

window.initMaterials = function () {
  var file = document.getElementById('matFile');
  var csv = document.getElementById('matCsv');
  var nm = document.getElementById('matName');
  file.addEventListener('change', function () {
    var f = file.files && file.files[0];
    if (!f) return;
    if (!nm.value.trim()) nm.value = f.name.replace(/\.[^.]*$/, '').replace(/[^A-Za-z0-9_]/g, '_').replace(/^[^A-Za-z]+/, '');
    var rd = new FileReader();
    rd.onload = function () {
      var txt = String(rd.result || '');
      /* Excel bazen ANSI (cp1254) kaydeder: UTF-8 bozuksa yeniden oku */
      if (txt.indexOf('\ufffd') >= 0) {
        var r2 = new FileReader();
        r2.onload = function () { csv.value = String(r2.result || ''); window.previewMaterial(); };
        r2.readAsText(f, 'windows-1254');
        return;
      }
      csv.value = txt;
      window.previewMaterial();
    };
    rd.readAsText(f, 'utf-8');
  });
  var tmr = null;
  csv.addEventListener('input', function () { clearTimeout(tmr); tmr = setTimeout(window.previewMaterial, 300); });
  fetch('/api/materials').then(function (r) { return r.json(); })
    .then(function (j) { window.applyMaterials(j.materials || {}); })
    .catch(function () {});
};

/* ======================================================================================
   14) SETTINGS MODAL
   ====================================================================================== */
window.openSettings = function () {
  fetch('/api/config').then(function (r) { return r.json(); }).then(function (c) {
    document.getElementById('cfgUrl').value = c.influx_url || '';
    document.getElementById('cfgDb').value = c.db || '';
    document.getElementById('cfgMeas').value = c.measurement || 'telemetry';
    document.getElementById('cfgUser').value = c.username || '';
    document.getElementById('cfgPass').value = c.password || '';
    document.getElementById('cfgRefresh').value = c.refresh_ms || 1000;
    document.getElementById('cfgVol').value = Math.round(window.ALARM_VOL * 100);
    document.getElementById('mmsg').textContent = '';
    document.getElementById('mask').style.display = 'flex';
  }).catch(function (e) { window.showError('Ayarlar okunamadi: ' + e.message); });
};

window.closeSettings = function () { document.getElementById('mask').style.display = 'none'; };

window.collectSettings = function () {
  return {
    influx_url: document.getElementById('cfgUrl').value.trim(),
    db: document.getElementById('cfgDb').value.trim(),
    measurement: document.getElementById('cfgMeas').value.trim() || 'telemetry',
    username: document.getElementById('cfgUser').value,
    password: document.getElementById('cfgPass').value,
    refresh_ms: parseInt(document.getElementById('cfgRefresh').value, 10) || 1000
  };
};

window.saveSettings = function () {
  /* Ses seviyesi SUNUCUYA gitmez: her ekranin kendi ortam gurultusu farklidir */
  var v = parseFloat(document.getElementById('cfgVol').value);
  if (isFinite(v)) {
    window.ALARM_VOL = Math.max(0, Math.min(1, v / 100));
    try { localStorage.setItem('tbm_alarm_vol', String(window.ALARM_VOL)); } catch (e) {}
  }

  fetch('/api/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(window.collectSettings())
  }).then(function (r) { return r.json(); }).then(function (c) {
    window.REFRESH = c.refresh_ms || 1000;
    document.getElementById('mmsg').style.color = 'var(--ok)';
    document.getElementById('mmsg').textContent = 'config.json kaydedildi.';
    window.restartLiveTimers();
    window.refreshAll(true);
    setTimeout(window.closeSettings, 700);
  }).catch(function (e) { window.showError('Ayarlar kaydedilemedi: ' + e.message); });
};

window.testSettings = function () {
  var m = document.getElementById('mmsg');
  m.style.color = 'var(--dim)'; m.textContent = 'Test ediliyor...';
  fetch('/api/health').then(function (r) { return r.json(); }).then(function (j) {
    m.style.color = j.ok ? 'var(--ok)' : 'var(--bad)';
    m.textContent = j.ok ? ('Baglanti OK (' + (j.version || 'influxdb') + ')') : ('HATA: ' + j.error);
  }).catch(function (e) { m.style.color = 'var(--bad)'; m.textContent = 'HATA: ' + e.message; });
};

/* ======================================================================================
   15) BOOT
   ====================================================================================== */
window.boot = function () {
  if (typeof uPlot === 'undefined') {
    window.showError('Cizim motoru yuklenemedi (beklenmedik durum).');
    return;
  }
  window.loadColors();
  window.initGlobalPan();
  window.initPoolReorder();
  window.initMaterials();
  document.addEventListener('keydown', function (e) {
    if (e.ctrlKey && e.shiftKey && (e.key === 'M' || e.key === 'm')) {
      e.preventDefault();
      window.triggerMock();
    }
  });

  /* --- ALARM: ses tercihi + izleyici ------------------------------------------------- */
  try { window.MUTED = localStorage.getItem('tbm_muted') === '1'; } catch (e) {}
  window.syncMuteBtn();

  /* Tarayicilar ilk kullanici etkilesimi olmadan ses calmaya izin vermez:
     ilk tiklamada ses baglamini uyandir (sessiz, tek seferlik). */
  var wake = function () {
    var ctx = window.audioCtx();
    if (ctx && ctx.state === 'suspended') { try { ctx.resume(); } catch (e) {} }
    document.removeEventListener('click', wake);
    document.removeEventListener('keydown', wake);
  };
  document.addEventListener('click', wake);
  document.addEventListener('keydown', wake);

  try {
    var v = parseFloat(localStorage.getItem('tbm_alarm_vol'));
    if (isFinite(v)) window.ALARM_VOL = Math.max(0, Math.min(1, v));
  } catch (e) {}

  /* Esigi olan sensorleri grafiklerden BAGIMSIZ izle (3 sn'de bir, cok hafif) */
  setInterval(window.alarmWatch, 3000);
  /* Tutma sureleri dolanlari serbest birak (veri cekmez, sadece sayac suzer) */
  setInterval(window.alarmTick, 300);

  fetch('/api/meta').then(function (r) { return r.json(); }).then(function (meta) {
    window.RANGES = meta.ranges || [];
    window.REFRESH = meta.refresh_ms || 1000;
    window.buildLayout();
    return window.migrateLocalPresets();
  }).then(function () {
    return window.fetchPresetList('');
  }).then(function () {
    return fetch('/api/sensors');
  }).then(function (r) { return r.json(); }).then(function (j) {
    window.POOL = j.sensors || [];
    window.renderPool();
    window.connectWS();
    window.setAllLive(true);
    window.alarmWatch();
  }).catch(function (e) {
    window.showError('Baslatma hatasi: ' + e.message);
  });

  window.addEventListener('resize', function () { window.resizeAllCharts(); });
};

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', window.boot);
else window.boot();
</script>
</body>
</html>
"""

# ==========================================================================================
# 8) FASTAPI APP
# ==========================================================================================

app = FastAPI(title="Industrial Test Bench Monitor", version="1.0.0")

os.makedirs(VENDOR_DIR, exist_ok=True)
app.mount("/vendor", StaticFiles(directory=VENDOR_DIR), name="vendor")


class SensorIn(BaseModel):
    """Webhook govdesi.
       influx_name : InfluxDB'deki alan adi   -> ZORUNLU
       name        : Arayuzde gorunecek ad    -> opsiyonel
       unit        : Olcu birimi              -> opsiyonel
       sensor_id   : eski surum takma adi (influx_name ile ayni is) -> opsiyonel"""
    influx_name: Optional[str] = None
    name: Optional[str] = None
    unit: Optional[str] = ""
    sensor_id: Optional[str] = None
    min: Optional[Any] = None          # alarm alt esigi (opsiyonel)
    max: Optional[Any] = None          # alarm ust esigi (opsiyonel)
    expr: Optional[str] = None         # HESAP sensoru formulu (opsiyonel, '=' olmadan)
    # --- SENSOR BAZLI KAYNAK (her biri opsiyonel ve BIRBIRINDEN BAGIMSIZ) ---
    db: Optional[str] = None           # verilmezse genel ayardaki db
    measurement: Optional[str] = None  # verilmezse genel ayardaki measurement
    meas: Optional[str] = None         # measurement icin kisa takma ad
    influx_url: Optional[str] = None   # verilmezse genel ayardaki influx_url
    url: Optional[str] = None          # influx_url icin kisa takma ad

    def resolved_influx(self) -> str:
        return (self.influx_name or self.sensor_id or "").strip()

    def resolved_meas(self) -> Optional[str]:
        return self.measurement if self.measurement is not None else self.meas

    def resolved_url(self) -> Optional[str]:
        return self.influx_url if self.influx_url is not None else self.url


class BulkSensorsIn(BaseModel):
    sensors: List[SensorIn] = []
    replace: bool = True


class ConfigIn(BaseModel):
    influx_url: Optional[str] = None
    db: Optional[str] = None
    measurement: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    refresh_ms: Optional[int] = None


def vendor_tags() -> str:
    """/vendor icinde GERCEK uPlot varsa onu yukle; yoksa hicbir dis istek yapilmaz
    ve gomulu cizim motoru devreye girer (404/CDN bekleme olmaz)."""
    tags = []
    if os.path.exists(os.path.join(VENDOR_DIR, "uPlot.min.css")):
        tags.append('<link rel="stylesheet" href="/vendor/uPlot.min.css">')
    if os.path.exists(os.path.join(VENDOR_DIR, "uPlot.iife.min.js")):
        tags.append('<script src="/vendor/uPlot.iife.min.js"></script>')
    return "\n".join(tags)


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML.replace("<!--VENDOR-->", vendor_tags()))


@app.get("/api/meta")
async def api_meta() -> JSONResponse:
    return JSONResponse({
        "ranges": [{"key": r["key"], "label": r["label"], "interval": auto_interval(r["seconds"])} for r in RANGE_TABLE],
        "refresh_ms": CONFIG.get("refresh_ms", 1000),
        "measurement": CONFIG.get("measurement", "telemetry"),
    })


# ---------------------------------- SENSOR POOL -------------------------------------------

@app.get("/api/sensors")
async def api_sensors() -> JSONResponse:
    return JSONResponse({"sensors": state["pool_sensors"]})


def upsert_sensor(sid: str, name: str, unit: str,
                  amin: Any = KEEP, amax: Any = KEEP, expr: Any = KEEP,
                  manual: Any = KEEP,
                  db: Any = KEEP, meas: Any = KEEP, url: Any = KEEP,
                  top: bool = False) -> bool:
    """Havuza ekler/gunceller. True donerse YENI kayittir.
       amin/amax    = KEEP verilirse mevcut esik degerlerine DOKUNULMAZ.
       expr         = KEEP verilirse mevcut formule DOKUNULMAZ, None ise SILINIR.
       manual       = kart arayuzden olusturuldu (formul satiri gosterilir).
       db/meas/url  = sensor bazli kaynak; KEEP dokunmaz, None gecersiz kilmayi
                      KALDIRIR (genel ayara doner). Ucu birbirinden bagimsizdir.
       top          = YENI kayit listenin BASINA eklenir (arayuzden eklenen kartlar);
                      HMI webhook'u ve preset yuklemesi sona ekler (sira korunur)."""
    for item in state["pool_sensors"]:
        if item["sensor_id"] == sid:
            if name:
                item["name"] = name
            if unit:
                item["unit"] = unit
            if amin is not KEEP:
                item["amin"] = amin
            if amax is not KEEP:
                item["amax"] = amax
            if expr is not KEEP:
                item["expr"] = expr
            if manual is not KEEP:
                item["manual"] = bool(manual)
            if db is not KEEP:
                item["db"] = db
            if meas is not KEEP:
                item["meas"] = meas
            if url is not KEEP:
                item["url"] = url
            item.setdefault("amin", None)
            item.setdefault("amax", None)
            item.setdefault("expr", None)
            item.setdefault("manual", False)
            item.setdefault("db", None)
            item.setdefault("meas", None)
            item.setdefault("url", None)
            item["ts"] = time.time()
            return False
    rec = {
        "sensor_id": sid, "name": name, "unit": unit,
        "amin": None if amin is KEEP else amin,
        "amax": None if amax is KEEP else amax,
        "expr": None if expr is KEEP else expr,
        "manual": False if manual is KEEP else bool(manual),
        "db": None if db is KEEP else db,
        "meas": None if meas is KEEP else meas,
        "url": None if url is KEEP else url,
        "ts": time.time(),
    }
    if top:
        state["pool_sensors"].insert(0, rec)
    else:
        state["pool_sensors"].append(rec)
    return True


async def _add_sensor(sid: str, name: str, unit: str,
                      raw_min: Any = KEEP, raw_max: Any = KEEP,
                      raw_expr: Any = KEEP, manual: Any = KEEP,
                      raw_db: Any = KEEP, raw_meas: Any = KEEP,
                      raw_url: Any = KEEP) -> Dict[str, Any]:
    """POST ve GET webhook'larinin ORTAK govdesi."""
    sid = (sid or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="influx_name zorunludur.")
    if not valid_sensor(sid):
        raise HTTPException(status_code=400, detail="Gecersiz influx_name (izinli: A-Z a-z 0-9 _ . -)")
    name = (name or "").strip()
    unit = (unit or "").strip()
    expr = parse_expr(raw_expr)
    if expr is not KEEP and expr and manual is KEEP:
        manual = True          # formul gelmisse kart her zaman arayuz kartidir
    created = upsert_sensor(sid, name, unit,
                            parse_limit(raw_min), parse_limit(raw_max), expr, manual,
                            parse_source(raw_db),
                            parse_source(raw_meas),
                            parse_source(raw_url, is_url=True),
                            top=manual is True)
    await hub.broadcast(pool_payload())
    rec = next((s for s in state["pool_sensors"] if s["sensor_id"] == sid), {})
    eff_url, eff_db, eff_meas = sensor_source(sid)
    return {
        "ok": True, "created": created,
        "influx_name": sid, "name": name, "unit": unit,
        "min": rec.get("amin"), "max": rec.get("amax"),
        "expr": rec.get("expr"), "manual": rec.get("manual", False),
        # kartin KENDI gecersiz kilmasi (bos = genel ayar kullaniliyor)
        "db": rec.get("db"), "measurement": rec.get("meas"), "influx_url": rec.get("url"),
        # bu sensor icin GERCEKTEN kullanilacak kaynak
        "effective": {"influx_url": eff_url, "db": eff_db, "measurement": eff_meas},
        "count": len(state["pool_sensors"]),
        "sensors": state["pool_sensors"],
    }


@app.post("/api/webhook/sensor")
async def api_webhook(payload: SensorIn) -> JSONResponse:
    return JSONResponse(await _add_sensor(
        payload.resolved_influx(), payload.name or "", payload.unit or "",
        KEEP if payload.min is None else payload.min,
        KEEP if payload.max is None else payload.max,
        KEEP if payload.expr is None else payload.expr,
        KEEP,
        KEEP if payload.db is None else payload.db,
        KEEP if payload.resolved_meas() is None else payload.resolved_meas(),
        KEEP if payload.resolved_url() is None else payload.resolved_url(),
    ))


@app.get("/api/webhook/sensor")
async def api_webhook_get(
    influx_name: str = "",
    sensor_id: str = "",
    id: str = "",
    name: str = "",
    unit: str = "",
    min: Optional[str] = None,
    max: Optional[str] = None,
    expr: Optional[str] = None,
    manual: Optional[int] = None,
    db: Optional[str] = None,
    measurement: Optional[str] = None,
    meas: Optional[str] = None,
    influx_url: Optional[str] = None,
    url: Optional[str] = None,
) -> JSONResponse:
    """GOVDESIZ webhook -- her sey query string'de.

       http://SUNUCU:8989/api/webhook/sensor?influx_name=P1&name=Pompa&unit=Bar&min=0&max=250

       HESAP sensoru (arayuzdeki '+ Yeni' kartinin formul satiri):
       ...?influx_name=CALC_GUC&name=Guc&unit=kW&expr=P1*N5/9550

       SENSOR BAZLI KAYNAK -- db / measurement / influx_url:
       ...?influx_name=P1&db=hat2                      -> yalnizca db degisir
       ...?influx_name=P1&measurement=press            -> yalnizca measurement degisir
       ...?influx_name=P1&db=hat2&measurement=press    -> ikisi birden
       ...?influx_name=P1&influx_url=http://10.0.0.9:8086
       Gonderilmeyen alan GENEL AYARDAN okunmaya devam eder; ucu birbirini
       BEKLEMEZ. Bos gondermek (db=) o gecersiz kilmayi KALDIRIR.
       'meas' ve 'url' kisa takma adlari da kabul edilir.

       Content-Type header'i GEREKMEZ; link dogrudan tarayiciya yazilabilir."""
    return JSONResponse(await _add_sensor(
        influx_name or sensor_id or id, name, unit, min, max, expr,
        KEEP if manual is None else bool(manual),
        KEEP if db is None else db,
        KEEP if (measurement is None and meas is None) else (measurement if measurement is not None else meas),
        KEEP if (influx_url is None and url is None) else (influx_url if influx_url is not None else url),
    ))


@app.get("/api/sensors/{sensor_id}/source")
@app.post("/api/sensors/{sensor_id}/source")
async def api_set_source(sensor_id: str,
                         db: Optional[str] = None,
                         measurement: Optional[str] = None,
                         meas: Optional[str] = None,
                         influx_url: Optional[str] = None,
                         url: Optional[str] = None) -> JSONResponse:
    """Mevcut bir kartin kaynagini degistirir (esikler gibi, tek tek).
       Parametre HIC verilmezse o alana dokunulmaz; BOS verilirse gecersiz
       kilma kalkar ve genel ayara donulur.
         /api/sensors/P1/source?db=hat2        -> sadece db
         /api/sensors/P1/source?measurement=   -> sadece measurement'i sifirla"""
    if not valid_sensor(sensor_id):
        raise HTTPException(status_code=400, detail="Gecersiz sensor adi.")
    if not any(s["sensor_id"] == sensor_id for s in state["pool_sensors"]):
        raise HTTPException(status_code=404, detail="Sensor havuzda yok.")
    m = measurement if measurement is not None else meas
    u = influx_url if influx_url is not None else url
    upsert_sensor(sensor_id, "", "", KEEP, KEEP, KEEP, KEEP,
                  parse_source(KEEP if db is None else db),
                  parse_source(KEEP if m is None else m),
                  parse_source(KEEP if u is None else u, is_url=True))
    await hub.broadcast(pool_payload())
    rec = next((s for s in state["pool_sensors"] if s["sensor_id"] == sensor_id), {})
    eff_url, eff_db, eff_meas = sensor_source(sensor_id)
    return JSONResponse({
        "ok": True, "sensor_id": sensor_id,
        "db": rec.get("db"), "measurement": rec.get("meas"), "influx_url": rec.get("url"),
        "effective": {"influx_url": eff_url, "db": eff_db, "measurement": eff_meas},
        "sensors": state["pool_sensors"],
    })


@app.get("/api/sensors/{sensor_id}/expr")
@app.post("/api/sensors/{sensor_id}/expr")
async def api_set_expr(sensor_id: str, expr: Optional[str] = None) -> JSONResponse:
    """Mevcut bir kartin formulunu degistirir. expr='' formulu siler."""
    if not valid_sensor(sensor_id):
        raise HTTPException(status_code=400, detail="Gecersiz sensor adi.")
    rec = next((s for s in state["pool_sensors"] if s["sensor_id"] == sensor_id), None)
    if rec is None:
        raise HTTPException(status_code=404, detail="Sensor havuzda yok.")
    parsed = parse_expr(expr)
    if parsed is not KEEP:
        rec["expr"] = parsed
        rec["manual"] = True
        rec["ts"] = time.time()
        await hub.broadcast(pool_payload())
    return JSONResponse({"ok": True, "sensor_id": sensor_id, "expr": rec.get("expr"),
                         "sensors": state["pool_sensors"]})


@app.get("/api/webhook/sensors")
async def api_webhook_get_bulk(s: str = "", replace: int = 0) -> JSONResponse:
    """TEK URL ile COKLU sensor ekleme. Alanlar '|', kayitlar ',' ile ayrilir:

       /api/webhook/sensors?s=P1|Pompa Basinci|Bar|0|250,T3|Yag Sicakligi|C|-10|120

       Sira: influx_name | name | unit | min | max | db | measurement | influx_url
       (sondakiler atlanabilir). db/measurement/influx_url verilmezse o sensor
       icin GENEL AYAR kullanilir; ucu birbirinden bagimsizdir:
       /api/webhook/sensors?s=P1|Pompa|Bar|0|250|hat2,T3|Sicaklik|C|||press
       replace=1 verilirse havuz once tamamen temizlenir."""
    if replace:
        state["pool_sensors"] = []
    added: List[str] = []
    skipped: List[str] = []
    for chunk in (s or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = [p.strip() for p in chunk.split("|")]
        sid = parts[0] if parts else ""
        if not valid_sensor(sid):
            skipped.append(sid or chunk)
            continue
        upsert_sensor(
            sid,
            parts[1] if len(parts) > 1 else "",
            parts[2] if len(parts) > 2 else "",
            parse_limit(parts[3]) if len(parts) > 3 else KEEP,
            parse_limit(parts[4]) if len(parts) > 4 else KEEP,
            KEEP, KEEP,
            parse_source(parts[5]) if len(parts) > 5 else KEEP,
            parse_source(parts[6]) if len(parts) > 6 else KEEP,
            parse_source(parts[7], is_url=True) if len(parts) > 7 else KEEP,
        )
        added.append(sid)
    await hub.broadcast(pool_payload())
    return JSONResponse({
        "ok": True, "added": added, "skipped": skipped,
        "count": len(state["pool_sensors"]), "sensors": state["pool_sensors"],
    })


@app.get("/api/sensors/{sensor_id}/limits")
@app.post("/api/sensors/{sensor_id}/limits")
async def api_set_limits(sensor_id: str,
                         min: Optional[str] = None,
                         max: Optional[str] = None) -> JSONResponse:
    """Alarm esiklerini ayarlar.
       Parametre HIC verilmezse o esige dokunulmaz; BOS verilirse esik kaldirilir.
       /api/sensors/P1/limits?min=0&max=250   -> ikisini de ayarla
       /api/sensors/P1/limits?min=            -> SADECE alt esigi kaldir"""
    if not valid_sensor(sensor_id):
        raise HTTPException(status_code=400, detail="Gecersiz sensor adi.")
    found = any(s["sensor_id"] == sensor_id for s in state["pool_sensors"])
    if not found:
        raise HTTPException(status_code=404, detail="Sensor havuzda yok.")
    upsert_sensor(sensor_id, "", "", parse_limit(min), parse_limit(max))
    await hub.broadcast(pool_payload())
    rec = next((s for s in state["pool_sensors"] if s["sensor_id"] == sensor_id), {})
    return JSONResponse({"ok": True, "sensor_id": sensor_id,
                         "min": rec.get("amin"), "max": rec.get("amax"),
                         "sensors": state["pool_sensors"]})


@app.post("/api/sensors/bulk")
async def api_bulk_sensors(payload: BulkSensorsIn) -> JSONResponse:
    """Preset yuklemede havuzu topluca geri yukler."""
    if payload.replace:
        state["pool_sensors"] = []
    skipped: List[str] = []
    for item in payload.sensors:
        sid = item.resolved_influx()
        if not valid_sensor(sid):
            skipped.append(sid)
            continue
        expr = parse_expr(item.expr)
        upsert_sensor(sid, (item.name or "").strip(), (item.unit or "").strip(),
                      parse_limit(item.min), parse_limit(item.max), expr,
                      True if (expr is not KEEP and expr) else KEEP,
                      parse_source(KEEP if item.db is None else item.db),
                      parse_source(KEEP if item.resolved_meas() is None else item.resolved_meas()),
                      parse_source(KEEP if item.resolved_url() is None else item.resolved_url(),
                                   is_url=True))
    await hub.broadcast(pool_payload())
    return JSONResponse({"ok": True, "sensors": state["pool_sensors"], "skipped": skipped})


@app.delete("/api/sensors/{sensor_id}")
async def api_delete_sensor(sensor_id: str) -> JSONResponse:
    before = len(state["pool_sensors"])
    state["pool_sensors"] = [s for s in state["pool_sensors"] if s["sensor_id"] != sensor_id]
    await hub.broadcast(pool_payload())
    return JSONResponse({
        "ok": True,
        "removed": before - len(state["pool_sensors"]),
        "sensors": state["pool_sensors"],
    })


@app.get("/api/sensors/order")
@app.post("/api/sensors/order")
async def api_order_sensors(ids: str = "") -> JSONResponse:
    """Sol paneldeki kart SIRASI (surukle-birak). ids=A,B,C verilen sirayla basa
       dizilir; listede olmayan kartlar mevcut siralariyla arkaya eklenir."""
    wanted = [s.strip() for s in ids.split(",") if s.strip()]
    by_id = {s["sensor_id"]: s for s in state["pool_sensors"]}
    head = []
    for sid in wanted:
        rec = by_id.pop(sid, None)
        if rec is not None:
            head.append(rec)
    tail = [s for s in state["pool_sensors"] if s["sensor_id"] in by_id]
    state["pool_sensors"] = head + tail
    await hub.broadcast(pool_payload())
    return JSONResponse({"ok": True, "sensors": state["pool_sensors"]})


class RenameIn(BaseModel):
    to: str = ""
    name: Optional[str] = None
    unit: Optional[str] = None
    expr: Optional[str] = None                   # None = formule dokunma, "" = sil
    exprs: Optional[Dict[str, str]] = None       # baska kartlarin GUNCELLENMIS formulleri


@app.post("/api/sensors/{sensor_id}/rename")
async def api_rename_sensor(sensor_id: str, payload: RenameIn) -> JSONResponse:
    """Kartin KIMLIGINI yerinde degistirir (sira, esik, kaynak korunur).
       Hesap karti adi degisince degisken adi da degisir; bu kimligi kullanan
       diger formuller arayuzde yeniden yazilip 'exprs' ile AYNI istekte gelir,
       boylece tum ekranlar tek bir guncelleme gorur."""
    new_id = (payload.to or "").strip()
    if not valid_sensor(sensor_id) or not valid_sensor(new_id):
        raise HTTPException(status_code=400, detail="Gecersiz ad (izinli: A-Z a-z 0-9 _ . -)")
    rec = next((s for s in state["pool_sensors"] if s["sensor_id"] == sensor_id), None)
    if rec is None:
        raise HTTPException(status_code=404, detail="Sensor havuzda yok.")
    if new_id != sensor_id and any(s["sensor_id"] == new_id for s in state["pool_sensors"]):
        raise HTTPException(status_code=409, detail='"{0}" zaten havuzda.'.format(new_id))
    rec["sensor_id"] = new_id
    if payload.name is not None:
        rec["name"] = payload.name.strip()
    if payload.unit is not None:
        rec["unit"] = payload.unit.strip()
    expr = parse_expr(payload.expr)
    if expr is not KEEP:
        rec["expr"] = expr
        rec["manual"] = True
    rec["ts"] = time.time()
    for sid, text in (payload.exprs or {}).items():
        other = next((s for s in state["pool_sensors"] if s["sensor_id"] == sid), None)
        parsed = parse_expr(text)
        if other is not None and parsed is not KEEP:
            other["expr"] = parsed
    await hub.broadcast(pool_payload())
    return JSONResponse({"ok": True, "sensor_id": new_id, "sensors": state["pool_sensors"]})


@app.post("/api/sensors/clear")
async def api_clear_sensors() -> JSONResponse:
    state["pool_sensors"] = []
    await hub.broadcast(pool_payload())
    return JSONResponse({"ok": True, "sensors": []})


MOCK_POOL = [
    ("MOCK_PRESSURE_P1", "Pompa Basinci", "Bar"),
    ("MOCK_FLOW_Q2", "Debi", "L/min"),
    ("MOCK_TEMP_T3", "Yag Sicakligi", "C"),
    ("MOCK_TORQUE_M4", "Tork", "Nm"),
    ("MOCK_RPM_N5", "Devir", "rpm"),
    ("MOCK_VIBRATION_V6", "Titresim", "mm/s"),
]


@app.post("/api/mock/trigger")
async def api_mock_trigger() -> JSONResponse:
    existing = {s["sensor_id"] for s in state["pool_sensors"]}
    added = []
    for sid, name, unit in MOCK_POOL:
        if sid not in existing:
            upsert_sensor(sid, name, unit)
            added.append(sid)
        if len(added) >= 3:
            break
    if not added:  # hepsi zaten havuzdaysa tek bir rastgele yeni tanimla
        sid = "MOCK_AUX_" + str(random.randint(100, 999))
        upsert_sensor(sid, "Yardimci Kanal", "V")
        added.append(sid)
    await hub.broadcast(pool_payload())
    return JSONResponse({"ok": True, "added": added, "sensors": state["pool_sensors"]})


# ---------------------------------- CONFIG ------------------------------------------------

@app.get("/api/config")
async def api_get_config() -> JSONResponse:
    return JSONResponse(CONFIG)


@app.post("/api/config")
async def api_set_config(payload: ConfigIn) -> JSONResponse:
    global CONFIG
    CONFIG = save_config({k: v for k, v in payload.dict().items() if v is not None})
    return JSONResponse(CONFIG)


async def _ping(root: str) -> Dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(str(root).rstrip("/") + "/ping")
        return {"url": root, "ok": resp.status_code in (200, 204),
                "status": resp.status_code,
                "version": resp.headers.get("X-Influxdb-Version", "")}
    except Exception as exc:
        return {"url": root, "ok": False,
                "error": "{0}: {1}".format(type(exc).__name__, exc)}


@app.get("/api/health")
async def api_health() -> JSONResponse:
    """Genel ayardaki sunucu + havuzdaki sensorlerin KENDI sunuculari.
       Boylece bir sensor baska bir InfluxDB'ye bakiyorsa ve O sunucu duserse
       durum cubugu bunu gorebilir."""
    main_url = str(CONFIG["influx_url"]).rstrip("/")
    extra = []
    for s in state["pool_sensors"]:
        u = str(s.get("url") or "").rstrip("/")
        if u and u != main_url and u not in extra:
            extra.append(u)

    primary = await _ping(main_url)
    body: Dict[str, Any] = dict(primary)
    if extra:
        others = [await _ping(u) for u in extra]
        body["sources"] = [primary] + others
        body["all_ok"] = all(x.get("ok") for x in body["sources"])
    return JSONResponse(body)


# ---------------------------------- GLOBAL PRESETS ----------------------------------------
# Presetler SUNUCUDA presets.json icinde tutulur. Boylece hangi PC / hangi tarayici
# olursa olsun ayni preset listesi gorunur. Preset icerigindeki sensor havuzu ise
# istemcide GECICI olarak kullanilir; sunucudaki webhook havuzuna ASLA yazilmaz.

@app.get("/api/presets")
async def api_presets_list() -> JSONResponse:
    return JSONResponse({"names": preset_names()})


@app.get("/api/presets/{name}")
async def api_preset_get(name: str) -> JSONResponse:
    data = load_presets()
    if name not in data:
        raise HTTPException(status_code=404, detail="Preset bulunamadi: " + name)
    return JSONResponse({"ok": True, "name": name, "preset": data[name]})


@app.post("/api/presets/{name}")
async def api_preset_save(name: str, payload: Dict[str, Any]) -> JSONResponse:
    name = (name or "").strip()
    if not valid_preset_name(name):
        raise HTTPException(status_code=400, detail="Gecersiz preset adi.")
    with _PRESET_LOCK:
        data = load_presets()
        data[name] = payload
        write_presets(data)
    await hub.broadcast(preset_payload())
    return JSONResponse({"ok": True, "name": name, "names": preset_names()})


@app.delete("/api/presets/{name}")
async def api_preset_delete(name: str) -> JSONResponse:
    with _PRESET_LOCK:
        data = load_presets()
        existed = name in data
        if existed:
            del data[name]
            write_presets(data)
    await hub.broadcast(preset_payload())
    return JSONResponse({"ok": True, "removed": existed, "names": preset_names()})


class MaterialIn(BaseModel):
    csv: str = ""
    dry: bool = False          # True: yalnizca dogrula / onizle, kaydetme


@app.get("/api/materials")
async def api_materials() -> JSONResponse:
    return JSONResponse({"materials": load_materials(),
                         "columns": [{"col": c, "unit": u} for c, u in MAT_COLS]})


@app.post("/api/materials/{name}")
async def api_material_save(name: str, payload: MaterialIn) -> JSONResponse:
    """CSV'den malzeme tablosu. Ad formulde onek olur: <ad>_dens(sicaklik)."""
    name = (name or "").strip()
    if not MAT_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Gecersiz ad: harfle baslamali; izinli A-Z a-z 0-9 _ (en fazla 32).")
    try:
        rows = parse_material_csv(payload.csv)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    rec = {"cols": [c for c, _ in MAT_COLS], "rows": rows, "ts": time.time()}
    if payload.dry:
        return JSONResponse({"ok": True, "name": name, "material": rec})
    with _MAT_LOCK:
        data = load_materials()
        for old in [k for k in data if k.lower() == name.lower()]:
            del data[old]                      # ayni ad (buyuk/kucuk harf farki) -> uzerine yaz
        data[name] = rec
        write_materials(data)
    await hub.broadcast(material_payload())
    return JSONResponse({"ok": True, "name": name, "material": rec, "materials": load_materials()})


@app.delete("/api/materials/{name}")
async def api_material_delete(name: str) -> JSONResponse:
    with _MAT_LOCK:
        data = load_materials()
        removed = [k for k in data if k.lower() == (name or "").strip().lower()]
        for k in removed:
            del data[k]
        if removed:
            write_materials(data)
    await hub.broadcast(material_payload())
    return JSONResponse({"ok": True, "removed": len(removed), "materials": load_materials()})


class PresetImportIn(BaseModel):
    presets: Dict[str, Any] = {}


@app.post("/api/presets_import")
async def api_preset_import(payload: PresetImportIn) -> JSONResponse:
    """Eski surumden kalan localStorage presetlerini bir defaliga sunucuya tasir.
       Ayni isimde sunucu preseti varsa DOKUNMAZ (veri kaybi olmaz)."""
    added: List[str] = []
    with _PRESET_LOCK:
        data = load_presets()
        for key, val in (payload.presets or {}).items():
            key = (key or "").strip()
            if not valid_preset_name(key) or key in data:
                continue
            data[key] = val
            added.append(key)
        if added:
            write_presets(data)
    if added:
        await hub.broadcast(preset_payload())
    return JSONResponse({"ok": True, "added": added, "names": preset_names()})


# ---------------------------------- DATA (InfluxQL) ---------------------------------------

@app.get("/api/data")
async def api_data(sensors: str = "", range: str = "5m", fine: int = 0) -> JSONResponse:
    range_key = range if range in RANGE_MAP else "5m"
    seconds = RANGE_MAP[range_key]["seconds"]
    base_ms = auto_interval_ms(seconds)
    interval_ms = fine_interval_ms(base_ms, fine)
    interval = fmt_interval(interval_ms)
    interval_sec = interval_ms / 1000.0
    fine_level = max(0, min(FINE_MAX, int(fine or 0)))

    requested = [s.strip() for s in sensors.split(",") if s.strip()]
    requested = [s for s in requested if valid_sensor(s)]
    if not requested:
        return JSONResponse({"t": [], "series": {}, "interval": interval, "base_interval": fmt_interval(base_ms),
                             "fine": fine_level, "range": range_key, "points": 0})

    real = [s for s in requested if not s.startswith("MOCK_")]
    mocks = [s for s in requested if s.startswith("MOCK_")]

    out_t: List[float] = []
    out_series: Dict[str, List[Optional[float]]] = {s: [] for s in requested}
    error: Optional[str] = None
    query_text = ""

    if real:
        # Sensorler (url, db, measurement) ucluIsune gore gruplanir: ayni kaynagi
        # paylasanlar TEK sorguda toplanir, farkli kaynaklar ayri sorgulara boluner.
        # GROUP BY time() kovalari mutlak epoch'a hizalandigi icin ayri sorgularin
        # zaman damgalari BIREBIR ortusur -> birlesim guvenle yapilabilir.
        buckets: Dict[float, Dict[str, Optional[float]]] = {}
        queries: List[str] = []
        errors: List[str] = []
        for (src_url, src_db, src_meas), sids in group_by_source(real).items():
            q = build_influxql(sids, range_key, interval, src_meas)
            queries.append(q)
            try:
                raw = await influx_query(q, src_url, src_db)
                results = (raw.get("results") or [{}])[0]
                if results.get("error"):
                    raise RuntimeError(results["error"])
                series_blocks = results.get("series") or []
                if not series_blocks:
                    continue
                block = series_blocks[0]
                columns = block.get("columns") or []
                values = block.get("values") or []
                name_to_idx = {name: idx for idx, name in enumerate(columns) if idx > 0}
                for row in values:
                    epoch = iso_to_epoch(row[0])
                    if epoch is None:
                        continue
                    slot = buckets.setdefault(epoch, {})
                    for sid in sids:
                        idx = name_to_idx.get(sid)
                        val = row[idx] if (idx is not None and idx < len(row)) else None
                        try:
                            slot[sid] = float(val) if val is not None else None
                        except (TypeError, ValueError):
                            slot[sid] = None
            except Exception as exc:
                errors.append("{0}/{1}: {2}: {3}".format(
                    src_db, src_meas, type(exc).__name__, exc))

        query_text = "\n".join(queries)
        if errors:
            error = " | ".join(errors)
        out_t = sorted(buckets)
        for sid in real:
            out_series[sid] = [buckets[ts].get(sid) for ts in out_t]

    if mocks:
        gen = {sid: mock_series(sid, seconds, interval_sec) for sid in mocks}
        if not out_t:
            first = gen[mocks[0]]
            out_t = first["t"]
            for sid in mocks:
                out_series[sid] = gen[sid]["v"][:len(out_t)]
        else:
            # gercek zaman eksenine en yakin ornegi hizala (basit ileri arama)
            for sid in mocks:
                g = gen[sid]
                aligned: List[Optional[float]] = []
                j = 0
                for tv in out_t:
                    while j + 1 < len(g["t"]) and g["t"][j + 1] <= tv:
                        j += 1
                    aligned.append(g["v"][j] if g["t"] else None)
                out_series[sid] = aligned

    # her serinin uzunlugunu zaman eksenine esitle (uPlot katı format ister)
    n = len(out_t)
    for sid in requested:
        arr = out_series.get(sid) or []
        if len(arr) < n:
            arr = arr + [None] * (n - len(arr))
        elif len(arr) > n:
            arr = arr[:n]
        out_series[sid] = arr

    body: Dict[str, Any] = {
        "t": out_t,
        "series": out_series,
        "interval": interval,
        "base_interval": fmt_interval(base_ms),
        "fine": fine_level,
        "range": range_key,
        "points": n,
        "query": query_text,
    }
    if error:
        body["error"] = error
    return JSONResponse(body)


@app.get("/api/last")
async def api_last(sensors: str = "") -> JSONResponse:
    """ALARM IZLEYICI icin cok hafif uc nokta: sensor basina SADECE son deger.
       Grafik verisinden bagimsizdir; grafikte olmayan / PAUSE edilmis
       sensorlerin esikleri de boylece izlenmeye devam eder."""
    requested = [s.strip() for s in sensors.split(",") if s.strip()]
    requested = [s for s in requested if valid_sensor(s)]
    if not requested:
        return JSONResponse({"values": {}})

    values: Dict[str, Optional[float]] = {s: None for s in requested}
    error: Optional[str] = None

    real = [s for s in requested if not s.startswith("MOCK_")]
    for sid in (s for s in requested if s.startswith("MOCK_")):
        gen = mock_series(sid, 10, 1.0)
        values[sid] = gen["v"][-1] if gen["v"] else None

    if real:
        # /api/data ile ayni kaynak gruplamasi: her (url, db, measurement) icin bir sorgu
        errors: List[str] = []
        for (src_url, src_db, src_meas), sids in group_by_source(real).items():
            selects = ", ".join('last("{0}") AS "{0}"'.format(s) for s in sids)
            query = 'SELECT {sel} FROM "{meas}" WHERE time > now() - 10m'.format(
                sel=selects, meas=src_meas)
            try:
                raw = await influx_query(query, src_url, src_db)
                results = (raw.get("results") or [{}])[0]
                if results.get("error"):
                    raise RuntimeError(results["error"])
                blocks = results.get("series") or []
                if not blocks:
                    continue
                columns = blocks[0].get("columns") or []
                rows = blocks[0].get("values") or []
                if not rows:
                    continue
                row = rows[-1]
                for idx, col in enumerate(columns):
                    if idx == 0 or col not in values:
                        continue
                    try:
                        values[col] = float(row[idx]) if row[idx] is not None else None
                    except (TypeError, ValueError):
                        values[col] = None
            except Exception as exc:
                errors.append("{0}/{1}: {2}: {3}".format(
                    src_db, src_meas, type(exc).__name__, exc))
        if errors:
            error = " | ".join(errors)

    body: Dict[str, Any] = {"values": values}
    if error:
        body["error"] = error
    return JSONResponse(body)


# ---------------------------------- WEBSOCKET ---------------------------------------------

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await hub.connect(ws)
    try:
        await ws.send_text(json.dumps(pool_payload()))
        await ws.send_text(json.dumps(preset_payload()))
        await ws.send_text(json.dumps(material_payload()))
        while True:
            await ws.receive_text()   # ping/keepalive; icerik onemli degil
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await hub.disconnect(ws)


# ==========================================================================================
# 9) ENTRYPOINT
# ==========================================================================================

class _LogStream(object):
    """print()/stderr ciktilarini log dosyasina yonlendirir (--noconsole'da stdout None)."""

    def __init__(self, logger: logging.Logger, level: int) -> None:
        self.logger, self.level, self._buf = logger, level, ""

    def write(self, text: str) -> int:
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self.logger.log(self.level, line.rstrip())
        return len(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


def setup_file_logging() -> None:
    """Konsol yoksa (exe / Windows servisi) tum loglari LOG_DIR altina doner dosyaya yaz."""
    os.makedirs(LOG_DIR, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        os.path.join(LOG_DIR, "benchmonitor.log"),
        maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    logging.captureWarnings(True)
    sys.stdout = _LogStream(logging.getLogger("stdout"), logging.INFO)
    sys.stderr = _LogStream(logging.getLogger("stderr"), logging.ERROR)


def listen_sockets(port: int) -> List[socket.socket]:
    """IPv4 (0.0.0.0) + varsa IPv6 (::). Yalnizca IPv4 dinlenirse tarayici
       'localhost'u once ::1 olarak dener; Windows'ta reddedilen bu deneme her yeni
       baglantida gecikme (300 ms .. 2 sn) yaratir. IPv6 soketi V6ONLY acilir ki
       IPv4 soketiyle cakismasin."""
    socks: List[socket.socket] = []
    targets = [(socket.AF_INET, "0.0.0.0")]
    if socket.has_ipv6:
        targets.append((socket.AF_INET6, "::"))
    for family, host in targets:
        sock = None
        try:
            sock = socket.socket(family, socket.SOCK_STREAM)
            if sys.platform != "win32":      # Windows'ta REUSEADDR ayni portu ikinci surece de acar
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6:
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            sock.bind((host, port))
            sock.set_inheritable(True)
            socks.append(sock)
        except OSError as exc:            # IPv6 kapali makinede soket hic olusmayabilir
            if sock is not None:
                sock.close()
            if family == socket.AF_INET:
                raise                          # IPv4 acilamiyorsa (port dolu) baslamasin
            print("[NET] IPv6 dinlenemedi, yalnizca IPv4:", exc)
    return socks


if __name__ == "__main__":
    file_logging = FROZEN or sys.stdout is None or sys.stderr is None
    if file_logging:
        setup_file_logging()
    port = get_port()
    print("=" * 78)
    print(" TEST BENCH MONITOR  ->  http://0.0.0.0:{0}".format(port))
    print(" InfluxDB : {0}  |  db={1}  |  measurement={2}".format(
        CONFIG["influx_url"], CONFIG["db"], CONFIG.get("measurement")))
    print(" Data     : {0}".format(BASE_DIR))
    print("=" * 78)
    opts: Dict[str, Any] = {"host": "0.0.0.0", "port": port, "log_level": "info", "ws_ping_interval": 20}
    if file_logging:
        # log_config=None: uvicorn kendi (konsol) handler'larini kurmaz, kok logger'a akar.
        # access_log kapali: her yoklama bir satir olur, log dosyasini bogar.
        opts.update(log_config=None, access_log=False)
    uvicorn.Server(uvicorn.Config(app, **opts)).run(sockets=listen_sockets(port))
