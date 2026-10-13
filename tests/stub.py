# -*- coding: utf-8 -*-
"""main.py icindeki arayuzu FastAPI olmadan test etmek icin stdlib sahte sunucu."""
import re, json, math, time, random, threading, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

SRC = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'main.py'), encoding='utf-8').read()
INDEX_HTML = re.search(r'INDEX_HTML = r"""(.*?)"""\n', SRC, re.S).group(1).replace('<!--VENDOR-->', '')

RANGE_TABLE = [
    {"key": "5m", "label": "5 Dk", "seconds": 300}, {"key": "10m", "label": "10 Dk", "seconds": 600},
    {"key": "15m", "label": "15 Dk", "seconds": 900}, {"key": "30m", "label": "30 Dk", "seconds": 1800},
    {"key": "1h", "label": "1 Saat", "seconds": 3600}, {"key": "2h", "label": "2 Saat", "seconds": 7200},
    {"key": "3h", "label": "3 Saat", "seconds": 10800}, {"key": "4h", "label": "4 Saat", "seconds": 14400},
    {"key": "5h", "label": "5 Saat", "seconds": 18000}, {"key": "10h", "label": "10 Saat", "seconds": 36000},
]
RMAP = {r["key"]: r for r in RANGE_TABLE}
FINE_MAX = 3
MIN_MS = 100
KEEP = object()

# testlerin alarm tetiklemek icin zorlayabildigi sabit degerler
FORCED = {}


def base_ms(sec):
    return 1000 if sec <= 600 else 2000 if sec <= 1800 else 5000 if sec <= 7200 else 15000 if sec <= 18000 else 30000


def fine_ms(b, f):
    try: f = int(f)
    except Exception: f = 0
    f = max(0, min(FINE_MAX, f))
    return max(MIN_MS, int(round(b / float(2 ** f))))


def fmt_ivl(ms):
    return "%ds" % (ms // 1000) if ms % 1000 == 0 else "%dms" % ms


def parse_limit(raw):
    if raw is KEEP or raw is None:
        return KEEP
    t = str(raw).strip()
    if t == "" or t.lower() in ("null", "none", "-"):
        return None
    try:
        v = float(t.replace(",", "."))
    except Exception:
        return KEEP
    return v if math.isfinite(v) else None


POOL = []
PRESETS = {}
LOCK = threading.Lock()


def upsert(sid, name, unit, amin=KEEP, amax=KEEP):
    for it in POOL:
        if it["sensor_id"] == sid:
            if name: it["name"] = name
            if unit: it["unit"] = unit
            if amin is not KEEP: it["amin"] = amin
            if amax is not KEEP: it["amax"] = amax
            it.setdefault("amin", None); it.setdefault("amax", None)
            return False
    POOL.append({"sensor_id": sid, "name": name, "unit": unit,
                 "amin": None if amin is KEEP else amin,
                 "amax": None if amax is KEEP else amax,
                 "ts": time.time()})
    return True


def pnames():
    return sorted(PRESETS.keys(), key=lambda s: s.lower())


def wave(sid, tv):
    seed = sum(ord(c) for c in sid); amp = 10 + seed % 90; base = seed % 40
    fr = 1.0 / (60.0 + seed % 240); rnd = random.Random(seed)
    return round(base + amp * math.sin(2 * math.pi * fr * tv) + rnd.uniform(-amp * .03, amp * .03), 4)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, obj, code=200, html=False):
        body = obj.encode('utf-8') if html else json.dumps(obj).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'text/html; charset=utf-8' if html else 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        try: return json.loads(self.rfile.read(n) or b'{}')
        except Exception: return {}

    def _one(self, q, key, dflt=None):
        v = q.get(key)
        return v[0] if v else dflt

    # ------------------------------------------------------------------ GET
    def do_GET(self):
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query, keep_blank_values=True)
        if p == '/':
            return self._send(INDEX_HTML, html=True)
        if p == '/api/meta':
            return self._send({"ranges": [{"key": r["key"], "label": r["label"],
                                           "interval": fmt_ivl(base_ms(r["seconds"]))} for r in RANGE_TABLE],
                               "refresh_ms": 1000, "measurement": "telemetry"})
        if p == '/api/sensors':
            return self._send({"sensors": POOL})
        if p == '/api/presets':
            return self._send({"names": pnames()})
        if p == '/api/webhook/sensor':
            with LOCK:
                sid = (self._one(q, 'influx_name') or self._one(q, 'sensor_id') or self._one(q, 'id') or '').strip()
                if not sid: return self._send({"detail": "influx_name zorunludur."}, 400)
                created = upsert(sid, (self._one(q, 'name') or '').strip(), (self._one(q, 'unit') or '').strip(),
                                 parse_limit(self._one(q, 'min')), parse_limit(self._one(q, 'max')))
                rec = next((s for s in POOL if s["sensor_id"] == sid), {})
                return self._send({"ok": True, "created": created, "influx_name": sid,
                                   "min": rec.get("amin"), "max": rec.get("amax"), "count": len(POOL)})
        if p == '/api/webhook/sensors':
            with LOCK:
                if self._one(q, 'replace') in ('1', 'true'): POOL[:] = []
                added, skipped = [], []
                for chunk in (self._one(q, 's') or '').split(','):
                    chunk = chunk.strip()
                    if not chunk: continue
                    parts = [x.strip() for x in chunk.split('|')]
                    sid = parts[0]
                    if not re.match(r'^[A-Za-z0-9_.\-]{1,128}$', sid or ''):
                        skipped.append(sid or chunk); continue
                    upsert(sid, parts[1] if len(parts) > 1 else '', parts[2] if len(parts) > 2 else '',
                           parse_limit(parts[3]) if len(parts) > 3 else KEEP,
                           parse_limit(parts[4]) if len(parts) > 4 else KEEP)
                    added.append(sid)
                return self._send({"ok": True, "added": added, "skipped": skipped,
                                   "count": len(POOL), "sensors": POOL})
        if p.endswith('/limits') and p.startswith('/api/sensors/'):
            return self._limits(p, q)
        if p.startswith('/api/presets/'):
            nm = unquote(p.split('/api/presets/', 1)[1])
            if nm not in PRESETS: return self._send({"detail": "yok"}, 404)
            return self._send({"ok": True, "name": nm, "preset": PRESETS[nm]})
        if p == '/api/config':
            return self._send({"influx_url": "http://10.10.2.6:8086", "db": "lc_100", "measurement": "telemetry",
                               "username": "", "password": "", "refresh_ms": 1000})
        if p == '/api/health':
            return self._send({"ok": True, "version": "2.0-stub"})
        if p == '/api/last':
            ids = [s for s in (self._one(q, 'sensors') or '').split(',') if s]
            now = time.time()
            return self._send({"values": {s: (FORCED[s] if s in FORCED else wave(s, now)) for s in ids}})
        if p == '/api/data':
            rk = (self._one(q, 'range') or '5m')
            rk = rk if rk in RMAP else '5m'
            secs = RMAP[rk]["seconds"]
            b = base_ms(secs)
            fl = self._one(q, 'fine', '0')
            ms = fine_ms(b, fl)
            iv = fmt_ivl(ms); step = ms / 1000.0
            ids = [s for s in (self._one(q, 'sensors') or '').split(',') if s]
            now = time.time(); t = []; x = now - secs
            while x <= now:
                t.append(round(x, 3)); x += step
            ser = {}
            for sid in ids:
                if sid in FORCED:
                    ser[sid] = [FORCED[sid]] * len(t)
                else:
                    ser[sid] = [wave(sid, tv) for tv in t]
            return self._send({"t": t, "series": ser, "interval": iv, "base_interval": fmt_ivl(b),
                               "fine": max(0, min(FINE_MAX, int(fl or 0))), "range": rk, "points": len(t)})
        return self._send({"error": "nf"}, 404)

    def _limits(self, p, q):
        sid = unquote(p[len('/api/sensors/'):-len('/limits')])
        with LOCK:
            if not any(s["sensor_id"] == sid for s in POOL):
                return self._send({"detail": "Sensor havuzda yok."}, 404)
            upsert(sid, "", "", parse_limit(self._one(q, 'min')), parse_limit(self._one(q, 'max')))
            rec = next((s for s in POOL if s["sensor_id"] == sid), {})
            return self._send({"ok": True, "sensor_id": sid, "min": rec.get("amin"),
                               "max": rec.get("amax"), "sensors": POOL})

    # ----------------------------------------------------------------- POST
    def do_POST(self):
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query, keep_blank_values=True)
        if p.endswith('/limits') and p.startswith('/api/sensors/'):
            return self._limits(p, q)
        b = self._body()
        with LOCK:
            if p == '/api/webhook/sensor':
                sid = (b.get('influx_name') or b.get('sensor_id') or '').strip()
                if not sid: return self._send({"detail": "influx_name zorunludur."}, 400)
                c = upsert(sid, (b.get('name') or '').strip(), (b.get('unit') or '').strip(),
                           parse_limit(b.get('min')), parse_limit(b.get('max')))
                return self._send({"ok": True, "created": c, "count": len(POOL), "sensors": POOL})
            if p == '/api/sensors/bulk':
                if b.get('replace', True): POOL[:] = []
                for it in (b.get('sensors') or []):
                    sid = (it.get('influx_name') or it.get('sensor_id') or '').strip()
                    if sid: upsert(sid, (it.get('name') or '').strip(), (it.get('unit') or '').strip(),
                                   parse_limit(it.get('min')), parse_limit(it.get('max')))
                return self._send({"ok": True, "sensors": POOL, "skipped": []})
            if p == '/api/sensors/clear':
                POOL[:] = []
                return self._send({"ok": True, "sensors": []})
            if p == '/api/presets_import':
                added = []
                for k, v in (b.get('presets') or {}).items():
                    if k and k not in PRESETS:
                        PRESETS[k] = v; added.append(k)
                return self._send({"ok": True, "added": added, "names": pnames()})
            if p == '/api/test/force':
                FORCED.clear()
                FORCED.update(b.get('values') or {})
                return self._send({"ok": True, "forced": FORCED})
            if p.startswith('/api/presets/'):
                nm = unquote(p.split('/api/presets/', 1)[1]).strip()
                if not nm: return self._send({"detail": "ad"}, 400)
                PRESETS[nm] = b
                return self._send({"ok": True, "name": nm, "names": pnames()})
            if p == '/api/mock/trigger':
                for sid, nm2, un in [("MOCK_PRESSURE_P1", "Pompa Basinci", "Bar"), ("MOCK_FLOW_Q2", "Debi", "L/min"),
                                     ("MOCK_TEMP_T3", "Yag Sicakligi", "C"), ("MOCK_TORQUE_M4", "Tork", "Nm"),
                                     ("MOCK_RPM_N5", "Devir", "rpm")]:
                    upsert(sid, nm2, un)
                return self._send({"ok": True, "added": [s["sensor_id"] for s in POOL], "sensors": POOL})
            if p == '/api/config':
                return self._send({"influx_url": "x", "db": "y", "refresh_ms": b.get('refresh_ms', 1000)})
        return self._send({"error": "nf"}, 404)

    def do_DELETE(self):
        p = urlparse(self.path).path
        with LOCK:
            if p.startswith('/api/presets/'):
                nm = unquote(p.split('/api/presets/', 1)[1])
                ex = nm in PRESETS
                if ex: del PRESETS[nm]
                return self._send({"ok": True, "removed": ex, "names": pnames()})
            if p.startswith('/api/sensors/'):
                sid = unquote(p.split('/api/sensors/', 1)[1])
                POOL[:] = [s for s in POOL if s["sensor_id"] != sid]
                return self._send({"ok": True, "sensors": POOL})
        return self._send({"error": "nf"}, 404)


ThreadingHTTPServer(('127.0.0.1', 8989), H).serve_forever()
