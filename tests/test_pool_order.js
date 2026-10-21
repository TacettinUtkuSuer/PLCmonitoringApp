/* ==========================================================================================
   SOL PANEL: KART SIRASI / YENI KART EN USTTE / HAVUZDA OLMAYAN SENSORLE FORMUL
   Sunucu 8989'da acik olmali:  python tests/serve_test.py
   ========================================================================================== */
const { chromium } = require('playwright');
const sleep = ms => new Promise(r => setTimeout(r, ms));

const PASS = [], FAIL = [];
function chk(name, cond, extra) {
  (cond ? PASS : FAIL).push(name);
  console.log((cond ? '  OK   ' : '  FAIL ') + name +
              ((!cond && extra !== undefined) ? ('  -> ' + JSON.stringify(extra)) : ''));
}

(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1600, height: 1000 } });
  const errs = [];
  p.on('pageerror', e => { errs.push(e.message); console.log('  !! PAGE ERROR: ' + e.message); });
  await p.goto('http://127.0.0.1:8989', { waitUntil: 'domcontentloaded' });
  await sleep(900);

  const srvIds = () => p.evaluate(() => fetch('/api/sensors').then(r => r.json())
    .then(j => j.sensors.map(s => s.sensor_id)));
  const domIds = () => p.evaluate(() => Array.from(document.querySelectorAll('#poolList .scard[data-sid]'))
    .map(e => e.getAttribute('data-sid')));
  const add = (id) => p.evaluate(i => fetch('/api/webhook/sensor?influx_name=' + i)
    .then(r => r.json()).then(j => window.applyPool(j.sensors)), id);

  await p.evaluate(async () => {
    await fetch('/api/sensors/clear', { method: 'POST' });
    window.PPOOL = []; window.DRAFT = null;
    window.CHARTS.forEach(C => { C.series = []; });
    window.applyPool([]);
  });
  for (const id of ['MOCK_A1', 'MOCK_B2', 'MOCK_C3']) await add(id);
  await sleep(200);

  console.log('\n=== HMI webhook sirasi ===');
  chk('webhook ile gelenler gelis sirasiyla (sona eklenir)',
      JSON.stringify(await srvIds()) === JSON.stringify(['MOCK_A1', 'MOCK_B2', 'MOCK_C3']), await srvIds());

  console.log('\n=== Surukle-birak ile siralama ===');
  await p.locator('[data-sid="MOCK_C3"]').dragTo(p.locator('[data-sid="MOCK_A1"]'),
    { targetPosition: { x: 20, y: 3 } });                      /* A1'in UST yarisina */
  await sleep(500);
  chk('ekranda C3 en uste tasindi',
      JSON.stringify(await domIds()) === JSON.stringify(['MOCK_C3', 'MOCK_A1', 'MOCK_B2']), await domIds());
  chk('sira sunucuya yazildi (tum ekranlar)',
      JSON.stringify(await srvIds()) === JSON.stringify(['MOCK_C3', 'MOCK_A1', 'MOCK_B2']), await srvIds());

  const box = await p.locator('[data-sid="MOCK_B2"]').boundingBox();
  await p.locator('[data-sid="MOCK_C3"]').dragTo(p.locator('[data-sid="MOCK_B2"]'),
    { targetPosition: { x: 20, y: box.height - 3 } });         /* B2'nin ALT yarisina */
  await sleep(500);
  chk('alt yariya birakinca ARKASINA gecti',
      JSON.stringify(await srvIds()) === JSON.stringify(['MOCK_A1', 'MOCK_B2', 'MOCK_C3']), await srvIds());

  console.log('\n=== Kart grafige surukleme hala calisiyor ===');
  await p.locator('[data-sid="MOCK_B2"]').dragTo(p.locator('#plot0'));
  await sleep(400);
  chk('grafige birakinca seri eklendi',
      (await p.evaluate(() => window.CHARTS[0].series.map(s => s.sensor_id))).indexOf('MOCK_B2') >= 0);
  chk('grafige birakmak havuz sirasini bozmadi',
      JSON.stringify(await srvIds()) === JSON.stringify(['MOCK_A1', 'MOCK_B2', 'MOCK_C3']));

  console.log('\n=== Yeni kart en ustte ===');
  await p.click('#newBtn');
  await sleep(150);
  chk('taslak kart listenin EN USTUNDE',
      await p.evaluate(() => document.querySelector('#poolList .scard').classList.contains('draft')));
  await p.locator('#poolList .scard.draft .nmi').fill('Fark');
  await p.locator('#poolList .scard.draft .fxi').fill('=MOCK_A1 - MOCK_ZZ9');
  const hint = await p.locator('#poolList .scard.draft .fxv').textContent();
  chk('havuzda olmayan sensor HATA sayilmiyor',
      !(await p.locator('#poolList .scard.draft .fxi').evaluate(e => e.classList.contains('bad'))), hint);
  chk('onizleme InfluxDB\'den okunacagini soyler', /InfluxDB'den: MOCK_ZZ9/.test(hint), hint);
  await p.locator('#poolList .scard.draft .fxi').press('Enter');
  await sleep(700);
  chk('kaydedilen kart da EN USTTE (sunucu)', (await srvIds())[0] === 'CALC_FARK', await srvIds());
  chk('kaydedilen kart da EN USTTE (ekran)', (await domIds())[0] === 'CALC_FARK', await domIds());

  console.log('\n=== Havuzda olmayan sensorle hesap ===');
  await p.evaluate(() => window.alarmWatch());
  await sleep(800);
  const st = await p.locator('[data-sid="CALC_FARK"] .fxv').textContent();
  chk('kart altinda hata yok', !/!/.test(st), st);
  chk('hesap degeri uretildi (MOCK_ZZ9 kart olmadan okundu)',
      await p.evaluate(() => window.LASTVAL.CALC_FARK != null && isFinite(window.LASTVAL.CALC_FARK)),
      await p.evaluate(() => window.LASTVAL.CALC_FARK));
  chk('MOCK_ZZ9 icin kart ACILMADI', (await srvIds()).indexOf('MOCK_ZZ9') < 0);
  await p.evaluate(() => { window.addSeries(1, 'CALC_FARK'); });
  await p.evaluate(() => window.fetchChart(window.CHARTS[1], true));
  await sleep(1200);
  const pts = await p.evaluate(() => {
    const d = window.CHARTS[1].u && window.CHARTS[1].u.data;
    if (!d) return 0;
    return d[1].filter(v => v != null).length;
  });
  chk('grafikte de hesaplaniyor', pts > 5, pts);

  console.log('\n=== Preset kaydet / yukle: sira korunur ===');
  /* sirayi degistir, kaydet */
  await p.evaluate(() => window.saveOrder(['MOCK_C3', 'MOCK_A1', 'CALC_FARK', 'MOCK_B2']));
  await sleep(400);
  await p.evaluate(() => { window.prompt = () => 'SIRA_TEST'; window.confirm = () => true; });
  await p.evaluate(() => window.savePreset());
  await sleep(500);
  const saved = await p.evaluate(() => fetch('/api/presets/SIRA_TEST').then(r => r.json())
    .then(j => j.preset.pool.map(x => x.influx_name)));
  chk('preset ekrandaki sirayla kaydedildi',
      JSON.stringify(saved) === JSON.stringify(['MOCK_C3', 'MOCK_A1', 'CALC_FARK', 'MOCK_B2']), saved);
  /* sira bozulsun, preset yuklensin */
  await p.evaluate(() => window.saveOrder(['MOCK_B2', 'CALC_FARK', 'MOCK_A1', 'MOCK_C3']));
  await sleep(400);
  await p.evaluate(() => window.loadPreset('SIRA_TEST'));
  await sleep(900);
  chk('preset yukleyince kayitli sira geri geldi',
      JSON.stringify(await srvIds()) === JSON.stringify(['MOCK_C3', 'MOCK_A1', 'CALC_FARK', 'MOCK_B2']),
      await srvIds());
  await p.evaluate(() => fetch('/api/presets/SIRA_TEST', { method: 'DELETE' }));

  console.log('\n=== Preset havuzu yerel siralama ===');
  await p.evaluate(async () => {
    await fetch('/api/sensors/clear', { method: 'POST' });
    window.applyPool([]);
    window.setPresetPool([{ influx_name: 'MOCK_P1' }, { influx_name: 'MOCK_P2' }, { influx_name: 'MOCK_P3' }]);
  });
  await sleep(300);
  await p.locator('[data-sid="MOCK_P3"]').dragTo(p.locator('[data-sid="MOCK_P1"]'),
    { targetPosition: { x: 20, y: 3 } });
  await sleep(300);
  chk('preset kartlari da siralanabiliyor',
      JSON.stringify(await domIds()) === JSON.stringify(['MOCK_P3', 'MOCK_P1', 'MOCK_P2']), await domIds());
  chk('preset siralamasi sunucuya YAZILMADI', (await srvIds()).length === 0);

  chk('sayfada JS hatasi yok', errs.length === 0, errs);

  console.log('\n' + '='.repeat(64));
  console.log('KART SIRASI TESTI:  ' + PASS.length + ' gecti,  ' + FAIL.length + ' kaldi');
  if (FAIL.length) FAIL.forEach(f => console.log('   KALAN: ' + f));
  console.log('='.repeat(64));
  await b.close();
  process.exit(FAIL.length ? 1 : 0);
})();
