const { chromium } = require('playwright');
const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1920, height: 1080 } });
  await p.goto('http://127.0.0.1:8989', { waitUntil: 'networkidle' });

  await p.evaluate(() => fetch('/api/sensors/clear', { method: 'POST' }));
  await p.evaluate(() => fetch('/api/webhook/sensors?s=MOCK_PRESSURE_P1|Pompa Basinci|Bar|20|80,MOCK_TEMP_T3|Yag Sicakligi|C|10|60,MOCK_RPM_N5|Devir|rpm|0|1500,MOCK_TORQUE_M4|Tork|Nm').then(r => r.json()));
  await p.evaluate(() => fetch('/api/sensors').then(r => r.json()).then(j => window.applyPool(j.sensors)));
  await sleep(500);

  /* iki grafige seri dagit */
  await p.evaluate(() => {
    window.addSeries(0, 'MOCK_PRESSURE_P1');
    window.addSeries(0, 'MOCK_TEMP_T3');
    window.addSeries(1, 'MOCK_RPM_N5');
    window.addSeries(2, 'MOCK_TORQUE_M4');
  });
  await sleep(1500);

  /* P1'i esik disina zorla -> havuz karti + CH1 cercevesi yanip sonmeli */
  await p.evaluate(() => fetch('/api/test/force', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ values: { MOCK_PRESSURE_P1: 140, MOCK_TEMP_T3: 35, MOCK_RPM_N5: 700, MOCK_TORQUE_M4: 25 } })
  }).then(r => r.json()));
  await p.evaluate(() => window.refreshAll(true));
  await sleep(1200);
  await p.evaluate(() => window.alarmWatch());
  await sleep(300);

  /* yanip sonmenin "yanik" fazini yakalamak icin animasyonu sabitle */
  await p.addStyleTag({ content: '.scard.alarm,.card.alarm{animation:none !important}' +
    '.scard.alarm{background:#4a1118 !important;border-color:#ff5c6c !important}' +
    '.card.alarm{border-color:#ff5c6c !important;box-shadow:0 0 0 2px rgba(255,92,108,.55) inset !important}' });
  await sleep(200);

  /* iki sensoru birden alarma sok: kartlar birbirinden bagimsiz yanmali */
  await p.evaluate(() => fetch('/api/test/force', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ values: { MOCK_PRESSURE_P1: 140, MOCK_TEMP_T3: 95, MOCK_RPM_N5: 700, MOCK_TORQUE_M4: 25 } })
  }).then(r => r.json()));
  await p.evaluate(() => window.alarmWatch());
  await sleep(500);

  const st = await p.evaluate(() => ({
    alarm: Object.keys(window.ALARM),
    badge: document.getElementById('altxt').textContent,
    card0: document.getElementById('card0').classList.contains('alarm'),
    card1: document.getElementById('card1').classList.contains('alarm')
  }));
  console.log(JSON.stringify(st));

  await p.screenshot({ path: __dirname + '/out/alarm_fhd.png' });
  await b.close();
})();
