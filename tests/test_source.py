# -*- coding: utf-8 -*-
"""SENSOR BAZLI KAYNAK (db / measurement / influx_url) testleri.
main.py dogrudan icdeki shim'lerle yuklenir; InfluxDB'ye cikilmaz -- bunun
yerine influx_query yakalanir ve HANGI sorgunun HANGI url/db'ye gittigi olculur."""
import sys, os, asyncio, importlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import serve_test  # noqa: F401  (fastapi/pydantic/httpx shim'lerini kurar)

main = importlib.import_module('main')
LOOP = asyncio.new_event_loop()
run = LOOP.run_until_complete

PASS, FAIL = [], []


def chk(name, cond, extra=None):
    (PASS if cond else FAIL).append(name)
    line = ('  OK   ' if cond else '  FAIL ') + name
    if not cond and extra is not None:
        line += '  -> ' + repr(extra)
    print(line)


def reset():
    main.state['pool_sensors'] = []
    main.CONFIG['influx_url'] = 'http://genel:8086'
    main.CONFIG['db'] = 'genel_db'
    main.CONFIG['measurement'] = 'genel_meas'


# --- influx_query'yi yakala: gercek istek yerine cagri kaydi tut -------------------
CALLS = []


async def fake_query(query, base_url=None, db=None):
    CALLS.append({'q': query, 'url': base_url, 'db': db})
    return {"results": [{"series": []}]}


main.influx_query = fake_query


print('=== 1) Webhook: alanlar BIRBIRINDEN BAGIMSIZ ===')
reset()
r = run(main.api_webhook_get(influx_name='P1', db='hat2'))
c = r.content
chk('yalnizca db gonderildi -> db gecersiz kilindi', c['db'] == 'hat2', c['db'])
chk('measurement DOKUNULMADI (genel ayar)', c['measurement'] is None, c['measurement'])
chk('influx_url DOKUNULMADI (genel ayar)', c['influx_url'] is None, c['influx_url'])
chk('etkin kaynak: db kart, digerleri genel',
    c['effective'] == {'influx_url': 'http://genel:8086', 'db': 'hat2',
                       'measurement': 'genel_meas'}, c['effective'])

r = run(main.api_webhook_get(influx_name='T3', measurement='press'))
c = r.content
chk('yalnizca measurement gonderildi', c['measurement'] == 'press', c['measurement'])
chk('db genel ayardan', c['effective']['db'] == 'genel_db', c['effective'])

r = run(main.api_webhook_get(influx_name='N5', influx_url='http://baska:8086/'))
c = r.content
chk('yalnizca url gonderildi', c['influx_url'] == 'http://baska:8086', c['influx_url'])
chk('url sonundaki / temizlendi', not c['influx_url'].endswith('/'), c['influx_url'])
chk('db + measurement genel ayardan',
    c['effective']['db'] == 'genel_db' and c['effective']['measurement'] == 'genel_meas',
    c['effective'])

r = run(main.api_webhook_get(influx_name='A1', db='d', measurement='m', influx_url='http://u:1'))
chk('ucu birden gonderilebiliyor',
    r.content['effective'] == {'influx_url': 'http://u:1', 'db': 'd', 'measurement': 'm'},
    r.content['effective'])

print('\n=== 2) Kismi guncelleme mevcut alanlari BOZMUYOR ===')
reset()
run(main.api_webhook_get(influx_name='P1', db='hat2', measurement='press'))
run(main.api_webhook_get(influx_name='P1', name='Pompa', unit='Bar'))   # kaynak YOK
rec = main.state['pool_sensors'][0]
chk('ad guncellendi', rec['name'] == 'Pompa', rec)
chk('db korundu', rec['db'] == 'hat2', rec)
chk('measurement korundu', rec['meas'] == 'press', rec)

run(main.api_webhook_get(influx_name='P1', db=''))                      # bos = KALDIR
rec = main.state['pool_sensors'][0]
chk('bos db gecersiz kilmayi KALDIRDI', rec['db'] is None, rec)
chk('measurement hala duruyor', rec['meas'] == 'press', rec)
chk('kaldirilan alan genel ayara dondu',
    main.sensor_source('P1') == ('http://genel:8086', 'genel_db', 'press'),
    main.sensor_source('P1'))

print('\n=== 3) /source uc noktasi ===')
reset()
run(main.api_webhook_get(influx_name='P1'))
r = run(main.api_set_source('P1', db='sonradan'))
chk('/source db ayarladi', r.content['db'] == 'sonradan', r.content)
chk('/source digerlerine dokunmadi', r.content['measurement'] is None, r.content)
try:
    run(main.api_set_source('YOK_BOYLE', db='x'))
    chk('havuzda olmayan sensor 404 verir', False, 'istisna atmadi')
except main.HTTPException as e:
    chk('havuzda olmayan sensor 404 verir', e.status_code == 404, e.status_code)

print('\n=== 4) Toplu (pipe) webhook ===')
reset()
#           0 |  1   | 2 |3|  4 |  5 |   6   |      7
#   influx_name|name  |br |m|max | db | meas  |  influx_url
r = run(main.api_webhook_get_bulk(
    s='P1|Pompa|Bar|0|250|hat2,T3|Sicaklik|C||||press|http://x:8086'))
p1 = next(s for s in main.state['pool_sensors'] if s['sensor_id'] == 'P1')
t3 = next(s for s in main.state['pool_sensors'] if s['sensor_id'] == 'T3')
chk('pipe: P1 db aldi', p1['db'] == 'hat2', p1)
chk('pipe: P1 measurement genel', p1['meas'] is None, p1)
chk('pipe: P1 esikleri bozulmadi', p1['amin'] == 0 and p1['amax'] == 250, p1)
chk('pipe: T3 measurement + url aldi',
    t3['meas'] == 'press' and t3['url'] == 'http://x:8086', t3)
chk('pipe: T3 db genel', t3['db'] is None, t3)

print('\n=== 5) /api/data: kaynaga gore GRUPLAMA ===')
reset()
del CALLS[:]
run(main.api_webhook_get(influx_name='A', db='dbA'))
run(main.api_webhook_get(influx_name='B', db='dbA'))          # A ile AYNI kaynak
run(main.api_webhook_get(influx_name='C', measurement='mC'))  # farkli measurement
run(main.api_webhook_get(influx_name='D'))                    # tamamen genel
run(main.api_data(sensors='A,B,C,D', range='5m'))

chk('3 ayri kaynak -> 3 sorgu', len(CALLS) == 3, [c['db'] for c in CALLS])
g_ab = [c for c in CALLS if c['db'] == 'dbA']
chk('ayni db\'li A ve B TEK sorguda birlesti', len(g_ab) == 1, g_ab)
chk('A ve B ayni SELECT icinde',
    len(g_ab) == 1 and '"A"' in g_ab[0]['q'] and '"B"' in g_ab[0]['q'],
    g_ab[0]['q'] if g_ab else None)
chk('A/B genel measurement kullaniyor',
    len(g_ab) == 1 and 'FROM "genel_meas"' in g_ab[0]['q'], g_ab[0]['q'] if g_ab else None)
g_c = [c for c in CALLS if 'FROM "mC"' in c['q']]
chk('C kendi measurement\'ini kullandi', len(g_c) == 1, [c['q'] for c in CALLS])
chk('C genel db kullandi', len(g_c) == 1 and g_c[0]['db'] == 'genel_db', g_c)
g_d = [c for c in CALLS if '"D"' in c['q']]
chk('D tamamen genel ayardan',
    len(g_d) == 1 and g_d[0]['db'] == 'genel_db' and g_d[0]['url'] == 'http://genel:8086',
    g_d)

print('\n=== 6) /api/last: ayni gruplama ===')
reset()
del CALLS[:]
run(main.api_webhook_get(influx_name='A', db='dbA'))
run(main.api_webhook_get(influx_name='B', db='dbA'))
run(main.api_webhook_get(influx_name='C', influx_url='http://uzak:8086'))
run(main.api_last(sensors='A,B,C'))
chk('2 ayri kaynak -> 2 sorgu', len(CALLS) == 2, [(c['url'], c['db']) for c in CALLS])
chk('uzak sunucuya ayri istek gitti',
    any(c['url'] == 'http://uzak:8086' for c in CALLS), [c['url'] for c in CALLS])

print('\n=== 7) Zaman ekseni BIRLESIMI (farkli kaynaklar hizalanir) ===')
reset()
run(main.api_webhook_get(influx_name='A', db='dbA'))
run(main.api_webhook_get(influx_name='B', db='dbB'))


async def split_query(query, base_url=None, db=None):
    """dbA 10:00:00 ve 10:00:02'de, dbB 10:00:01 ve 10:00:02'de veri verir."""
    if db == 'dbA':
        rows = [["2026-01-01T10:00:00Z", 1.0], ["2026-01-01T10:00:02Z", 3.0]]
        cols = ["time", "A"]
    else:
        rows = [["2026-01-01T10:00:01Z", 20.0], ["2026-01-01T10:00:02Z", 30.0]]
        cols = ["time", "B"]
    return {"results": [{"series": [{"columns": cols, "values": rows}]}]}


main.influx_query = split_query
body = run(main.api_data(sensors='A,B', range='5m')).content
main.influx_query = fake_query

chk('zaman ekseni birlesti (3 ayrik an)', body['points'] == 3, body['points'])
chk('zaman ekseni sirali', body['t'] == sorted(body['t']), body['t'])
chk('A: [1, BOS, 3]', body['series']['A'] == [1.0, None, 3.0], body['series']['A'])
chk('B: [BOS, 20, 30]', body['series']['B'] == [None, 20.0, 30.0], body['series']['B'])
chk('her seri zaman ekseni ile ayni uzunlukta',
    all(len(v) == body['points'] for v in body['series'].values()),
    {k: len(v) for k, v in body['series'].items()})

print('\n=== 8) Bir kaynak DUSERSE digeri ayakta kalir ===')
reset()
run(main.api_webhook_get(influx_name='A', db='iyi'))
run(main.api_webhook_get(influx_name='B', db='bozuk'))


async def flaky(query, base_url=None, db=None):
    if db == 'bozuk':
        raise RuntimeError('baglanti yok')
    return {"results": [{"series": [{"columns": ["time", "A"],
                                     "values": [["2026-01-01T10:00:00Z", 5.0]]}]}]}


main.influx_query = flaky
body = run(main.api_data(sensors='A,B', range='5m')).content
main.influx_query = fake_query

chk('saglam kaynagin verisi geldi', body['series']['A'] == [5.0], body['series']['A'])
chk('bozuk kaynak hata olarak bildirildi', 'bozuk' in (body.get('error') or ''), body.get('error'))
chk('bozuk kaynagin serisi BOS (cokme yok)', body['series']['B'] == [None], body['series']['B'])

print('\n=== 9) Preset / toplu geri yukleme kaynagi tasiyor ===')
reset()
payload = main.BulkSensorsIn(sensors=[
    main.SensorIn(influx_name='P1', name='Pompa', unit='Bar', db='hat2'),
    main.SensorIn(influx_name='T3', name='Sic', unit='C', measurement='press'),
    main.SensorIn(influx_name='N5', name='Devir', unit='rpm'),
], replace=True)
run(main.api_bulk_sensors(payload))
by = {s['sensor_id']: s for s in main.state['pool_sensors']}
chk('toplu: db tasindi', by['P1']['db'] == 'hat2', by['P1'])
chk('toplu: measurement tasindi', by['T3']['meas'] == 'press', by['T3'])
chk('toplu: bos birakilan kart genel ayarda',
    by['N5']['db'] is None and by['N5']['meas'] is None, by['N5'])

print('\n=== 10) POST webhook (JSON govde) ===')
reset()
r = run(main.api_webhook(main.SensorIn(influx_name='P1', name='Pompa', db='hat9', meas='m9')))
chk('POST: db alindi', r.content['db'] == 'hat9', r.content)
chk('POST: meas takma adi calisti', r.content['measurement'] == 'm9', r.content)
chk('POST: url genel ayarda', r.content['influx_url'] is None, r.content)

print('\n' + '=' * 62)
print('KAYNAK TESTI:  %d gecti,  %d kaldi' % (len(PASS), len(FAIL)))
for f in FAIL:
    print('   KALAN: ' + f)
print('=' * 62)
sys.exit(1 if FAIL else 0)
