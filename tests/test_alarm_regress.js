/* Alarm sistemi gerileme testi -- /api/test/force uc noktasi OLMADAN.
   Degerler tarayicida /api/last yanitini keserek sabitlenir; boylece
   test_ui.js'in 5/10/11 bolumlerindeki senaryolar birebir tekrarlanir. */
const { chromium } = require('playwright');
const sleep = ms => new Promise(r => setTimeout(r, ms));
const PASS = [], FAIL = [];
function chk(n, c, x) {
  (c ? PASS : FAIL).push(n);
  console.log((c ? '  OK   ' : '  FAIL ') + n + ((!c && x !== undefined) ? '  -> ' + JSON.stringify(x) : ''));
}

(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1600, height: 1000 } });
  await p.goto('http://127.0.0.1:8989', { waitUntil: 'domcontentloaded' });
  await sleep(900);

  /* havuzu kur: esikli iki sensor + esiksiz bir sensor */
  await p.evaluate(async () => {
    await fetch('/api/sensors/clear', { method: 'POST' });
    const j = await fetch('/api/webhook/sensors?s=P1|Pompa|Bar|20|80,T3|Sicaklik|C|10|60,N5|Devir|rpm')
      .then(r => r.json());
    window.PPOOL = [];
    window.CHARTS.forEach(C => { C.series = []; });
    window.applyPool(j.sensors);
    window.renderPool(true);
  });
  await sleep(300);

  /* /api/last yanitini biz veriyoruz */
  await p.evaluate(() => {
    window.__vals = { P1: 50, T3: 35, N5: 700 };
    const orig = window.fetch;
    window.fetch = function (u, o) {
      const s = String(u);
      if (s.indexOf('/api/last') === 0) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ values: window.__vals }) });
      }
      return orig.call(this, u, o);
    };
  });
  const force = async v => {
    await p.evaluate(v => { window.__vals = Object.assign({}, window.__vals, v); }, v);
    await p.evaluate(() => window.alarmWatch());
    await sleep(250);
  };

  /* grafige seri koy (cerceve testleri icin) */
  await p.evaluate(() => { window.addSeries(0, 'P1'); window.addSeries(1, 'T3'); });
  await sleep(400);

  console.log('\n=== 5) Temel alarm ===');
  await force({ P1: 50, T3: 35 });
  chk('normal durumda alarm YOK', await p.evaluate(() => Object.keys(window.ALARM).filter(k => window.ALARM[k]).length === 0),
      await p.evaluate(() => window.ALARM));
  chk('normalde havuz karti yanip sonmuyor',
      (await p.locator('[data-sid="P1"]').evaluate(e => e.classList.contains('alarm'))) === false);

  await force({ P1: 140 });
  chk('esik asilinca alarm ACILIR', await p.evaluate(() => window.ALARM.P1 === true));
  chk('havuz karti yanip sonuyor',
      await p.locator('[data-sid="P1"]').evaluate(e => e.classList.contains('alarm')));
  chk('grafik cercevesi yaniyor',
      await p.locator('#card0').evaluate(e => e.classList.contains('alarm')));
  chk('alarm rozeti gorunur',
      await p.locator('#albar').evaluate(e => e.classList.contains('on')));

  console.log('\n=== 10) Alarm TUTMA (en az 3 sn) ===');
  await force({ P1: 50 });
  chk('deger normale dondu ama alarm HALA acik (tutma)',
      await p.evaluate(() => window.ALARM.P1 === true));
  await sleep(1500);
  await p.evaluate(() => window.alarmTick && window.alarmTick());
  chk('1.7 sn sonra hala acik (3 sn dolmadi)',
      await p.evaluate(() => window.ALARM.P1 === true));
  await sleep(2000);
  await p.evaluate(() => { if (window.alarmTick) window.alarmTick(); window.evalAlarms(); });
  await sleep(200);
  chk('3 sn dolunca alarm TEMIZLENDI',
      await p.evaluate(() => !window.ALARM.P1), await p.evaluate(() => window.ALARM));
  chk('kart yanip sonmesi durdu',
      (await p.locator('[data-sid="P1"]').evaluate(e => e.classList.contains('alarm'))) === false);
  chk('grafik cercevesi normale dondu',
      (await p.locator('#card0').evaluate(e => e.classList.contains('alarm'))) === false);
  chk('alarm rozeti gizlendi',
      (await p.locator('#albar').evaluate(e => e.classList.contains('on'))) === false);

  console.log('\n=== 11) Coklu alarm bagimsizligi ===');
  await force({ P1: 140, T3: 35 });
  chk('P1 alarmda', await p.evaluate(() => window.ALARM.P1 === true));
  const anim1 = await p.evaluate(() => {
    const el = document.querySelector('[data-sid="P1"]');
    const a = el.getAnimations ? el.getAnimations() : [];
    return a.length ? a[0].startTime : null;
  });
  chk('P1 yanip sonme animasyonu calisiyor', anim1 !== null, anim1);

  await sleep(400);
  await force({ P1: 140, T3: 95 });
  chk('ikinci sensor de alarma gecti', await p.evaluate(() => window.ALARM.T3 === true));
  chk('ilk sensorun alarmi bozulmadi', await p.evaluate(() => window.ALARM.P1 === true));
  const anim2 = await p.evaluate(() => {
    const el = document.querySelector('[data-sid="P1"]');
    const a = el.getAnimations ? el.getAnimations() : [];
    return a.length ? a[0].startTime : null;
  });
  chk('yeni alarm, devam eden animasyonu SIFIRLAMADI', anim1 !== null && anim2 === anim1, { once: anim1, sonra: anim2 });
  chk('her iki kart da ayni anda yaniyor',
      (await p.locator('[data-sid="P1"]').evaluate(e => e.classList.contains('alarm'))) &&
      (await p.locator('[data-sid="T3"]').evaluate(e => e.classList.contains('alarm'))));
  chk('iki grafik cercevesi de yaniyor',
      (await p.locator('#card0').evaluate(e => e.classList.contains('alarm'))) &&
      (await p.locator('#card1').evaluate(e => e.classList.contains('alarm'))));

  /* T3 normale donsun, tutma dolsun; P1 esik disinda kalsin */
  await force({ P1: 140, T3: 35 });
  await sleep(3400);
  await p.evaluate(() => { if (window.alarmTick) window.alarmTick(); window.evalAlarms(); });
  await sleep(200);
  chk('biri susunca digeri ETKILENMEDI',
      await p.evaluate(() => window.ALARM.P1 === true && !window.ALARM.T3),
      await p.evaluate(() => window.ALARM));
  chk('devam eden sensorun karti hala yaniyor',
      await p.locator('[data-sid="P1"]').evaluate(e => e.classList.contains('alarm')));
  chk('susan sensorun karti sondu',
      (await p.locator('[data-sid="T3"]').evaluate(e => e.classList.contains('alarm'))) === false);

  console.log('\n=== Esigi olmayan sensor ===');
  await force({ N5: 999999 });
  chk('esiksiz sensor alarm vermez', await p.evaluate(() => !window.ALARM.N5));

  console.log('\n=== Grafikte OLMAYAN sensor de izlenir ===');
  await p.evaluate(() => {
    window.CHARTS.forEach(C => { C.series = []; window.renderChips(C); });
  });
  await force({ P1: 140 });
  chk('grafikte olmayan sensor de alarm veriyor', await p.evaluate(() => window.ALARM.P1 === true));
  chk('grafikte olmadigi icin hicbir cerceve yanmiyor',
      (await p.locator('#card0').evaluate(e => e.classList.contains('alarm'))) === false);

  console.log('\n' + '='.repeat(60));
  console.log('ALARM GERILEME:  ' + PASS.length + ' gecti,  ' + FAIL.length + ' kaldi');
  if (FAIL.length) FAIL.forEach(f => console.log('   KALAN: ' + f));
  console.log('='.repeat(60));
  await b.close();
  process.exit(FAIL.length ? 1 : 0);
})();
