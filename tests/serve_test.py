# -*- coding: utf-8 -*-
"""main.py'yi GERCEK haliyle calistiran, fastapi GEREKTIRMEYEN test sunucusu.
fastapi/pydantic/httpx/uvicorn icin asgari shim'ler kurulur; main.py'nin kendi
route tablosu stdlib http.server uzerinden servis edilir.  Boylece gomulu
arayuz (INDEX_HTML) gercek uc noktalarla tarayicida test edilebilir."""
import sys, types, json, asyncio, inspect, typing, re, os
from urllib.parse import urlparse, parse_qs

# ----------------------------------------------------------------- SHIM: pydantic
pyd = types.ModuleType('pydantic')


class BaseModel(object):
    def __init__(self, **kw):
        ann = {}
        for klass in reversed(type(self).__mro__):
            ann.update(getattr(klass, '__annotations__', {}) or {})
        for k in ann:
            setattr(self, k, kw.get(k, getattr(type(self), k, None)))
        for k, v in kw.items():
            if k not in ann:
                setattr(self, k, v)


pyd.BaseModel = BaseModel
sys.modules['pydantic'] = pyd

# ----------------------------------------------------------------- SHIM: fastapi
fa = types.ModuleType('fastapi')


class HTTPException(Exception):
    def __init__(self, status_code=400, detail=''):
        super(HTTPException, self).__init__(detail)
        self.status_code = status_code
        self.detail = detail


class WebSocket(object):
    pass


class WebSocketDisconnect(Exception):
    pass


class FastAPI(object):
    def __init__(self, **kw):
        self.routes = []

    def _reg(self, method):
        def deco(path, **kw):
            def wrap(fn):
                self.routes.append((method, path, fn))
                return fn
            return wrap
        return deco

    def __getattr__(self, name):
        if name in ('get', 'post', 'delete', 'put', 'patch'):
            return self._reg(name.upper())
        raise AttributeError(name)

    def websocket(self, path):
        def wrap(fn):
            self.routes.append(('WS', path, fn))
            return fn
        return wrap

    def mount(self, *a, **kw):
        pass


fa.FastAPI = FastAPI
fa.HTTPException = HTTPException
fa.WebSocket = WebSocket
fa.WebSocketDisconnect = WebSocketDisconnect
sys.modules['fastapi'] = fa

resp_mod = types.ModuleType('fastapi.responses')


class JSONResponse(object):
    def __init__(self, content=None, **kw):
        self.content = content


class HTMLResponse(object):
    def __init__(self, content=None, **kw):
        self.content = content


class Response(object):
    def __init__(self, *a, **kw):
        pass


resp_mod.JSONResponse = JSONResponse
resp_mod.HTMLResponse = HTMLResponse
resp_mod.Response = Response
sys.modules['fastapi.responses'] = resp_mod
fa.responses = resp_mod

static_mod = types.ModuleType('fastapi.staticfiles')


class StaticFiles(object):
    def __init__(self, **kw):
        pass


static_mod.StaticFiles = StaticFiles
sys.modules['fastapi.staticfiles'] = static_mod
fa.staticfiles = static_mod

# ----------------------------------------------------------------- SHIM: httpx / uvicorn
hx = types.ModuleType('httpx')


class _FakeResp(object):
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class AsyncClient(object):
    """Gercek InfluxDB yok: bos sonuc kumesi doner (MOCK_* sensorler yine calisir)."""
    PAYLOAD = {"results": [{"series": []}]}

    def __init__(self, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kw):
        return _FakeResp(AsyncClient.PAYLOAD)


hx.AsyncClient = AsyncClient
sys.modules['httpx'] = hx

uv = types.ModuleType('uvicorn')
uv.run = lambda *a, **kw: None
sys.modules['uvicorn'] = uv

# ----------------------------------------------------------------- IMPORT
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import main  # noqa: E402

LOOP = asyncio.new_event_loop()

# ----------------------------------------------------------------- ROUTER
PARAM_RE = re.compile(r'\{([A-Za-z_][A-Za-z0-9_]*)\}')


def compile_route(path):
    pat = '^' + PARAM_RE.sub(lambda m: '(?P<%s>[^/]+)' % m.group(1), path) + '$'
    return re.compile(pat)


ROUTES = []
for _m, _p, _fn in main.app.routes:
    if _m == 'WS':
        continue
    ROUTES.append((_m, compile_route(_p), _fn, _p))


def _unwrap(ann):
    """Optional[X] / Union[X, None] -> X"""
    origin = typing.get_origin(ann)
    if origin is typing.Union:
        args = [a for a in typing.get_args(ann) if a is not type(None)]
        if args:
            return args[0]
    return ann


def build_kwargs(fn, path_vars, query, body):
    sig = inspect.signature(fn)
    kwargs = {}
    for name, param in sig.parameters.items():
        ann = _unwrap(param.annotation)
        if name in path_vars:
            kwargs[name] = path_vars[name]
            continue
        if inspect.isclass(ann) and issubclass(ann, BaseModel):
            kwargs[name] = ann(**(body or {}))
            continue
        if name in query:
            raw = query[name][0]
            if ann is int:
                try:
                    kwargs[name] = int(raw)
                except ValueError:
                    kwargs[name] = 0
            elif ann is bool:
                kwargs[name] = raw not in ('0', 'false', 'False', '')
            else:
                kwargs[name] = raw
            continue
        if param.default is not inspect.Parameter.empty:
            continue
        kwargs[name] = None
    return kwargs


def dispatch(method, path, query, body):
    for m, rx, fn, raw in ROUTES:
        if m != method:
            continue
        hit = rx.match(path)
        if not hit:
            continue
        try:
            kwargs = build_kwargs(fn, hit.groupdict(), query, body)
            res = fn(**kwargs)
            if inspect.iscoroutine(res):
                res = LOOP.run_until_complete(res)
        except HTTPException as e:
            return status_of(e.status_code), {"detail": e.detail}
        except Exception as e:                                  # noqa: BLE001
            import traceback
            traceback.print_exc()
            return 500, {"detail": "%s: %s" % (type(e).__name__, e)}
        if isinstance(res, HTMLResponse):
            return 200, ('html', res.content)
        if isinstance(res, JSONResponse):
            return 200, res.content
        return 200, res
    return 404, {"detail": "not found"}


def status_of(code):
    try:
        return int(code)
    except Exception:
        return 400


# ----------------------------------------------------------------- HTTP SERVER
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def _go(self, method):
        u = urlparse(self.path)
        query = parse_qs(u.query, keep_blank_values=True)
        body = None
        n = int(self.headers.get('Content-Length') or 0)
        if n:
            raw = self.rfile.read(n)
            try:
                body = json.loads(raw.decode('utf-8'))
            except Exception:
                body = None
        code, payload = dispatch(method, u.path, query, body)
        if isinstance(payload, tuple) and payload and payload[0] == 'html':
            data = payload[1].encode('utf-8')
            ctype = 'text/html; charset=utf-8'
        else:
            data = json.dumps(payload, default=str).encode('utf-8')
            ctype = 'application/json'
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._go('GET')

    def do_POST(self):
        self._go('POST')

    def do_DELETE(self):
        self._go('DELETE')


if __name__ == '__main__':
    port = int(sys.argv[1] if len(sys.argv) > 1 else 8989)
    print('test server on %d  (routes: %d)' % (port, len(ROUTES)), flush=True)
    ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()
