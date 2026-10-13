import os, sys, asyncio, importlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import serve_test  # noqa: F401
main = importlib.import_module('main')
LOOP = asyncio.new_event_loop(); run = LOOP.run_until_complete
CALLS = []
async def fake_query(query, base_url=None, db=None):
    CALLS.append({'q': query, 'url': base_url, 'db': db})
    return {"results": [{"series": []}]}
main.influx_query = fake_query
main.state['pool_sensors'] = []
main.CONFIG.update({'influx_url': 'http://10.10.2.6:8086', 'db': 'lc_100', 'measurement': 'telemetry'})
run(main.api_webhook_get(influx_name='P1', name='Pompa Basinci', unit='Bar', min=0, max=250))
run(main.api_webhook_get(influx_name='T3', name='Yag Sicakligi', unit='C', db='hat2'))
run(main.api_webhook_get(influx_name='N5', name='Devir', unit='rpm', measurement='press', influx_url='http://10.0.0.9:8086'))
run(main.api_data(sensors='P1,T3,N5', range='5m'))
run(main.api_last(sensors='P1,T3,N5'))
for c in CALLS:
    print('URL :', c['url'])
    print('DB  :', c['db'])
    print('Q   :', c['q'])
    print()
