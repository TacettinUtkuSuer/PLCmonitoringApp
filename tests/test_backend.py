# -*- coding: utf-8 -*-
"""main.py'nin GERCEK backend fonksiyonlarini FastAPI olmadan test eder.
fastapi/pydantic/httpx/uvicorn icin asgari shim'ler kurulur, sonra main.py
normal sekilde import edilir ve uc nokta fonksiyonlari dogrudan cagrilir."""
import sys, types, json, asyncio, inspect, os

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
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import main  # noqa: E402

run = asyncio.get_event_loop().run_until_complete
PASS, FAIL = [], []


def chk(name, cond, extra=''):
    (PASS if cond else FAIL).append(name)
    print(('  OK   ' if cond else '  FAIL ') + name + (('  -> ' + str(extra)) if (extra and not cond) else ''))


def reset():
    main.state["pool_sensors"] = []


print('\n=== 1) parse_limit / KEEP semantigi ===')
chk('verilmemis (None) -> KEEP', main.parse_limit(None) is main.KEEP)
chk('KEEP -> KEEP', main.parse_limit(main.KEEP) is main.KEEP)
chk('bos string -> None (esigi kaldir)', main.parse_limit("") is None)
chk('"null" -> None', main.parse_limit("null") is None)
chk('"250" -> 250.0', main.parse_limit("250") == 250.0)
chk('"-10.5" -> -10.5', main.parse_limit("-10.5") == -10.5)
chk('virgullu "12,5" -> 12.5', main.parse_limit("12,5") == 12.5)
chk('cop metin -> KEEP (mevcut korunur)', main.parse_limit("abc") is main.KEEP)
chk('"0" -> 0.0 (sifir esik gecerli)', main.parse_limit("0") == 0.0)

print('\n=== 2) upsert_sensor esik davranisi ===')
reset()
main.upsert_sensor("P1", "Pompa", "Bar", 0.0, 250.0)
rec = main.state["pool_sensors"][0]
chk('yeni kayit esikleri alir', rec["amin"] == 0.0 and rec["amax"] == 250.0, rec)
main.upsert_sensor("P1", "Pompa2", "Bar")
rec = main.state["pool_sensors"][0]
chk('KEEP ile guncelleme esige DOKUNMAZ', rec["amin"] == 0.0 and rec["amax"] == 250.0, rec)
chk('isim guncellendi', rec["name"] == "Pompa2")
main.upsert_sensor("P1", "", "", None, main.KEEP)
rec = main.state["pool_sensors"][0]
chk('None ile alt esik SILINIR', rec["amin"] is None and rec["amax"] == 250.0, rec)
main.upsert_sensor("Q2", "Debi", "L/min")
chk('esiksiz yeni kayit None/None', main.state["pool_sensors"][1]["amin"] is None
    and main.state["pool_sensors"][1]["amax"] is None)

print('\n=== 3) GET webhook (govdesiz, tek URL) ===')
reset()
r = run(main.api_webhook_get(influx_name="TEMP_T3", name="Yag Sicakligi", unit="C", min="-10", max="120"))
chk('GET webhook sensoru olusturur', r.content["ok"] and r.content["created"], r.content)
chk('GET webhook esikleri yazar', r.content["min"] == -10.0 and r.content["max"] == 120.0, r.content)
chk('havuza eklendi', len(main.state["pool_sensors"]) == 1)
r = run(main.api_webhook_get(influx_name="TEMP_T3", name="Yeni Ad"))
chk('ikinci cagri created=False', r.content["created"] is False)
chk('esik parametresi verilmeyince KORUNUR', r.content["min"] == -10.0 and r.content["max"] == 120.0, r.content)
r = run(main.api_webhook_get(sensor_id="ALIAS_A"))
chk('sensor_id takma adi calisir', r.content["influx_name"] == "ALIAS_A")
r = run(main.api_webhook_get(id="SHORT_B"))
chk('id takma adi calisir', r.content["influx_name"] == "SHORT_B")
try:
    run(main.api_webhook_get())
    chk('bos ad 400 verir', False, 'exception atilmadi')
except main.HTTPException as e:
    chk('bos ad 400 verir', e.status_code == 400)
try:
    run(main.api_webhook_get(influx_name="kotu ad!"))
    chk('gecersiz karakter 400 verir', False, 'exception atilmadi')
except main.HTTPException as e:
    chk('gecersiz karakter 400 verir', e.status_code == 400)

print('\n=== 4) GET coklu webhook (tek URL, cok sensor) ===')
reset()
r = run(main.api_webhook_get_bulk(s="P1|Pompa Basinci|Bar|0|250,T3|Yag Sicakligi|C|-10|120,N5|Devir|rpm"))
c = r.content
chk('3 sensor eklendi', c["added"] == ["P1", "T3", "N5"], c["added"])
pool = {s["sensor_id"]: s for s in main.state["pool_sensors"]}
chk('P1 adi/birimi dogru', pool["P1"]["name"] == "Pompa Basinci" and pool["P1"]["unit"] == "Bar")
chk('P1 esikleri dogru', pool["P1"]["amin"] == 0.0 and pool["P1"]["amax"] == 250.0)
chk('T3 negatif esik dogru', pool["T3"]["amin"] == -10.0)
chk('N5 esiksiz (eksik alanlar atlanabilir)', pool["N5"]["amin"] is None and pool["N5"]["amax"] is None)
r = run(main.api_webhook_get_bulk(s="ok_1|A,ge cersiz!|B"))
chk('gecersiz ad atlanir', r.content["skipped"] == ["ge cersiz!"], r.content["skipped"])
chk('gecerli olan eklenir', "ok_1" in r.content["added"])
n_before = len(main.state["pool_sensors"])
r = run(main.api_webhook_get_bulk(s="ONLY|Tek", replace=1))
chk('replace=1 havuzu temizler', len(main.state["pool_sensors"]) == 1 and n_before > 1)

print('\n=== 5) /api/sensors/{id}/limits ===')
reset()
main.upsert_sensor("P1", "Pompa", "Bar")
r = run(main.api_set_limits("P1", min="5", max="95"))
chk('ikisini birden ayarla', r.content["min"] == 5.0 and r.content["max"] == 95.0, r.content)
r = run(main.api_set_limits("P1", min="7"))
chk('sadece min gonderince max KORUNUR', r.content["min"] == 7.0 and r.content["max"] == 95.0, r.content)
r = run(main.api_set_limits("P1", max="80"))
chk('sadece max gonderince min KORUNUR', r.content["min"] == 7.0 and r.content["max"] == 80.0, r.content)
r = run(main.api_set_limits("P1", min=""))
chk('bos min esigi KALDIRIR', r.content["min"] is None and r.content["max"] == 80.0, r.content)
try:
    run(main.api_set_limits("YOK_BOYLE"))
    chk('havuzda olmayan sensor 404', False, 'exception atilmadi')
except main.HTTPException as e:
    chk('havuzda olmayan sensor 404', e.status_code == 404)
try:
    run(main.api_set_limits("kotu ad!"))
    chk('gecersiz ad 400', False, 'exception atilmadi')
except main.HTTPException as e:
    chk('gecersiz ad 400', e.status_code == 400)

print('\n=== 6) POST webhook geriye donuk uyumluluk ===')
reset()
r = run(main.api_webhook(main.SensorIn(influx_name="LEGACY_1", name="Eski", unit="Bar")))
chk('eski POST govdesi hala calisir', r.content["ok"] and r.content["created"])
chk('esik verilmeyince None', r.content["min"] is None and r.content["max"] is None)
r = run(main.api_webhook(main.SensorIn(influx_name="LEGACY_2", name="Yeni", unit="C", min=10, max=90)))
chk('POST govdesiyle de esik yazilir', r.content["min"] == 10.0 and r.content["max"] == 90.0, r.content)

print('\n=== 7) /api/last (alarm izleyici) ===')
reset()
r = run(main.api_last(sensors=""))
chk('bos istek bos sozluk doner', r.content["values"] == {})
r = run(main.api_last(sensors="MOCK_PRESSURE_P1,MOCK_TEMP_T3"))
v = r.content["values"]
chk('MOCK sensorler deger uretir', len(v) == 2 and all(x is not None for x in v.values()), v)
hx.AsyncClient.PAYLOAD = {"results": [{"series": [
    {"columns": ["time", "REAL_A", "REAL_B"], "values": [["2026-09-17T10:00:00Z", 42.5, None]]}]}]}
r = run(main.api_last(sensors="REAL_A,REAL_B"))
v = r.content["values"]
chk('gercek sensor son degeri okunur', v["REAL_A"] == 42.5, v)
chk('null deger None kalir', v["REAL_B"] is None, v)
r = run(main.api_last(sensors="kotu ad!"))
chk('gecersiz ad filtrelenir', r.content["values"] == {})


class _Boom(AsyncClient):
    async def get(self, url, **kw):
        raise RuntimeError('baglanti yok')


hx.AsyncClient = _Boom
main._HTTP["client"] = None          # paylasilan istemci yeniden kurulsun
r = run(main.api_last(sensors="REAL_A"))
chk('DB hatasi cokmeden error doner', 'error' in r.content and r.content["values"]["REAL_A"] is None, r.content)
hx.AsyncClient = AsyncClient
main._HTTP["client"] = None

print('\n=== 8) Preset bulk esikleri tasir ===')
reset()
payload = main.BulkSensorsIn(sensors=[
    main.SensorIn(influx_name="B1", name="Bir", unit="Bar", min=1, max=9),
    main.SensorIn(influx_name="B2", name="Iki", unit="C"),
], replace=True)
r = run(main.api_bulk_sensors(payload))
pool = {s["sensor_id"]: s for s in main.state["pool_sensors"]}
chk('bulk esikleri yazar', pool["B1"]["amin"] == 1.0 and pool["B1"]["amax"] == 9.0, pool["B1"])
chk('esiksiz bulk kaydi None', pool["B2"]["amin"] is None and pool["B2"]["amax"] is None)

print('\n=== 8b) Kart sirasi / yeni kart en ustte / yerinde yeniden adlandirma ===')
reset()
for sid in ("H1", "H2", "H3"):
    run(main.api_webhook_get(influx_name=sid))
ids = lambda: [s["sensor_id"] for s in main.state["pool_sensors"]]
chk('HMI webhook sona ekler', ids() == ["H1", "H2", "H3"], ids())
run(main.api_webhook_get(influx_name="CALC_X", expr="H1*2", manual=1))
chk('arayuz karti (manual) EN BASA eklenir', ids()[0] == "CALC_X", ids())
run(main.api_webhook_get(influx_name="H3", name="guncel", manual=1))
chk('mevcut karti guncellemek yerini DEGISTIRMEZ', ids()[-1] == "H3", ids())
run(main.api_order_sensors(ids="H3,H1"))
chk('order: verilenler basa, kalanlar arkaya', ids() == ["H3", "H1", "CALC_X", "H2"], ids())
run(main.api_order_sensors(ids="YOK,H2"))
chk('order: bilinmeyen ad yok sayilir', ids() == ["H2", "H3", "H1", "CALC_X"], ids())
run(main.api_webhook_get(influx_name="CALC_Y", expr="CALC_X+1", manual=1))
pos = ids().index("CALC_X")
r = run(main.api_rename_sensor("CALC_X", main.RenameIn(to="CALC_Z", name="Zet", exprs={"CALC_Y": "CALC_Z+1"})))
rec = next(s for s in main.state["pool_sensors"] if s["sensor_id"] == "CALC_Z")
chk('rename: kimlik degisti', "CALC_X" not in ids() and "CALC_Z" in ids(), ids())
chk('rename: sira korundu', ids().index("CALC_Z") == pos, ids())
chk('rename: ad yazildi, formul korundu', rec["name"] == "Zet" and rec["expr"] == "H1*2", rec)
chk('rename: baska kartin formulu ayni istekte guncellendi',
    next(s for s in main.state["pool_sensors"] if s["sensor_id"] == "CALC_Y")["expr"] == "CALC_Z+1")
try:
    run(main.api_rename_sensor("CALC_Z", main.RenameIn(to="H1")))
    chk('rename: var olan ada 409', False)
except main.HTTPException as e:
    chk('rename: var olan ada 409', e.status_code == 409)
run(main.api_rename_sensor("CALC_Z", main.RenameIn(to="H9", expr="")))
rec = next(s for s in main.state["pool_sensors"] if s["sensor_id"] == "H9")
chk('rename + expr="" formulu kaldirir (duz alana donus)', rec["expr"] is None, rec)

print('\n=== 8c) Malzeme tablolari (CSV) ===')
import tempfile
main.MATERIAL_PATH = os.path.join(tempfile.mkdtemp(), "materials.json")
SU = ("temp [degC],dens [kg/m3],cp [J/kg\u00b7K],dvis [Pa\u00b7s],cond [W/m\u00b7K]\n"
      "10,998.50,4189.05,0.001311964,0.5772\n0,998.73,4235.36,0.001779548,0.5567\n"
      "20,996.95,4172.15,0.001004854,0.5959\n")
rows = main.parse_material_csv(SU)
chk('CSV okundu ve sicakliga gore siralandi', [r[0] for r in rows] == [0.0, 10.0, 20.0], rows)
chk('sutunlar dogru eslesti', rows[0] == [0.0, 998.73, 4235.36, 0.001779548, 0.5567], rows[0])
chk('Turkce Excel (; ve ondalik virgul) kabul',
    main.parse_material_csv("temp [degC];dens [kg/m3];cp [J/kg.K];dvis [Pa.s];cond [W/m*K]\n"
                            "0;998,73;1;2;3\n10;998,5;1;2;3")[0][1] == 998.73)
chk('sutun sirasi serbest',
    main.parse_material_csv("dens [kg/m3],temp [degC],cp [J/kg\u00b7K],dvis [Pa\u00b7s],cond [W/m\u00b7K]\n"
                            "5,0,1,2,3\n6,10,1,2,3")[1][:2] == [10.0, 6.0])
for label, bad, frag in [
        ('yanlis birim reddedilir', SU.replace("[degC]", "[K]"), "degC"),
        ('eksik sutun reddedilir', "temp [degC],dens [kg/m3]\n0,1\n1,2", "Eksik"),
        ('sayi olmayan deger reddedilir', SU.replace("998.50", "abc"), "sayi degil"),
        ('tekrar eden sicaklik reddedilir', SU.replace("20,996.95", "10,996.95"), "iki kez"),
        ('tek veri satiri reddedilir', SU.split("\n")[0] + "\n0,1,2,3,4", "iki veri")]:
    try:
        main.parse_material_csv(bad)
        chk(label, False)
    except ValueError as e:
        chk(label, frag in str(e), str(e))
r = run(main.api_material_save("SU", main.MaterialIn(csv=SU, dry=True)))
chk('dry=1 kaydetmez', r.content["ok"] and main.load_materials() == {})
r = run(main.api_material_save("SU", main.MaterialIn(csv=SU)))
chk('kaydedildi (dosyaya)', "SU" in main.load_materials() and len(main.load_materials()["SU"]["rows"]) == 3)
run(main.api_material_save("su", main.MaterialIn(csv=SU)))
chk('ayni ad (harf farki) uzerine yazar', list(main.load_materials().keys()) == ["su"])
try:
    run(main.api_material_save("2SU", main.MaterialIn(csv=SU)))
    chk('rakamla baslayan ad reddedilir', False)
except main.HTTPException as e:
    chk('rakamla baslayan ad reddedilir', e.status_code == 400)
run(main.api_material_delete("SU"))
chk('silindi', main.load_materials() == {})

print('\n=== 9) Rota kaydi ===')
paths = {(m, p) for m, p, _ in main.app.routes}
for m, p in [('GET', '/api/webhook/sensor'), ('POST', '/api/webhook/sensor'),
             ('GET', '/api/webhook/sensors'), ('GET', '/api/sensors/{sensor_id}/limits'),
             ('POST', '/api/sensors/{sensor_id}/limits'), ('GET', '/api/last'),
             ('POST', '/api/sensors/order'), ('POST', '/api/sensors/{sensor_id}/rename'),
             ('GET', '/api/materials'), ('POST', '/api/materials/{name}'),
             ('DELETE', '/api/materials/{name}')]:
    chk('rota kayitli: %s %s' % (m, p), (m, p) in paths)

print('\n' + '=' * 62)
print('BACKEND:  %d gecti,  %d kaldi' % (len(PASS), len(FAIL)))
if FAIL:
    for f in FAIL:
        print('   KALAN: ' + f)
print('=' * 62)
sys.exit(1 if FAIL else 0)
