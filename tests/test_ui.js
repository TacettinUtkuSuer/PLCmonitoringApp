/* Test Bench Monitor -- yeni isterlerin arayuz testleri */
const { chromium } = require('playwright');

const BASE = 'http://127.0.0.1:8989';
let PASS = 0, FAIL = [];

function chk(name, cond, extra) {
  if (cond) { PASS++; console.log('  OK   ' + name); }
  else { FAIL.push(name); console.log('  FAIL ' + name + (extra !== undefined ? ('  -> ' + JSON.stringify(extra)) : '')); }
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

/* Stub'da WebSocket yok: sunucu havuzunu istemciye elle uygula
   (gercek uygulamada bunu /ws yayini yapar). */
async function syncPool(page) {
  await page.evaluate(() => fetch('/api/sensors')
    .then(r => r.json())
    .then(j => window.applyPool(j.sensors || [])));
}

async function force(page, values) {
  await page.evaluate(v => fetch('/api/test/force', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ values: v })
  }).then(r => r.json()), values);
}

(async () => {
  const browser = await chromium.launch({ args: ['--autoplay-policy=no-user-gesture-required'] });
  const ctx = await browser.newContext({ viewport: { width: 1920, height: 1080 }, acceptDownloads: true });
  const page = await ctx.newPage();

  const jsErrors = [];
  page.on('pageerror', e => jsErrors.push(String(e)));
  page.on('console', m => { if (m.type() === 'error') jsErrors.push('console: ' + m.text()); });

  await page.goto(BASE, { waitUntil: 'networkidle' });
  await page.evaluate(() => fetch('/api/sensors/clear', { method: 'POST' }));
  await page.evaluate(() => fetch('/api/test/force', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ values: {} })
  }));
  await page.reload({ waitUntil: 'networkidle' });
  await sleep(600);

  /* ==================================================================== 1) FIT + AUTO */
  console.log('\n=== 1) FIT / AUTO: 0.1x pay + duzgun yuvarlama ===');

  const fit = await page.evaluate(() => {
    const f = window.fitRange;
    return {
      a: f(10, 20),        // span 10 -> pay 1 -> 9..21
      b: f(0, 100),        // span 100 -> pay 10 -> -10..110
      c: f(23.87451, 47.13298),
      d: f(0.0012, 0.0048),
      e: f(-50, -10),
      same: f(5, 5),
      bad: f(null, 10)
    };
  });
  chk('fitRange(10,20) alt <= 9', fit.a[0] <= 9 && fit.a[0] > 8, fit.a);
  chk('fitRange(10,20) ust >= 21', fit.a[1] >= 21 && fit.a[1] < 22, fit.a);
  chk('fitRange(0,100) -> [-10,110] civari', fit.b[0] <= -10 && fit.b[1] >= 110, fit.b);
  chk('kusuratli giris duzgun sayiya yuvarlanir',
    Number.isFinite(fit.c[0]) && String(fit.c[0]).replace('-', '').replace('.', '').length <= 6, fit.c);
  chk('kusuratli alt sinir ASAGIYA yuvarlanir', fit.c[0] <= 23.87451 - 0.1 * (47.13298 - 23.87451) + 1e-9, fit.c);
  chk('kusuratli ust sinir YUKARIYA yuvarlanir', fit.c[1] >= 47.13298 + 0.1 * (47.13298 - 23.87451) - 1e-9, fit.c);
  chk('cok kucuk degerlerde de calisir', fit.d[0] < 0.0012 && fit.d[1] > 0.0048, fit.d);
  chk('negatif aralik dogru', fit.e[0] <= -54 && fit.e[1] >= -6, fit.e);
  chk('min==max cokmez', Array.isArray(fit.same) && fit.same[1] > fit.same[0], fit.same);
  chk('gecersiz giris null doner', fit.bad === null, fit.bad);

  const mk = await page.evaluate(() => {
    /* mkRange elle limit YOKKEN fitRange ile ayni sonucu vermeli (AUTO = FIT) */
    const C = { yman: { y1: { min: null, max: null }, y2: { min: null, max: null } } };
    const rng = window.mkRange(C, 'y1');
    return { auto: rng(null, 10, 20), fit: window.fitRange(10, 20) };
  });
  chk('AUTO ekseni FIT ile AYNI araligi uretir',
    mk.auto[0] === mk.fit[0] && mk.auto[1] === mk.fit[1], mk);

  const mkMan = await page.evaluate(() => {
    const C = { yman: { y1: { min: 3, max: 77 }, y2: { min: null, max: null } } };
    return window.mkRange(C, 'y1')(null, 10, 20);
  });
  chk('elle girilen limit AUTO formulunu EZER', mkMan[0] === 3 && mkMan[1] === 77, mkMan);

  /* ==================================================== 2) Aciklama yazilari kaldirildi */
  console.log('\n=== 2) Gereksiz aciklama yazilari ===');
  const body = await page.evaluate(() => document.body.innerText);
  chk('"surukleyip birakin ... Renk kutusu" yazisi YOK', !/Renk kutusu tum grafikleri etkiler/.test(body));
  chk('"Preset havuzu yalnizca bu ekranda" yazisi YOK', !/Preset havuzu yalnizca bu ekranda gecerlidir/.test(body));
  chk('#poolFoot elemani kaldirildi', await page.evaluate(() => !document.getElementById('poolFoot')));

  /* ===================================================== 3) GET webhook (tek URL) */
  console.log('\n=== 3) GET webhook -- tarayiciya yazilabilen tek URL ===');
  const w1 = await page.evaluate(() =>
    fetch('/api/webhook/sensor?influx_name=MOCK_PRESSURE_P1&name=Pompa Basinci&unit=Bar&min=0&max=250')
      .then(r => r.json()));
  chk('GET ile sensor eklendi (header/body YOK)', w1.ok === true && w1.created === true, w1);
  chk('GET ile esikler de yazildi', w1.min === 0 && w1.max === 250, w1);

  const w2 = await page.evaluate(() =>
    fetch('/api/webhook/sensors?s=MOCK_TEMP_T3|Yag Sicakligi|C|-10|120,MOCK_RPM_N5|Devir|rpm')
      .then(r => r.json()));
  chk('tek URL ile coklu ekleme', w2.added.length === 2, w2.added);
  await syncPool(page);
  await sleep(400);
  const poolIds = await page.evaluate(() => window.POOL.map(s => s.sensor_id));
  chk('havuz 3 sensore ulasti', poolIds.length === 3, poolIds);
  chk('esikler istemciye ulasti', await page.evaluate(() => {
    const l = window.limitsOf('MOCK_PRESSURE_P1'); return l.min === 0 && l.max === 250;
  }));

  /* ====================================================== 4) Havuzda esik giris alanlari */
  console.log('\n=== 4) Havuz kartinda alarm min/max kutulari ===');
  await page.waitForSelector('#poolList .scard', { timeout: 4000 });
  chk('her kartta ALARM satiri var',
    await page.evaluate(() => document.querySelectorAll('#poolList .scard .lrow').length === 3));
  chk('min kutusu deger gosteriyor',
    await page.evaluate(() => document.querySelector('.scard[data-sid="MOCK_PRESSURE_P1"] .lo').value) === '0');
  chk('max kutusu deger gosteriyor',
    await page.evaluate(() => document.querySelector('.scard[data-sid="MOCK_PRESSURE_P1"] .hi').value) === '250');
  chk('dolu kutu "set" sinifi aliyor',
    await page.evaluate(() => document.querySelector('.scard[data-sid="MOCK_PRESSURE_P1"] .lo').classList.contains('set')));
  chk('bos kutu "set" sinifi ALMIYOR',
    await page.evaluate(() => !document.querySelector('.scard[data-sid="MOCK_RPM_N5"] .lo').classList.contains('set')));

  /* kutudan elle esik girisi sunucuya yazilmali */
  await page.fill('.scard[data-sid="MOCK_RPM_N5"] .hi', '1500');
  await page.dispatchEvent('.scard[data-sid="MOCK_RPM_N5"] .hi', 'change');
  await sleep(500);
  const srv = await page.evaluate(() => fetch('/api/sensors').then(r => r.json()));
  const n5 = srv.sensors.find(s => s.sensor_id === 'MOCK_RPM_N5');
  chk('UI\'dan girilen esik SUNUCUYA yazildi', n5 && n5.amax === 1500, n5);
  chk('diger esige dokunulmadi', n5 && n5.amin === null, n5);

  /* ================================================================ 5) Alarm tetikleme */
  console.log('\n=== 5) Alarm: yanip sonme + sesli ikaz ===');
  /* Deterministik baslangic: tum sensorleri esik ICINDE bir degere sabitle.
     (dogal sinus dalgasi zamana gore esigi kendiliginden asabiliyor) */
  const SAFE = { MOCK_PRESSURE_P1: 100, MOCK_TEMP_T3: 50, MOCK_RPM_N5: 700 };
  await force(page, SAFE);
  /* P1'i grafige ekle, sonra degerini esik disina zorla */
  await page.evaluate(() => window.addSeries(0, 'MOCK_PRESSURE_P1'));
  await page.evaluate(() => window.alarmWatch());
  await sleep(700);
  chk('normal durumda alarm YOK', await page.evaluate(() => Object.keys(window.ALARM).length === 0));
  chk('normalde havuz karti yanip sonmuyor',
    await page.evaluate(() => !document.querySelector('.scard[data-sid="MOCK_PRESSURE_P1"]').classList.contains('alarm')));

  await force(page, { MOCK_PRESSURE_P1: 900 });      // max=250 -> asildi
  await page.evaluate(() => window.alarmWatch());
  await sleep(600);

  chk('esik asilinca ALARM durumu olustu',
    await page.evaluate(() => window.ALARM['MOCK_PRESSURE_P1'] === true),
    await page.evaluate(() => ({ alarm: window.ALARM, last: window.LASTVAL })));
  chk('havuz karti "alarm" sinifi aldi (yanip soner)',
    await page.evaluate(() => document.querySelector('.scard[data-sid="MOCK_PRESSURE_P1"]').classList.contains('alarm')));
  chk('serinin bulundugu grafik cercevesi "alarm" sinifi aldi',
    await page.evaluate(() => document.getElementById('card0').classList.contains('alarm')));
  chk('seride olmayan grafik cercevesi ETKILENMEDI',
    await page.evaluate(() => !document.getElementById('card1').classList.contains('alarm')));
  chk('ust bardaki alarm rozeti gorunur',
    await page.evaluate(() => document.getElementById('albar').classList.contains('on')));
  chk('rozet sensor adini yaziyor',
    (await page.evaluate(() => document.getElementById('altxt').textContent)).indexOf('Pompa') >= 0);
  chk('sesli ikaz zamanlayicisi calisiyor',
    await page.evaluate(() => window.ALTIMER !== null));

  /* alt esik testi */
  await force(page, { MOCK_PRESSURE_P1: -5 });       // min=0 -> altina dustu
  await page.evaluate(() => window.alarmWatch());
  await sleep(500);
  chk('ALT esik ihlali de alarm veriyor',
    await page.evaluate(() => window.ALARM['MOCK_PRESSURE_P1'] === true));

  /* normale don -- 3 sn TUTMA suresi dolana kadar bekle (bolum 10 bunu ayrica sinar) */
  await force(page, SAFE);
  await page.evaluate(() => window.alarmWatch());
  await sleep(3500);
  chk('deger normale donup tutma dolunca alarm TEMIZLENDI',
    await page.evaluate(() => Object.keys(window.ALARM).length === 0),
    await page.evaluate(() => ({ alarm: window.ALARM, last: window.LASTVAL })));
  chk('kart yanip sonmesi durdu',
    await page.evaluate(() => !document.querySelector('.scard[data-sid="MOCK_PRESSURE_P1"]').classList.contains('alarm')));
  chk('grafik cercevesi normale dondu',
    await page.evaluate(() => !document.getElementById('card0').classList.contains('alarm')));
  chk('ses zamanlayicisi durdu', await page.evaluate(() => window.ALTIMER === null));
  chk('alarm rozeti gizlendi',
    await page.evaluate(() => !document.getElementById('albar').classList.contains('on')));

  /* grafikte OLMAYAN sensor de izleniyor mu */
  await force(page, Object.assign({}, SAFE, { MOCK_RPM_N5: 9999 }));  // max=1500, grafige eklenmedi
  await page.evaluate(() => window.alarmWatch());
  await sleep(500);
  chk('GRAFIKTE OLMAYAN sensor de alarm veriyor',
    await page.evaluate(() => window.ALARM['MOCK_RPM_N5'] === true),
    await page.evaluate(() => window.LASTVAL));
  chk('grafikte olmadigi icin hicbir cerceve yanmiyor',
    await page.evaluate(() => !document.getElementById('card0').classList.contains('alarm')));
  await force(page, SAFE);
  await page.evaluate(() => window.alarmWatch());
  await sleep(3500);

  /* sessize alma */
  console.log('\n--- sessize alma ---');
  chk('varsayilan ses ACIK', await page.evaluate(() => window.MUTED === false));
  await page.click('#muteBtn');
  chk('mute butonu sesi kapatti', await page.evaluate(() => window.MUTED === true));
  chk('mute butonu "on" sinifi aldi',
    await page.evaluate(() => document.getElementById('muteBtn').classList.contains('on')));
  chk('tercih localStorage\'a yazildi',
    await page.evaluate(() => localStorage.getItem('tbm_muted') === '1'));
  await page.click('#muteBtn');
  chk('tekrar tiklamak sesi aciyor', await page.evaluate(() => window.MUTED === false));

  /* =========================================================== 6) Esik cizgileri */
  console.log('\n=== 6) Grafikte yatay esik cizgileri ===');
  const lines = await page.evaluate(() => {
    const C = window.CHARTS[0];
    const out = [];
    C.series.forEach(s => {
      const l = window.limitsOf(s.sensor_id);
      if (l.min != null) out.push({ sid: s.sensor_id, v: l.min, axis: s.axis });
      if (l.max != null) out.push({ sid: s.sensor_id, v: l.max, axis: s.axis });
    });
    return out;
  });
  chk('grafik 0 icin 2 esik cizgisi hesaplaniyor', lines.length === 2, lines);
  chk('drawLimits fonksiyonu mevcut', await page.evaluate(() => typeof window.drawLimits === 'function'));
  chk('motor valToPos destekliyor',
    await page.evaluate(() => typeof window.CHARTS[0].u.valToPos === 'function'));
  chk('valToPos makul piksel donduruyor', await page.evaluate(() => {
    const u = window.CHARTS[0].u;
    const y = u.valToPos(u.scales.y1.min, 'y1', true);
    return Number.isFinite(y) && y > 0;
  }));
  /* draw kancasi gercekten cagriliyor mu */
  chk('draw kancasi redraw sirasinda tetikleniyor', await page.evaluate(() => {
    let hit = 0;
    const C = window.CHARTS[0];
    const orig = window.drawLimits;
    window.drawLimits = function () { hit++; return orig.apply(this, arguments); };
    C.u.redraw();
    window.drawLimits = orig;
    return hit > 0;
  }));
  /* cizgiler gorsel olarak canvas'a dusuyor mu: esik degerini gorunur araliga cek */
  chk('esik cizgisi canvas pikselini degistiriyor', await page.evaluate(async () => {
    const C = window.CHARTS[0];
    const u = C.u;
    const cv = u.canvas || u.ctx.canvas;
    const shot = () => u.ctx.getImageData(0, 0, cv.width, cv.height).data.slice();
    const mid = (u.scales.y1.min + u.scales.y1.max) / 2;
    const s = window.POOL.find(x => x.sensor_id === 'MOCK_PRESSURE_P1');
    const keepMin = s.amin, keepMax = s.amax;
    s.amin = null; s.amax = null;
    u.redraw();
    const before = shot();
    s.amax = mid;                   /* tam ortaya bir esik koy */
    u.redraw();
    const after = shot();
    s.amin = keepMin; s.amax = keepMax; u.redraw();
    let diff = 0;
    for (let i = 0; i < before.length; i += 4) if (before[i] !== after[i]) diff++;
    return diff > 50;
  }));

  /* =============================================================== 7) CSV / PNG */
  console.log('\n=== 7) CSV ve PNG indirme (her grafik icin ayri) ===');
  chk('her grafikte CSV butonu var',
    await page.evaluate(() => document.querySelectorAll('button[onclick^="window.exportCsv"]').length === 4));
  chk('her grafikte PNG butonu var',
    await page.evaluate(() => document.querySelectorAll('button[onclick^="window.exportPng"]').length === 4));

  const dlCsv = page.waitForEvent('download', { timeout: 8000 });
  await page.click('button[onclick="window.exportCsv(0)"]');
  const csv = await dlCsv;
  chk('CSV indirildi', /^grafik1_\d{8}_\d{6}\.csv$/.test(csv.suggestedFilename()), csv.suggestedFilename());
  const fs = require('fs');
  const csvPath = await csv.path();
  const text = fs.readFileSync(csvPath, 'utf8');
  chk('CSV BOM ile basliyor (Excel/Turkce)', text.charCodeAt(0) === 0xFEFF);
  const rows = text.replace(/^﻿/, '').trim().split('\r\n');
  chk('CSV basligi dogru', rows[0].split(';')[0] === 'Zaman' && rows[0].split(';')[1] === 'Epoch', rows[0]);
  chk('CSV basliginda ad + INFLUXDB ADI + birim var',
    /Pompa Basinci \(MOCK_PRESSURE_P1\) \[Bar\]/.test(rows[0]), rows[0]);
  chk('CSV basligindaki her seri sutununda influx adi parantez icinde',
    rows[0].split(';').slice(2).every(h => /\([A-Za-z0-9_.\-]+\)/.test(h)), rows[0]);
  chk('CSV birden fazla veri satiri iceriyor', rows.length > 10, rows.length);
  chk('CSV ondalik ayraci virgul', /\d+,\d+/.test(rows[1]), rows[1]);
  chk('CSV sutun sayisi tutarli',
    rows.slice(1, 20).every(r => r.split(';').length === rows[0].split(';').length));

  const dlPng = page.waitForEvent('download', { timeout: 8000 });
  await page.click('button[onclick="window.exportPng(0)"]');
  const png = await dlPng;
  chk('PNG indirildi', /^grafik1_\d{8}_\d{6}\.png$/.test(png.suggestedFilename()), png.suggestedFilename());
  const pngBuf = fs.readFileSync(await png.path());
  chk('PNG imzasi gecerli',
    pngBuf[0] === 0x89 && pngBuf[1] === 0x50 && pngBuf[2] === 0x4E && pngBuf[3] === 0x47);
  chk('PNG bos degil (>5KB)', pngBuf.length > 5000, pngBuf.length);

  /* bos grafikte CSV hata vermeli, cokmemeli */
  await page.click('button[onclick="window.exportCsv(3)"]');
  await sleep(300);
  chk('bos grafikte CSV uyari veriyor, cokmuyor',
    await page.evaluate(() => document.getElementById('errbar').style.display === 'block'));
  await page.evaluate(() => window.hideError());

  /* ======================================================= 8) Preset esikleri tasir */
  console.log('\n=== 8) Preset esikleri tasiyor ===');
  const pres = await page.evaluate(() => {
    const out = [];
    window.allSensors().forEach(s => {
      const l = window.limitsOf(s.sensor_id);
      out.push({ influx_name: s.sensor_id, min: l.min, max: l.max });
    });
    return out;
  });
  chk('preset govdesi esikleri iceriyor',
    pres.find(p => p.influx_name === 'MOCK_PRESSURE_P1').max === 250, pres);
  const rt = await page.evaluate(() => {
    window.setPresetPool([{ influx_name: 'PRESET_ONLY_X', name: 'Gecici', unit: 'V', min: 1.5, max: 8.5 }]);
    const l = window.limitsOf('PRESET_ONLY_X');
    return { l: l, onlyPreset: window.isPresetOnly('PRESET_ONLY_X') };
  });
  chk('preset havuzu sensoru esiklerini koruyor', rt.l.min === 1.5 && rt.l.max === 8.5, rt);
  chk('preset sensoru yalnizca bu ekranda', rt.onlyPreset === true);
  const before = await page.evaluate(() => fetch('/api/sensors').then(r => r.json()).then(j => j.sensors.length));
  await page.evaluate(() => window.setLimit('PRESET_ONLY_X', 'max', '99'));
  await sleep(400);
  const after = await page.evaluate(() => fetch('/api/sensors').then(r => r.json()).then(j => j.sensors.length));
  chk('preset sensorunun esigi SUNUCUYA yazilmiyor', before === after, { before, after });
  chk('preset sensorunun esigi yerelde guncellendi',
    await page.evaluate(() => window.limitsOf('PRESET_ONLY_X').max === 99));
  /* sonraki bolumleri kirletmesin: gecici preset sensorunu kaldir */
  await page.evaluate(() => window.clearPresetPool());
  await sleep(300);

  /* ================================================================= 9) Regresyon */
  console.log('\n=== 9) Mevcut ozellikler bozulmadi ===');
  chk('4 grafik karti duruyor', await page.evaluate(() => document.querySelectorAll('#grid .card').length === 4));
  chk('per-eksen AUTO butonlari duruyor',
    await page.evaluate(() => !!document.getElementById('autoy10') && !!document.getElementById('autoy20')));
  chk('M / F cozunurluk butonlari duruyor',
    await page.evaluate(() => !!document.getElementById('fm0') && !!document.getElementById('ff0')));
  chk('tam ekran butonu duruyor', await page.evaluate(() => !!document.getElementById('fs0')));
  await page.evaluate(() => window.toggleFull(0));
  await sleep(400);
  chk('tam ekran calisiyor', await page.evaluate(() => window.FULL === 0 &&
    document.getElementById('grid').classList.contains('solo')));
  chk('tam ekranda cozunurluk 1 kademe artiyor',
    await page.evaluate(() => window.effFine(window.CHARTS[0]) === 1));
  await page.evaluate(() => window.toggleFull(0));
  await sleep(300);
  chk('tam ekrandan cikildi', await page.evaluate(() => window.FULL === -1));
  chk('FIT butonu calisiyor', await page.evaluate(() => {
    window.fitAxis(0, 'y1');
    const m = window.CHARTS[0].yman.y1;
    return m.min != null && m.max != null && m.max > m.min;
  }));
  chk('LIVE/PAUSE calisiyor', await page.evaluate(() => {
    const C = window.CHARTS[0]; const was = C.live;
    window.toggleChartLive(0); const now = C.live;
    window.setChartLive(C, was); return was !== now;
  }));

  /* ---------------------------------------------------------------- basliktaki tasma */
  /* Satir sayisini yuksekligden olc: tek satir ~34px, her ek satir ~+25px */
  const headH = await page.evaluate(() =>
    Math.round(document.querySelector('#card0 .chead').getBoundingClientRect().height));
  chk('grafik basligi TEK satir (FHD)', headH <= 40, headH + 'px');
  chk('grafik alani basliktan cok daha buyuk (FHD)', await page.evaluate(() => {
    const card = document.getElementById('card0').getBoundingClientRect().height;
    const head = document.querySelector('#card0 .chead').getBoundingClientRect().height;
    return head / card < 0.15;
  }));

  await page.screenshot({ path: __dirname + '/out/new_fhd.png' });

  /* HD cozunurlukte de kontrol */
  await page.setViewportSize({ width: 1366, height: 768 });
  await sleep(500);
  const headHd = await page.evaluate(() =>
    Math.round(document.querySelector('#card0 .chead').getBoundingClientRect().height));
  chk('grafik basligi HD\'de en fazla 2 satir', headHd <= 66, headHd + 'px');
  chk('HD\'de tum baslik butonlari gorunur', await page.evaluate(() => {
    const need = ['live0', 'rng0', 'ivl0', 'fm0', 'ff0', 'y1min0', 'y2max0', 'fs0'];
    return need.every(id => {
      const e = document.getElementById(id);
      return e && e.getBoundingClientRect().width > 0;
    }) && document.querySelectorAll('#card0 .chead button[onclick^="window.export"]').length === 2;
  }));
  await page.screenshot({ path: __dirname + '/out/new_hd.png' });

  /* ======================================================= 10) ALARM TUTMA (LATCH) */
  console.log('\n=== 10) Alarm tutma: anlik sicrama gozden kacmaz ===');
  await page.setViewportSize({ width: 1920, height: 1080 });
  await sleep(400);
  await page.evaluate(() => { window.CHARTS.forEach(C => window.setChartLive(C, false)); });

  const HOLD = await page.evaluate(() => window.ALARM_HOLD_MS);
  chk('tutma suresi 3 saniye', HOLD === 3000, HOLD);

  await force(page, SAFE);
  await page.evaluate(() => window.alarmWatch());
  await sleep(400);
  chk('baslangicta alarm yok', await page.evaluate(() => Object.keys(window.ALARM).length === 0));

  /* TEK bir olcumde esik asilsin, hemen normale donsun */
  await force(page, Object.assign({}, SAFE, { MOCK_PRESSURE_P1: 900 }));
  await page.evaluate(() => window.alarmWatch());
  await sleep(350);
  const t0 = Date.now();
  chk('anlik sicrama alarmi TETIKLER',
    await page.evaluate(() => window.ALARM['MOCK_PRESSURE_P1'] === true));

  await force(page, SAFE);                      /* deger hemen normale dondu */
  await page.evaluate(() => window.alarmWatch());
  await sleep(500);
  chk('deger normale donse bile alarm HALA acik (tutma)',
    await page.evaluate(() => window.ALARM['MOCK_PRESSURE_P1'] === true),
    { gecen: Date.now() - t0 });
  chk('tutma sirasinda kart yanip sonmeye devam ediyor',
    await page.evaluate(() => document.querySelector('.scard[data-sid="MOCK_PRESSURE_P1"]').classList.contains('alarm')));
  chk('tutma sirasinda ses devam ediyor', await page.evaluate(() => window.ALTIMER !== null));

  await sleep(1200);
  chk('1.7 sn sonra hala acik (3 sn dolmadi)',
    await page.evaluate(() => window.ALARM['MOCK_PRESSURE_P1'] === true),
    { gecen: Date.now() - t0 });

  await sleep(1900);                            /* toplam > 3 sn */
  chk('3 sn dolunca alarm KENDILIGINDEN sonuyor',
    await page.evaluate(() => Object.keys(window.ALARM).length === 0),
    { gecen: Date.now() - t0, alarm: await page.evaluate(() => window.ALARM) });
  chk('sure dolunca kart normale dondu',
    await page.evaluate(() => !document.querySelector('.scard[data-sid="MOCK_PRESSURE_P1"]').classList.contains('alarm')));
  chk('sure dolunca ses durdu', await page.evaluate(() => window.ALTIMER === null));

  /* esik disinda KALIRSA alarm hic sonmemeli */
  await force(page, Object.assign({}, SAFE, { MOCK_PRESSURE_P1: 900 }));
  await page.evaluate(() => window.alarmWatch());
  await sleep(400);
  await page.evaluate(() => window.alarmWatch());
  await sleep(3400);
  chk('esik disinda kaldigi surece alarm SURUYOR',
    await page.evaluate(() => window.ALARM['MOCK_PRESSURE_P1'] === true));

  /* ================================================ 11) ALARMLAR BIRBIRINI ETKILEMEZ */
  console.log('\n=== 11) Coklu alarm: birbirini etkilemez, her biri kendi ritminde ===');
  const animOf = sid => page.evaluate(s => {
    const el = document.querySelector('.scard[data-sid="' + s + '"]');
    const a = el.getAnimations ? el.getAnimations() : [];
    return a.length ? { name: a[0].animationName, start: a[0].startTime } : null;
  }, sid);

  const aBefore = await animOf('MOCK_PRESSURE_P1');
  chk('P1 yanip sonme animasyonu calisiyor', aBefore && aBefore.name === 'alarmCard', aBefore);

  /* P1 alarmdayken IKINCI sensoru de alarma sok */
  await force(page, { MOCK_PRESSURE_P1: 900, MOCK_TEMP_T3: 999, MOCK_RPM_N5: 700 });
  await page.evaluate(() => window.alarmWatch());
  await sleep(600);

  chk('ikinci sensor de alarma gecti',
    await page.evaluate(() => window.ALARM['MOCK_TEMP_T3'] === true));
  chk('ilk sensorun alarmi bozulmadi',
    await page.evaluate(() => window.ALARM['MOCK_PRESSURE_P1'] === true));

  const aAfter = await animOf('MOCK_PRESSURE_P1');
  chk('yeni alarm, devam eden animasyonu SIFIRLAMADI',
    aAfter && aBefore && aAfter.start === aBefore.start, { once: aBefore, sonra: aAfter });
  chk('havuz karti DOM olarak yeniden uretilmedi', await page.evaluate(() => {
    const el = document.querySelector('.scard[data-sid="MOCK_PRESSURE_P1"]');
    el.dataset.probe = 'x';
    window.renderPool();                 /* yapi degismedi -> yeniden kurulmamali */
    const el2 = document.querySelector('.scard[data-sid="MOCK_PRESSURE_P1"]');
    return el2 === el && el2.dataset.probe === 'x';
  }));
  chk('her iki sensorun karti da ayni anda yanip soner', await page.evaluate(() =>
    ['MOCK_PRESSURE_P1', 'MOCK_TEMP_T3'].every(s =>
      document.querySelector('.scard[data-sid="' + s + '"]').classList.contains('alarm')) &&
    !document.querySelector('.scard[data-sid="MOCK_RPM_N5"]').classList.contains('alarm')));

  /* biri normale donunce digeri etkilenmemeli */
  const t1 = Date.now();
  await force(page, { MOCK_PRESSURE_P1: 900, MOCK_TEMP_T3: 50, MOCK_RPM_N5: 700 });
  await page.evaluate(() => window.alarmWatch());
  await sleep(3500);
  chk('biri susunca digeri ETKILENMEDI',
    await page.evaluate(() => window.ALARM['MOCK_PRESSURE_P1'] === true &&
                              !window.ALARM['MOCK_TEMP_T3']),
    await page.evaluate(() => window.ALARM));
  chk('susan sensorun karti normale dondu',
    await page.evaluate(() => !document.querySelector('.scard[data-sid="MOCK_TEMP_T3"]').classList.contains('alarm')));
  chk('devam eden sensorun karti hala yaniyor',
    await page.evaluate(() => document.querySelector('.scard[data-sid="MOCK_PRESSURE_P1"]').classList.contains('alarm')));

  await force(page, SAFE);
  await page.evaluate(() => window.alarmWatch());
  await sleep(3400);

  /* ==================================================== 12) SESLI IKAZ SIDDETI */
  console.log('\n=== 12) Sesli ikaz: daha yuksek ve belirgin ===');
  const audio = await page.evaluate(() => {
    const ctx = window.audioCtx();
    let osc = 0, gains = [], freqs = [], masterVol = null;
    const oOsc = ctx.createOscillator.bind(ctx);
    const oGain = ctx.createGain.bind(ctx);
    let first = true;
    ctx.createOscillator = function () { osc++; const o = oOsc(); freqs.push(o.frequency); return o; };
    ctx.createGain = function () {
      const g = oGain();
      if (first) { masterVol = g; first = false; }
      gains.push(g); return g;
    };
    window.beep();
    const out = {
      osc: osc,
      gains: gains.length,
      master: masterVol ? masterVol.gain.value : null,
      freqs: freqs.map(f => Math.round(f.value))
    };
    ctx.createOscillator = oOsc; ctx.createGain = oGain;
    return out;
  });
  chk('tek bip degil, cok darbeli (>=6 osilator)', audio.osc >= 6, audio);
  chk('iki farkli ton kullaniliyor (siren)', new Set(audio.freqs).size >= 2, audio.freqs);
  chk('tonlar tiz bantta (>1000 Hz)', audio.freqs.every(f => f > 1000), audio.freqs);
  chk('ses seviyesi eskisinden cok daha yuksek', audio.master >= 0.5, audio.master);

  chk('Ayarlar penceresinde ses seviyesi alani var',
    await page.evaluate(() => !!document.getElementById('cfgVol')));
  await page.evaluate(() => {
    window.ALARM_VOL = 0.2;
    localStorage.setItem('tbm_alarm_vol', '0.2');
  });
  const vol2 = await page.evaluate(() => {
    const ctx = window.audioCtx();
    let m = null, first = true;
    const oGain = ctx.createGain.bind(ctx);
    ctx.createGain = function () { const g = oGain(); if (first) { m = g; first = false; } return g; };
    window.beep();
    ctx.createGain = oGain;
    return m ? m.gain.value : null;
  });
  chk('ses seviyesi ayari beep\'e uygulaniyor', Math.abs(vol2 - 0.2) < 1e-6, vol2);
  chk('sifir seviyede hic ses uretilmiyor', await page.evaluate(() => {
    window.ALARM_VOL = 0;
    const ctx = window.audioCtx();
    let n = 0;
    const oOsc = ctx.createOscillator.bind(ctx);
    ctx.createOscillator = function () { n++; return oOsc(); };
    window.beep();
    ctx.createOscillator = oOsc;
    window.ALARM_VOL = 0.55;
    return n === 0;
  }));
  chk('MUTED iken hic ses uretilmiyor', await page.evaluate(() => {
    window.MUTED = true;
    const ctx = window.audioCtx();
    let n = 0;
    const oOsc = ctx.createOscillator.bind(ctx);
    ctx.createOscillator = function () { n++; return oOsc(); };
    window.beep();
    ctx.createOscillator = oOsc;
    window.MUTED = false;
    return n === 0;
  }));

  /* =========================================== 13) PNG = TUM GRAFIK PENCERESI */
  console.log('\n=== 13) PNG: grafigin TUM penceresi (baslik, seriler, ayarlar) ===');
  await page.evaluate(() => { window.addSeries(0, 'MOCK_TEMP_T3'); });
  await sleep(800);

  const geo = await page.evaluate(() => {
    const card = document.getElementById('card0');
    const cv = window.CHARTS[0].u.canvas || window.CHARTS[0].u.ctx.canvas;
    const cr = card.getBoundingClientRect(), vr = cv.getBoundingClientRect();
    return { cardW: Math.round(cr.width), cardH: Math.round(cr.height),
             plotW: Math.round(vr.width), plotH: Math.round(vr.height) };
  });

  const dlPng2 = page.waitForEvent('download', { timeout: 15000 });
  await page.click('button[onclick="window.exportPng(0)"]');
  const png2 = await dlPng2;
  const buf2 = fs.readFileSync(await png2.path());
  chk('PNG imzasi gecerli',
    buf2[0] === 0x89 && buf2[1] === 0x50 && buf2[2] === 0x4E && buf2[3] === 0x47);
  /* PNG IHDR: 16..19 genislik, 20..23 yukseklik */
  const pw = buf2.readUInt32BE(16), ph = buf2.readUInt32BE(20);
  const dpr = await page.evaluate(() => window.devicePixelRatio || 1);
  chk('PNG genisligi KARTIN genisligi (cizim alani degil)',
    Math.abs(pw / dpr - geo.cardW) <= 3, { png: pw / dpr, kart: geo.cardW, cizim: geo.plotW });
  chk('PNG yuksekligi cizim alanindan BUYUK (baslik+seriler dahil)',
    ph / dpr > geo.plotH + 40, { png: ph / dpr, cizim: geo.plotH, kart: geo.cardH });
  chk('PNG yuksekligi kart + alt bilgi seridi kadar',
    Math.abs(ph / dpr - (geo.cardH + 20)) <= 4, { png: ph / dpr, beklenen: geo.cardH + 20 });
  chk('PNG dolu bir goruntu (>20KB)', buf2.length > 20000, buf2.length);
  fs.copyFileSync(await png2.path(), __dirname + '/out/export_card.png');

  /* tam ekranda da tum pencere alinmali */
  await page.evaluate(() => window.toggleFull(0));
  await sleep(900);
  const geoFull = await page.evaluate(() => {
    const r = document.getElementById('card0').getBoundingClientRect();
    return { w: Math.round(r.width), h: Math.round(r.height) };
  });
  const dlPng3 = page.waitForEvent('download', { timeout: 15000 });
  await page.click('button[onclick="window.exportPng(0)"]');
  const png3 = await dlPng3;
  const buf3 = fs.readFileSync(await png3.path());
  const pw3 = buf3.readUInt32BE(16);
  chk('tam ekranda PNG tum grafik ekranini kapsiyor',
    Math.abs(pw3 / dpr - geoFull.w) <= 3 && geoFull.w > geo.cardW * 1.5,
    { png: pw3 / dpr, tamEkran: geoFull.w, normal: geo.cardW });
  fs.copyFileSync(await png3.path(), __dirname + '/out/export_full.png');
  await page.evaluate(() => window.toggleFull(0));
  await sleep(500);

  console.log('\n=== 14) JS hatasi yok ===');
  const real = jsErrors.filter(e => !/favicon|WebSocket|ws:/i.test(e));
  chk('konsolda JS hatasi yok', real.length === 0, real.slice(0, 4));

  await browser.close();

  console.log('\n' + '='.repeat(62));
  console.log('ARAYUZ:  ' + PASS + ' gecti,  ' + FAIL.length + ' kaldi');
  FAIL.forEach(f => console.log('   KALAN: ' + f));
  console.log('='.repeat(62));
  process.exit(FAIL.length ? 1 : 0);
})().catch(e => { console.error('TEST COKTU:', e); process.exit(2); });
