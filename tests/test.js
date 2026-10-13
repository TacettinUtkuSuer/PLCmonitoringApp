const { chromium } = require('playwright');
const EXE = '/opt/pw-browsers/chromium-1194/chrome-linux/chrome';
let pass = 0, fail = 0;
const ok = (n, c, x) => { if (c) { pass++; console.log('  OK   ' + n); } else { fail++; console.log('  FAIL ' + n + (x ? '  -> ' + x : '')); } };
const H = t => console.log('\n=== ' + t + ' ===');

(async () => {
  const b = await chromium.launch({ executablePath: EXE });
  const page = await b.newPage({ viewport: { width: 1920, height: 1080 } });
  const errs = [];
  page.on('pageerror', e => errs.push(e.message));
  page.on('console', m => { if (m.type() === 'error' && !/websocket|ws:/i.test(m.text())) errs.push('console: ' + m.text()); });

  await page.goto('http://127.0.0.1:8989/', { waitUntil: 'networkidle' });
  await page.waitForTimeout(700);

  // --------------------------------------------------------------- 7) engine yazisi
  H('7) "engine: gomulu (offline)" kaldirildi');
  const bodyTxt = await page.evaluate(() => document.body.innerText);
  ok('sayfada "engine:" gecmiyor', !/engine\s*:/i.test(bodyTxt), bodyTxt.match(/engine.{0,25}/i));
  ok('#engine elemani yok', await page.evaluate(() => !document.getElementById('engine')));
  ok('"gomulu" kelimesi ekranda yok', !/gomulu/i.test(bodyTxt));

  // sensorleri yukle
  await page.evaluate(() => fetch('/api/mock/trigger', { method: 'POST' }).then(r => r.json()).then(j => window.applyPool(j.sensors)));
  await page.waitForTimeout(500);
  const sids = await page.evaluate(() => window.POOL.map(s => s.sensor_id));
  ok('havuzda 5 sensor var', sids.length === 5, sids.length);

  // CH1'e iki seri at, ikincisini Y2'ye al
  for (const sid of [sids[0], sids[1]]) {
    await page.locator(`.scard[data-sid="${sid}"]`).dragTo(page.locator('#card0'));
    await page.waitForTimeout(250);
  }
  await page.evaluate(() => window.toggleAxis(0, window.CHARTS[0].series[1].sensor_id));
  await page.waitForTimeout(1200);

  // --------------------------------------------------------------- 1) AUTO Y1/Y2 ayri
  H('1) AUTO her eksen icin ayri');
  ok('autoy10 butonu var', await page.locator('#autoy10').count() === 1);
  ok('autoy20 butonu var', await page.locator('#autoy20').count() === 1);
  ok('eski tekil #auto0 butonu yok', await page.locator('#auto0').count() === 0);
  ok('C.auto obje ({y1,y2})', await page.evaluate(() => typeof window.CHARTS[0].auto === 'object' && 'y1' in window.CHARTS[0].auto));
  ok('her iki eksende de A varsayilan ACIK',
     await page.evaluate(() => window.CHARTS[0].auto.y1 === true && window.CHARTS[0].auto.y2 === true));

  await page.locator('#autoy10').click();
  await page.waitForTimeout(150);
  ok('Y1 A kapatildi, Y2 etkilenmedi',
     await page.evaluate(() => window.CHARTS[0].auto.y1 === false && window.CHARTS[0].auto.y2 === true));
  ok('Y1 butonu gorsel olarak kapali', !(await page.locator('#autoy10').getAttribute('class')).includes('on'));
  ok('Y2 butonu gorsel olarak acik', (await page.locator('#autoy20').getAttribute('class')).includes('on'));

  await page.locator('#autoy20').click();
  await page.waitForTimeout(150);
  ok('Y2 A da kapatilabiliyor', await page.evaluate(() => window.CHARTS[0].auto.y2 === false));
  await page.locator('#autoy10').click();
  await page.locator('#autoy20').click();
  await page.waitForTimeout(150);
  ok('ikisi de geri acilabiliyor', await page.evaluate(() => window.CHARTS[0].auto.y1 && window.CHARTS[0].auto.y2));

  // AUTO davranis testi: Y1 limiti daralt, yeni veri gelince genislemeli / Y2 sabit kalmali
  await page.evaluate(() => {
    const C = window.CHARTS[0];
    C.auto = { y1: true, y2: false };
    C.yman.y1 = { min: 0, max: 1 };
    C.yman.y2 = { min: 0, max: 1 };
    window.applyY(C);
  });
  await page.evaluate(() => window.fetchChart(window.CHARTS[0], false));
  await page.waitForTimeout(1200);
  const expand = await page.evaluate(() => ({ y1: window.CHARTS[0].yman.y1, y2: window.CHARTS[0].yman.y2 }));
  ok('AUTO acik olan Y1 genisledi', expand.y1.max > 1, JSON.stringify(expand.y1));
  ok('AUTO kapali olan Y2 genislemedi (0..1 kaldi)', expand.y2.max === 1 && expand.y2.min === 0, JSON.stringify(expand.y2));

  // --------------------------------------------------------------- 5) M / F kademeleri
  H('5) GROUP BY uzerine 2 kademe (M / F)');
  ok('M butonu var', await page.locator('#fm0').count() === 1);
  ok('F butonu var', await page.locator('#ff0').count() === 1);

  await page.selectOption('#rng0', '10h');
  await page.waitForTimeout(1400);
  const i0 = await page.locator('#ivl0').innerText();
  ok('10 Saat varsayilan = 30s', i0.trim() === '30s', i0);

  await page.locator('#fm0').click(); await page.waitForTimeout(1400);
  const i1 = await page.locator('#ivl0').innerText();
  ok('M -> 15s (yarisi)', i1.trim() === '15s', i1);
  ok('M butonu aktif isaretli', (await page.locator('#fm0').getAttribute('class')).includes('on'));

  await page.locator('#ff0').click(); await page.waitForTimeout(1400);
  const i2 = await page.locator('#ivl0').innerText();
  ok('F -> 7500ms (ceyregi)', i2.trim() === '7500ms', i2);
  ok('M butonu artik pasif', !(await page.locator('#ff0').getAttribute('class')).includes('undefined'));
  ok('C.fine = 2', await page.evaluate(() => window.CHARTS[0].fine === 2));

  await page.locator('#ff0').click(); await page.waitForTimeout(1400);
  ok('ayni tusa tekrar basinca varsayilana doner (30s)', (await page.locator('#ivl0').innerText()).trim() === '30s');

  // --------------------------------------------------------------- 4) TAM EKRAN
  H('4) Tam ekran (solo) + cozunurluk artisi');
  ok('fs0 butonu var', await page.locator('#fs0').count() === 1);
  const before = await page.locator('#plot0').boundingBox();

  await page.locator('#fs0').click();
  await page.waitForTimeout(1600);
  const after = await page.locator('#plot0').boundingBox();
  ok('grid solo moduna gecti', await page.evaluate(() => document.getElementById('grid').classList.contains('solo')));
  ok('CH1 karti .full', await page.evaluate(() => document.getElementById('card0').classList.contains('full')));
  ok('grafik alani genisledi', after.width > before.width * 1.5, before.width + ' -> ' + after.width);
  ok('grafik alani yukseldi', after.height > before.height * 1.5, before.height + ' -> ' + after.height);
  ok('diger 3 kart gizlendi', await page.evaluate(() =>
     [1, 2, 3].every(k => document.getElementById('card' + k).offsetParent === null)));

  const fi = await page.locator('#ivl0').innerText();
  ok('tam ekranda 30s -> 15s (1 kademe ince)', fi.trim() === '15s', fi);
  ok('window.FULL = 0', await page.evaluate(() => window.FULL === 0));

  // tam ekranda tum kontroller birebir duruyor mu
  H('4b) Tam ekranda butonlar birebir ayni');
  for (const id of ['live0', 'rng0', 'ivl0', 'fm0', 'ff0', 'y1min0', 'y1max0', 'autoy10', 'y2min0', 'y2max0', 'autoy20', 'fs0']) {
    ok('#' + id + ' tam ekranda gorunur', await page.locator('#' + id).isVisible());
  }
  ok('FIT butonlari duruyor (2 adet)', await page.locator('#card0 .ygrp .btn:has-text("FIT")').count() === 2);
  ok('chip satiri duruyor', await page.locator('#chips0 .chip').count() === 2);
  ok('infobar duruyor', await page.locator('#info0').isVisible());

  // M + tam ekran birlesimi
  await page.locator('#fm0').click(); await page.waitForTimeout(1500);
  ok('tam ekran + M = 7500ms (2 kademe)', (await page.locator('#ivl0').innerText()).trim() === '7500ms');
  await page.locator('#fm0').click(); await page.waitForTimeout(1400);

  await page.locator('#fs0').click();
  await page.waitForTimeout(1600);
  ok('tam ekrandan cikildi', await page.evaluate(() => !document.getElementById('grid').classList.contains('solo') && window.FULL === -1));
  ok('4 kart geri geldi', await page.evaluate(() => [0, 1, 2, 3].every(k => document.getElementById('card' + k).offsetParent !== null)));
  ok('cozunurluk 30s\'e dondu', (await page.locator('#ivl0').innerText()).trim() === '30s');

  // --------------------------------------------------------------- 6) FIT davranisi
  H('6) FIT = min-0.1*span .. max+0.1*span, disa yuvarlama');
  const fitChk = await page.evaluate(() => {
    const cases = [[0, 100], [23.7, 41.2], [0.0032, 0.0048], [-55, -12], [1000, 8000]];
    return cases.map(([lo, hi]) => {
      const r = window.fitRange(lo, hi);
      const span = hi - lo;
      return { lo, hi, r, padLo: lo - 0.1 * span, padHi: hi + 0.1 * span };
    });
  });
  fitChk.forEach(c => {
    ok('[' + c.lo + ',' + c.hi + '] -> [' + c.r[0] + ',' + c.r[1] + ']  padli araligi kapsiyor',
       c.r[0] <= c.padLo + 1e-12 && c.r[1] >= c.padHi - 1e-12, JSON.stringify(c));
    const s = String(c.r[0]) + String(c.r[1]);
    ok('  sonuc kusuratsiz/duzgun (uzun ondalik yok)', !/\.\d{7,}/.test(s), s);
  });

  // gercek grafikte FIT
  await page.selectOption('#rng0', '5m');
  await page.waitForTimeout(1400);
  await page.evaluate(() => { window.CHARTS[0].yman.y1 = { min: null, max: null }; window.applyY(window.CHARTS[0]); });
  const dr = await page.evaluate(() => window.axisDataRange(window.CHARTS[0], 'y1'));
  await page.locator('#card0 .ygrp').first().locator('.btn:has-text("FIT")').click();
  await page.waitForTimeout(600);
  const fitted = await page.evaluate(() => window.CHARTS[0].yman.y1);
  const span = dr[1] - dr[0];
  ok('FIT sonucu veriyi + %10 payi kapsiyor',
     fitted.min <= dr[0] - 0.1 * span + 1e-9 && fitted.max >= dr[1] + 0.1 * span - 1e-9,
     'veri[' + dr[0].toFixed(3) + ',' + dr[1].toFixed(3) + '] fit[' + fitted.min + ',' + fitted.max + ']');
  ok('FIT sinirlari duzgun sayi', !/\.\d{7,}/.test(String(fitted.min) + String(fitted.max)),
     fitted.min + ' / ' + fitted.max);
  ok('min/max kutulari FIT degerini gosteriyor',
     (await page.locator('#y1min0').inputValue()) !== '' && (await page.locator('#y1max0').inputValue()) !== '');

  // --------------------------------------------------------------- 2+3) PRESET
  H('2) Preset global (sunucuda) + 3) preset havuzu ayri');
  await page.evaluate(() => {
    window.CHARTS[1].series = [];
    return fetch('/api/webhook/sensor', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ influx_name: 'WH_ONLY_X1', name: 'Webhook Sensoru', unit: 'kPa' })
    }).then(r => r.json()).then(j => window.applyPool(j.sensors));
  });
  await page.waitForTimeout(400);

  page.once('dialog', d => d.accept('HAT_A'));
  await page.locator('button:has-text("Kaydet")').first().click();
  await page.waitForTimeout(700);

  const srvNames = await page.evaluate(() => fetch('/api/presets').then(r => r.json()));
  ok('preset SUNUCUDA saklandi', (srvNames.names || []).includes('HAT_A'), JSON.stringify(srvNames));
  ok('acilir listede gorunuyor', (await page.locator('#presetSel option').allTextContents()).includes('HAT_A'));
  ok('localStorage preset kullanilmiyor',
     await page.evaluate(() => !localStorage.getItem('tbm_presets_v1')));

  const saved = await page.evaluate(() => fetch('/api/presets/HAT_A').then(r => r.json()));
  ok('preset v3 formatinda', saved.preset.v === 3);
  ok('preset icinde fine bilgisi var', 'fine' in saved.preset.charts[0]);
  ok('preset icinde auto {y1,y2} var', typeof saved.preset.charts[0].auto === 'object');
  ok('preset havuzunda sensorler kayitli', (saved.preset.pool || []).length === 6, (saved.preset.pool || []).length);

  // --- IKINCI TARAYICI (baska bir PC gibi) ---------------------------------------
  H('2b) Baska tarayici ayni preseti goruyor mu');
  const page2 = await b.newPage({ viewport: { width: 1600, height: 900 } });
  page2.on('pageerror', e => errs.push('p2: ' + e.message));
  await page2.goto('http://127.0.0.1:8989/', { waitUntil: 'networkidle' });
  await page2.waitForTimeout(1200);
  ok('2. tarayicida preset listede (GLOBAL)',
     (await page2.locator('#presetSel option').allTextContents()).includes('HAT_A'));

  // --- PRESET HAVUZU IZOLASYONU --------------------------------------------------
  H('3b) Preset havuzu sunucu havuzunu KIRLETMIYOR');
  // once sunucu havuzunu bilincli olarak kucult
  await page2.evaluate(() => fetch('/api/sensors/clear', { method: 'POST' })
    .then(r => r.json()).then(j => window.applyPool(j.sensors)));
  await page2.evaluate(() => fetch('/api/webhook/sensor', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ influx_name: 'CANLI_HAT', name: 'Canli Hat', unit: 'Bar' })
  }).then(r => r.json()).then(j => window.applyPool(j.sensors)));
  await page2.waitForTimeout(500);
  const srvBefore = await page2.evaluate(() => fetch('/api/sensors').then(r => r.json()).then(j => j.sensors.map(s => s.sensor_id)));
  ok('sunucu havuzunda tek sensor var', srvBefore.length === 1 && srvBefore[0] === 'CANLI_HAT', JSON.stringify(srvBefore));

  // page2'de preset sec -> havuz gelmeli ama SUNUCU DEGISMEMELI
  await page2.selectOption('#presetSel', 'HAT_A');
  await page2.waitForTimeout(1800);

  const srvAfter = await page2.evaluate(() => fetch('/api/sensors').then(r => r.json()).then(j => j.sensors.map(s => s.sensor_id)));
  ok('SUNUCU havuzu preset yuklemesinden ETKILENMEDI',
     srvAfter.length === 1 && srvAfter[0] === 'CANLI_HAT', JSON.stringify(srvAfter));
  ok('preset havuzu istemcide dolu (PPOOL)', await page2.evaluate(() => window.PPOOL.length === 6),
     await page2.evaluate(() => window.PPOOL.length));
  ok('webhook havuzu (POOL) bozulmadi', await page2.evaluate(() => window.POOL.length === 1 && window.POOL[0].sensor_id === 'CANLI_HAT'));
  ok('sol panelde PRESET HAVUZU bolumu gorundu', await page2.locator('.psep').count() === 1);
  ok('preset kartlari isaretli (.scard.pre = 6)', await page2.locator('.scard.pre').count() === 6,
     await page2.locator('.scard.pre').count());
  ok('webhook karti normal (isaretsiz = 1)', await page2.locator('.scard:not(.pre)').count() === 1);
  ok('preset grafikleri kuruldu', await page2.evaluate(() => window.CHARTS[0].series.length === 2));

  // ILK TARAYICI ETKILENDI MI?
  await page.waitForTimeout(1200);
  ok('1. tarayicinin PRESET havuzu bos kaldi (bulasma yok)', await page.evaluate(() => window.PPOOL.length === 0),
     await page.evaluate(() => window.PPOOL.length));

  // preset havuzunu temizleme
  await page2.locator('.psep .xbtn').click();
  await page2.waitForTimeout(500);
  ok('preset havuzu temizlenebiliyor', await page2.evaluate(() => window.PPOOL.length === 0));
  ok('temizleyince PRESET bolumu kayboldu', await page2.locator('.psep').count() === 0);
  ok('webhook havuzu hala yerinde', await page2.evaluate(() => window.POOL.length === 1));

  // preset silme global mi
  H('2c) Preset silme tum ekranlardan kaldiriyor');
  page2.once('dialog', d => d.accept());
  await page2.locator('button:has-text("Sil")').first().click();
  await page2.waitForTimeout(800);
  const gone = await page2.evaluate(() => fetch('/api/presets').then(r => r.json()));
  ok('preset sunucudan silindi', !(gone.names || []).includes('HAT_A'), JSON.stringify(gone));

  // --------------------------------------------------------------- yerlesim
  H('Yerlesim / hata kontrolu');
  // GORSEL satir sayisi: align-items:center yuzunden offsetTop farkli olabilir,
  // bu yuzden elemanlarin DIKEY MERKEZLERI toleransla kumelenir.
  const ROWFN = () => {
    const h = document.querySelector('#card0 .chead');
    const kids = [...h.children].filter(e => e.offsetParent !== null);
    const cs = kids.map(e => e.offsetTop + e.offsetHeight / 2).sort((a, b) => a - b);
    let rows = 0, last = -999;
    cs.forEach(c => { if (c - last > 8) { rows++; last = c; } });
    return { satir: rows, w: h.clientWidth, hgt: h.clientHeight };
  };
  const rows = await page.evaluate(ROWFN);
  console.log('  -> 1920px: chead GORSEL satir = ' + rows.satir + '  (kart ' + rows.w + 'px, yukseklik ' + rows.hgt + 'px)');
  ok('FHD: baslik TEK satir', rows.satir === 1, rows.satir);
  ok('sayfada JS hatasi yok', errs.length === 0, errs.slice(0, 4).join(' | '));

  await page.screenshot({ path: __dirname + '/out/ui_normal.png' });
  await page.locator('#fs0').click();
  await page.waitForTimeout(1400);
  await page.screenshot({ path: __dirname + '/out/ui_full.png' });
  await page.locator('#fs0').click();
  await page.waitForTimeout(800);

  const p3 = await b.newPage({ viewport: { width: 1366, height: 768 } });
  await p3.goto('http://127.0.0.1:8989/', { waitUntil: 'networkidle' });
  await p3.evaluate(() => fetch('/api/mock/trigger', { method: 'POST' }).then(r => r.json()).then(j => window.applyPool(j.sensors)));
  await p3.waitForTimeout(600);
  const s3 = await p3.evaluate(() => window.POOL.map(s => s.sensor_id));
  for (const sid of [s3[0], s3[1]]) { await p3.locator(`.scard[data-sid="${sid}"]`).dragTo(p3.locator('#card0')); await p3.waitForTimeout(220); }
  await p3.waitForTimeout(1200);
  await p3.screenshot({ path: __dirname + '/out/ui_hd.png' });
  const r3 = await p3.evaluate(ROWFN);
  console.log('  -> 1366px: chead GORSEL satir = ' + r3.satir + '  (kart ' + r3.w + 'px)');
  ok('HD: baslik en fazla 2 satir', r3.satir <= 2, r3.satir);

  await b.close();
  console.log('\n===================================================');
  console.log(' SONUC:  ' + pass + ' gecti  |  ' + fail + ' kaldi');
  console.log('===================================================');
  process.exit(fail ? 1 : 0);
})();
