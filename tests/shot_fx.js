const { chromium } = require('playwright');
const sleep = ms => new Promise(r => setTimeout(r, ms));
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1600, height: 950 } });
  await p.goto('http://127.0.0.1:8989', { waitUntil: 'domcontentloaded' });
  await sleep(900);
  await p.evaluate(async () => {
    await fetch('/api/sensors/clear', { method: 'POST' });
    await fetch('/api/webhook/sensors?s=MOCK_PRESSURE_P1|Pompa Basinci|Bar|20|180,MOCK_TEMP_T3|Yag Sicakligi|C|10|60,MOCK_RPM_N5|Devir|rpm').then(r=>r.json());
    await fetch('/api/webhook/sensor?influx_name=asd_65&name=Elle Eklenen&unit=Bar&manual=1').then(r=>r.json());
    await fetch('/api/webhook/sensor?influx_name=CALC_GUC&name=Mil Gucu&unit=kW&expr=MOCK_RPM_N5*MOCK_PRESSURE_P1/9550&manual=1').then(r=>r.json());
    const j = await fetch('/api/sensors').then(r=>r.json());
    window.applyPool(j.sensors); window.renderPool(true);
  });
  await sleep(400);
  await p.evaluate(() => {
    window.addSeries(0, 'MOCK_PRESSURE_P1');
    window.addSeries(0, 'MOCK_RPM_N5');
    window.addSeries(1, 'CALC_GUC');
  });
  await p.evaluate(() => window.refreshAll(true));
  await sleep(1500);
  await p.evaluate(() => window.alarmWatch());
  await sleep(800);
  await p.locator('#pool').screenshot({ path: __dirname + '/out/pool_cards.png' });
  await p.click('#newBtn');
  await p.locator('#poolList .scard.draft .nmi').fill('Fark');
  await p.locator('#poolList .scard.draft .uni').fill('Bar');
  await p.locator('#poolList .scard.draft .fxi').fill('=MOCK_PRESSURE_P1 - MOCK_TEMP_T3');
  await sleep(400);
  await p.locator('#pool').screenshot({ path: __dirname + '/out/pool_draft.png' });
  await p.screenshot({ path: __dirname + '/out/full.png' });
  await b.close();
})();
