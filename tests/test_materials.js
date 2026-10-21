/* ==========================================================================================
   MALZEME TABLOLARI: CSV ice aktarma (pencere), formulde AD_dens(x) / AD_cp(x) ...
   Sunucu 8989'da acik olmali:  python tests/serve_test.py
   ========================================================================================== */
const { chromium } = require('playwright');
const fs = require('fs');
const os = require('os');
const path = require('path');
const sleep = ms => new Promise(r => setTimeout(r, ms));

const PASS = [], FAIL = [];
function chk(name, cond, extra) {
  (cond ? PASS : FAIL).push(name);
  console.log((cond ? '  OK   ' : '  FAIL ') + name +
              ((!cond && extra !== undefined) ? ('  -> ' + JSON.stringify(extra)) : ''));
}
const near = (a, b, eps) => a != null && b != null && Math.abs(a - b) < (eps || 1e-9);

const CSV = [
  'temp [degC],dens [kg/m3],cp [J/kg·K],dvis [Pa·s],cond [W/m·K]',
  '0,998.73,4235.36,0.001779548,0.5567',
  '10,998.50,4189.05,0.001311964,0.5772',
  '20,996.95,4172.15,0.001004854,0.5959',
  '30,994.38,4169.73,0.000796125,0.6126',
  '40,990.99,4174.14,0.00064966,0.6273',
  '50,986.91,4181.46,0.000543684,0.6402',
  '60,982.22,4189.83,0.000464612,0.6511',
  '70,976.95,4198.79,0.00040369,0.6601',
  '80,971.10,4209.07,0.000355098,0.6671',
  '90,964.64,4222.61,0.000314861,0.6723'
].join('\n');

(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1600, height: 1000 } });
  const p2 = await b.newPage({ viewport: { width: 1200, height: 800 } });   /* "baska PC" */
  const errs = [];
  [p, p2].forEach(pg => pg.on('pageerror', e => { errs.push(e.message); console.log('  !! PAGE ERROR: ' + e.message); }));
  await p.goto('http://127.0.0.1:8989', { waitUntil: 'domcontentloaded' });
  await p2.goto('http://127.0.0.1:8989', { waitUntil: 'domcontentloaded' });
  await sleep(1000);

  /* temiz baslangic */
  await p.evaluate(async () => {
    const j = await fetch('/api/materials').then(r => r.json());
    for (const n of Object.keys(j.materials)) await fetch('/api/materials/' + n, { method: 'DELETE' });
    await fetch('/api/sensors/clear', { method: 'POST' });
    window.PPOOL = []; window.DRAFT = null;
    window.CHARTS.forEach(C => { C.series = []; });
    window.applyPool([]);
  });
  await sleep(300);

  console.log('\n=== Pencere ve zorunlu birimler ===');
  await p.click('text=Malzemeler');
  await sleep(200);
  chk('Malzemeler penceresi acildi', await p.locator('#matMask').isVisible());
  const units = await p.locator('#matModal .units').innerText();
  chk('zorunlu birimler ekranda yaziyor',
      ['degC', 'kg/m3', 'J/kg·K', 'Pa·s', 'W/m·K'].every(u => units.indexOf(u) >= 0), units);

  console.log('\n=== Yanlis birimli CSV ===');
  await p.locator('#matName').fill('SU');
  await p.locator('#matCsv').fill(CSV.replace('[kg/m3]', '[g/cm3]'));
  await sleep(700);
  const bad = await p.locator('#matMsg').textContent();
  chk('yanlis birim onizlemede HATA verir', /dens.*kg\/m3/.test(bad) && /HATA/.test(bad), bad);

  console.log('\n=== Dosyadan ice aktarma ===');
  const tmp = path.join(os.tmpdir(), 'SU.csv');
  fs.writeFileSync(tmp, CSV, 'utf8');
  await p.locator('#matName').fill('');
  await p.locator('#matFile').setInputFiles(tmp);
  await sleep(800);
  chk('dosya adindan ad onerildi', (await p.locator('#matName').inputValue()) === 'SU');
  const ok = await p.locator('#matMsg').textContent();
  chk('onizleme: 10 satir, 0..90 degC', /10 satir/.test(ok) && /0 \.\. 90/.test(ok), ok);
  await p.click('#matSave');
  await sleep(700);
  chk('kaydedildi mesaji', /kaydedildi/.test(await p.locator('#matMsg').textContent()));
  chk('listede gorunuyor', /SU/.test(await p.locator('#matList').innerText()));
  chk('sunucuda kayitli (materials.json)', await p.evaluate(() =>
    fetch('/api/materials').then(r => r.json()).then(j => !!j.materials.SU && j.materials.SU.rows.length === 10)));
  chk('baska ekran (WebSocket) aninda gordu', await p2.evaluate(() => !!window.MATERIALS.SU));
  await p.click('#matModal >> text=Kapat');

  console.log('\n=== Formul motoru ===');
  const run = (pg, src, vals) => pg.evaluate(([s, v]) => {
    const c = window.fxCompile(s);
    if (c.err) return { err: c.err };
    return { v: window.fxRun(c.ast, id => (id in v ? v[id] : undefined)), deps: c.deps };
  }, [src, vals || {}]);
  chk('SU_dens(20) = tablodaki deger', near((await run(p, 'SU_dens(20)')).v, 996.95));
  chk('SU_dens(25) = 20 ile 30 arasi dogrusal', near((await run(p, 'SU_dens(25)')).v, (996.95 + 994.38) / 2));
  chk('SU_cp(5) ara deger', near((await run(p, 'SU_cp(5)')).v, (4235.36 + 4189.05) / 2));
  chk('SU_dvis(90) son satir', near((await run(p, 'SU_dvis(90)')).v, 0.000314861));
  chk('SU_cond(0) ilk satir', near((await run(p, 'SU_cond(0)')).v, 0.5567));
  chk('sensor argumani: SU_dens(T1), T1=40', near((await run(p, 'SU_dens(T1)', { T1: 40 })).v, 990.99));
  chk('ifade argumani: SU_dens(T1+5)', near((await run(p, 'SU_dens(T1+5)', { T1: 40 })).v, (990.99 + 986.91) / 2));
  chk('buyuk/kucuk harf onemsiz: su_DENS(20)', near((await run(p, 'su_DENS(20)')).v, 996.95));
  chk('aralik disi (95) bos', (await run(p, 'SU_dens(95)')).v === null);
  chk('aralik disi (-1) bos', (await run(p, 'SU_dens(-1)')).v === null);
  chk('sensor bos ise sonuc bos', (await run(p, 'SU_dens(T1)', {})).v === null);
  chk('bagimlilik yalnizca sensor (fonksiyon adi degil)',
      JSON.stringify((await run(p, 'SU_dens(T1)', { T1: 1 })).deps) === '["T1"]');
  chk('formulde kullanilabilir: SU_dens(T1)*SU_cp(T1)',
      near((await run(p, 'SU_dens(T1)*SU_cp(T1)', { T1: 10 })).v, 998.50 * 4189.05, 1e-6));
  const miss = await run(p, 'YAG_dens(20)');
  chk('olmayan malzeme anlasilir hata', /Malzeme bulunamadi: 'YAG'/.test(miss.err || ''), miss);
  chk('baska ekranda da calisiyor', near((await run(p2, 'SU_dens(25)')).v, (996.95 + 994.38) / 2));

  console.log('\n=== Hesap kartinda kullanim ===');
  await p.click('#newBtn');
  await p.locator('#poolList .scard.draft .nmi').fill('Yogunluk');
  await p.locator('#poolList .scard.draft .uni').fill('kg/m3');
  await p.locator('#poolList .scard.draft .fxi').fill('=SU_dens(MOCK_TEMP_T3)');
  const hint = await p.locator('#poolList .scard.draft .fxv').textContent();
  chk('kart onizlemesi hatasiz', !(await p.locator('#poolList .scard.draft .fxi')
    .evaluate(e => e.classList.contains('bad'))), hint);
  await p.locator('#poolList .scard.draft .fxi').press('Enter');
  await sleep(700);
  chk('kart olustu', (await p.locator('[data-sid="CALC_YOGUNLUK"]').count()) === 1);
  await p.evaluate(() => window.alarmWatch());
  await sleep(900);
  const st = await p.locator('[data-sid="CALC_YOGUNLUK"] .fxv').textContent();
  chk('kart altinda hata yok', !/!/.test(st), st);

  console.log('\n=== Malzeme silinince ===');
  await p.evaluate(() => { window.confirm = () => true; });
  await p.evaluate(() => window.deleteMaterial('SU'));
  await sleep(700);
  chk('sunucudan silindi', await p.evaluate(() =>
    fetch('/api/materials').then(r => r.json()).then(j => !j.materials.SU)));
  chk('baska ekrandan da kalkti', await p2.evaluate(() => !window.MATERIALS.SU));
  const st2 = await p.locator('[data-sid="CALC_YOGUNLUK"] .fxv').textContent();
  chk('onu kullanan kart hata gosteriyor', /Malzeme bulunamadi/.test(st2), st2);

  chk('sayfada JS hatasi yok', errs.length === 0, errs);

  console.log('\n' + '='.repeat(64));
  console.log('MALZEME TESTI:  ' + PASS.length + ' gecti,  ' + FAIL.length + ' kaldi');
  if (FAIL.length) FAIL.forEach(f => console.log('   KALAN: ' + f));
  console.log('='.repeat(64));
  await b.close();
  process.exit(FAIL.length ? 1 : 0);
})();
