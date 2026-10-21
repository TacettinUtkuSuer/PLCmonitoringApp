/* ==========================================================================================
   HESAP SENSORU (FORMUL) TESTLERI
   A) Formul motoru birim testleri  (tarayici icinde, deterministik)
   B) "+ New" kart akisi ve grafige tasima  (gercek uc noktalarla)
   ========================================================================================== */
const { chromium } = require('playwright');
const sleep = ms => new Promise(r => setTimeout(r, ms));

const PASS = [], FAIL = [];
function chk(name, cond, extra) {
  (cond ? PASS : FAIL).push(name);
  console.log((cond ? '  OK   ' : '  FAIL ') + name +
              ((!cond && extra !== undefined) ? ('  -> ' + JSON.stringify(extra)) : ''));
}
const near = (a, b, eps) => a != null && b != null && Math.abs(a - b) < (eps || 1e-9);

(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1600, height: 1000 } });
  p.on('pageerror', e => console.log('  !! PAGE ERROR: ' + e.message));
  await p.goto('http://127.0.0.1:8989', { waitUntil: 'domcontentloaded' });
  await sleep(900);

  const clearPool = async () => {
    await p.evaluate(async () => {
      await fetch('/api/sensors/clear', { method: 'POST' });
      const j = await fetch('/api/sensors').then(r => r.json());
      window.PPOOL = [];
      window.DRAFT = null;
      window.CHARTS.forEach(C => { C.series = []; });
      window.applyPool(j.sensors || []);
      window.renderPool(true);
    });
    await sleep(120);
  };

  /* --- kisa yardimci: formulu verilen degerlerle calistir --------------------- */
  const run = (src, vals) => p.evaluate(([s, v]) => {
    const c = window.fxCompile(s);
    if (c.err) return { err: c.err };
    return { v: window.fxRun(c.ast, id => (id in v ? v[id] : undefined)), deps: c.deps };
  }, [src, vals || {}]);

  /* ======================================================================================
     A) FORMUL MOTORU
     ====================================================================================== */
  console.log('\n=== A) Formul motoru: aritmetik ve oncelik ===');
  chk('2+3*4 = 14 (carpma once)',            (await run('2+3*4')).v === 14);
  chk('(2+3)*4 = 20 (parantez)',             (await run('(2+3)*4')).v === 20);
  chk('10-4-3 = 3 (sola baglanir)',          (await run('10-4-3')).v === 3);
  chk('100/10/2 = 5 (sola baglanir)',        (await run('100/10/2')).v === 5);
  chk('2^3^2 = 512 (saga baglanir)',         (await run('2^3^2')).v === 512);
  chk('2**10 = 1024 (** ustel)',             (await run('2**10')).v === 1024);
  chk('-3+5 = 2 (tekli eksi)',               (await run('-3+5')).v === 2);
  chk('-2^2 = -4 (us once)',                 (await run('-2^2')).v === -4);
  chk('7%3 = 1 (mod)',                       (await run('7%3')).v === 1);
  chk('1.5e2 = 150 (bilimsel gosterim)',     (await run('1.5e2')).v === 150);
  chk('.25*4 = 1 (basta nokta)',             (await run('.25*4')).v === 1);

  console.log('\n=== A) Formul motoru: fonksiyonlar ===');
  chk('sqrt(16) = 4',                        (await run('sqrt(16)')).v === 4);
  chk('abs(-7) = 7',                         (await run('abs(-7)')).v === 7);
  chk('min(3,7,2) = 2',                      (await run('min(3,7,2)')).v === 2);
  chk('max(3,7,2) = 7',                      (await run('max(3,7,2)')).v === 7);
  chk('avg(2,4,6) = 4',                      (await run('avg(2,4,6)')).v === 4);
  chk('sum(1,2,3,4) = 10',                   (await run('sum(1,2,3,4)')).v === 10);
  chk('round(3.14159, 2) = 3.14',            (await run('round(3.14159,2)')).v === 3.14);
  chk('floor(2.9) = 2',                      (await run('floor(2.9)')).v === 2);
  chk('ceil(2.1) = 3',                       (await run('ceil(2.1)')).v === 3);
  chk('clamp(150, 0, 100) = 100',            (await run('clamp(150,0,100)')).v === 100);
  chk('hypot(3,4) = 5',                      (await run('hypot(3,4)')).v === 5);
  chk('pow(2,8) = 256',                      (await run('pow(2,8)')).v === 256);
  chk('log(1000) = 3 (10 tabani)',           near((await run('log(1000)')).v, 3));
  chk('log(8,2) = 3 (secilen taban)',        near((await run('log(8,2)')).v, 3));
  chk('deg(rad(90)) = 90 (ic ice)',          near((await run('deg(rad(90))')).v, 90));
  chk('pi sabiti',                           near((await run('pi')).v, Math.PI));
  chk('e sabiti',                            near((await run('e')).v, Math.E));

  console.log('\n=== A) Formul motoru: sensor referanslari ===');
  let r = await run('A*2 + B', { A: 10, B: 5 });
  chk('A*2+B = 25',                          r.v === 25, r);
  chk('bagimliliklar dogru cikarildi',       JSON.stringify(r.deps) === '["A","B"]', r.deps);
  r = await run('sqrt(N5) + avg(A,B)', { N5: 9, A: 2, B: 4 });
  chk('sqrt(N5)+avg(A,B) = 6',               r.v === 6, r);
  r = await run('A-B', { A: 10, B: 4, 'A-B': 999 });
  chk('A-B cikarma olarak okunur (6)',       r.v === 6, r);
  r = await run('[A-B]*2', { 'A-B': 7 });
  chk('[A-B] koseli parantezle sensordur',   r.v === 14, r);
  chk('[A-B] bagimliligi dogru',             JSON.stringify(r.deps) === '["A-B"]', r.deps);
  r = await run('YAG.SIC * 2', { 'YAG.SIC': 21 });
  chk('noktali ad calisir',                  r.v === 42, r);
  r = await run('pi * R', { R: 2, pi: 100 });
  chk('havuzdaki sensor sabiti golgeler',    r.v === 200, r);

  console.log('\n=== A) Formul motoru: BOS deger ve tanimsizlik yayilimi ===');
  chk('bagimlilik null -> sonuc null',       (await run('A+1', { A: null })).v === null);
  chk('bagimlilik yok -> sonuc null',        (await run('YOK+1', {})).v === null);
  chk('0 a bolme -> null',                   (await run('A/0', { A: 5 })).v === null);
  chk('0 ile mod -> null',                   (await run('A%0', { A: 5 })).v === null);
  chk('sqrt(-1) -> null (NaN degil)',        (await run('sqrt(0-1)')).v === null);
  chk('ln(0) -> null (sonsuz degil)',        (await run('ln(0)')).v === null);
  chk('coalesce bos degeri atlar',           (await run('coalesce(A,B,7)', { A: null })).v === 7);

  console.log('\n=== A) Formul motoru: karsilastirma ve if() ===');
  chk('A>B -> 1',                            (await run('A>B', { A: 5, B: 3 })).v === 1);
  chk('A>B -> 0',                            (await run('A>B', { A: 1, B: 3 })).v === 0);
  chk('A>=B -> 1 (esit)',                    (await run('A>=B', { A: 3, B: 3 })).v === 1);
  chk('A!=B -> 1',                           (await run('A!=B', { A: 3, B: 4 })).v === 1);
  chk('if(A>100, 1, 0) -> 1',                (await run('if(A>100,1,0)', { A: 150 })).v === 1);
  chk('if() secilmeyen dal bos olsa da calisir',
                                             (await run('if(A>0, 42, B)', { A: 1, B: null })).v === 42);

  console.log('\n=== A) Formul motoru: hata yakalama ===');
  chk('"2+" yarim formul reddedilir',        !!(await run('2+')).err);
  chk('"(2+3" kapanmamis parantez',          !!(await run('(2+3')).err);
  chk('"2 $ 3" taninmayan karakter',         !!(await run('2 $ 3')).err);
  chk('bilinmeyen fonksiyon reddedilir',     /Bilinmeyen fonksiyon/.test((await run('foo(1)')).err || ''));
  chk('yanlis arguman sayisi reddedilir',    /arguman/.test((await run('sqrt(1,2)')).err || ''));
  chk('kapanmamis koseli parantez',          !!(await run('[ABC * 2')).err);
  chk('bos formul reddedilir',               !!(await run('')).err);
  chk('gecerli formulde err yok',            (await run('A+1', { A: 1 })).err === undefined);

  /* ======================================================================================
     B) KART AKISI
     ====================================================================================== */
  console.log('\n=== B) "+ New" taslak karti ===');
  await clearPool();
  chk('baslangicta taslak yok',              (await p.locator('#poolList .scard.draft').count()) === 0);

  await p.click('#newBtn');
  await sleep(200);
  chk('New taslak karti acar',               (await p.locator('#poolList .scard.draft').count()) === 1);
  chk('taslakta ad kutusu var',              (await p.locator('#poolList .scard.draft .nmi').count()) === 1);
  chk('taslakta formul kutusu var',          (await p.locator('#poolList .scard.draft .fxi').count()) === 1);
  chk('ad kutusu odakta',
      await p.evaluate(() => document.activeElement && document.activeElement.classList.contains('nmi')));
  chk('sunucuya henuz yazilmadi',
      (await p.evaluate(() => fetch('/api/sensors').then(r => r.json()).then(j => j.sensors.length))) === 0);

  await p.locator('#poolList .scard.draft .fxi').fill('=1+');
  await sleep(150);
  chk('gecersiz formul aninda kirmizi',
      await p.locator('#poolList .scard.draft .fxv').evaluate(e => e.classList.contains('bad')));

  await p.keyboard.press('Escape');
  await sleep(200);
  chk('Escape taslagi iptal eder',           (await p.locator('#poolList .scard.draft').count()) === 0);

  console.log('\n=== B) Duz kart (webhooksuz sensor) ===');
  await p.click('#newBtn');
  await p.locator('#poolList .scard.draft .nmi').fill('Yag Sicakligi');
  await p.locator('#poolList .scard.draft .uni').fill('C');
  await p.locator('#poolList .scard.draft .fxi').fill('asd_65');
  await sleep(120);
  chk('duz ad gecerli diye isaretlenir',
      await p.locator('#poolList .scard.draft .fxv').evaluate(e => e.classList.contains('ok')));
  await p.locator('#poolList .scard.draft .fxi').press('Enter');
  await sleep(500);

  let pool = await p.evaluate(() => fetch('/api/sensors').then(r => r.json()).then(j => j.sensors));
  chk('duz kart sunucuya yazildi',           pool.length === 1 && pool[0].sensor_id === 'asd_65', pool);
  chk('kimlik = yazilan influx adi',         pool[0].sensor_id === 'asd_65');
  chk('ad kaydedildi',                       pool[0].name === 'Yag Sicakligi', pool[0]);
  chk('birim kaydedildi',                    pool[0].unit === 'C');
  chk('duz kartta formul yok',               !pool[0].expr);
  chk('manual isaretlendi (fx satiri gorunur)', pool[0].manual === true);
  chk('kart havuzda gorunuyor',              (await p.locator('[data-sid="asd_65"]').count()) === 1);
  chk('kartta fx satiri var',                (await p.locator('[data-sid="asd_65"] .fxi').count()) === 1);
  chk('fx satirinda influx adi yazili',
      (await p.locator('[data-sid="asd_65"] .fxi').inputValue()) === 'asd_65');
  chk('duz kart hesap karti degil',
      (await p.locator('[data-sid="asd_65"]').evaluate(e => e.classList.contains('calc'))) === false);

  console.log('\n=== B) Hesap karti (= ile formul) ===');
  await clearPool();
  await p.evaluate(() => fetch('/api/webhook/sensors?s=MOCK_AA|A Sensoru|Bar,MOCK_BB|B Sensoru|Bar')
    .then(r => r.json()).then(j => window.applyPool(j.sensors)));
  await sleep(300);

  await p.click('#newBtn');
  await p.locator('#poolList .scard.draft .nmi').fill('Toplam Guc');
  await p.locator('#poolList .scard.draft .uni').fill('kW');
  await p.locator('#poolList .scard.draft .fxi').fill('=MOCK_AA + MOCK_BB');
  await sleep(150);
  const hint = await p.locator('#poolList .scard.draft .fxv').textContent();
  chk('formul onizlemesi bagimliliklari listeler',
      /MOCK_AA/.test(hint) && /MOCK_BB/.test(hint), hint);
  await p.locator('#poolList .scard.draft .fxi').press('Enter');
  await sleep(600);

  pool = await p.evaluate(() => fetch('/api/sensors').then(r => r.json()).then(j => j.sensors));
  const calc = pool.filter(s => s.expr)[0];
  chk('hesap karti olusturuldu',             !!calc, pool.map(s => s.sensor_id));
  chk('formul "=" olmadan saklandi',         calc && calc.expr === 'MOCK_AA + MOCK_BB', calc);
  chk('kimlik CALC_ ile uretildi',           calc && /^CALC_/.test(calc.sensor_id), calc && calc.sensor_id);
  chk('kimlik addan turetildi',              calc && calc.sensor_id === 'CALC_TOPLAM_GUC', calc && calc.sensor_id);
  chk('ad korundu',                          calc && calc.name === 'Toplam Guc');
  chk('birim korundu',                       calc && calc.unit === 'kW');

  const cid = calc.sensor_id;
  chk('kart calc sinifi aldi',
      await p.locator('[data-sid="' + cid + '"]').evaluate(e => e.classList.contains('calc')));
  chk('kartta fx rozeti var',                (await p.locator('[data-sid="' + cid + '"] .fxtag').count()) === 1);
  chk('fx kutusunda = ile formul gorunur',
      (await p.locator('[data-sid="' + cid + '"] .fxi').inputValue()) === '=MOCK_AA + MOCK_BB');
  chk('kart suruklenebilir',
      await p.locator('[data-sid="' + cid + '"]').evaluate(e => e.draggable === true));

  console.log('\n=== B) Hesap sensoru grafikte: sayisal dogruluk ===');
  await p.evaluate((id) => {
    window.addSeries(0, 'MOCK_AA');
    window.addSeries(0, 'MOCK_BB');
    window.addSeries(0, id);
  }, cid);
  await p.evaluate(() => window.fetchChart(window.CHARTS[0], true));
  await sleep(1500);

  const chartInfo = await p.evaluate(() => {
    const C = window.CHARTS[0];
    return { n: C.series.length, ids: C.series.map(s => s.sensor_id), len: C.u ? C.u.data[0].length : 0 };
  });
  chk('grafikte 3 seri var',                 chartInfo.n === 3, chartInfo);
  chk('grafikte veri noktalari var',         chartInfo.len > 5, chartInfo);

  const sumCheck = await p.evaluate(() => {
    const d = window.CHARTS[0].u.data;
    let checked = 0, bad = 0, sample = null;
    for (let i = 0; i < d[0].length; i++) {
      const a = d[1][i], bv = d[2][i], c = d[3][i];
      if (a == null || bv == null) continue;
      checked++;
      if (c == null || Math.abs(c - (a + bv)) > 1e-9) {
        bad++; if (!sample) sample = { i, a, b: bv, c, want: a + bv };
      }
    }
    return { checked, bad, sample };
  });
  chk('her noktada CALC = A + B',            sumCheck.checked > 5 && sumCheck.bad === 0, sumCheck);

  const netCheck = await p.evaluate(async (id) => {
    const seen = [];
    const orig = window.fetch;
    window.fetch = function (u) { if (String(u).indexOf('/api/data') === 0) seen.push(String(u)); return orig.apply(this, arguments); };
    await window.fetchChart(window.CHARTS[0], true);
    window.fetch = orig;
    return seen;
  }, cid);
  chk('sunucuya CALC id ISTENMEZ (yalniz bagimliliklar)',
      netCheck.length === 1 && netCheck[0].indexOf(cid) < 0 &&
      netCheck[0].indexOf('MOCK_AA') > 0 && netCheck[0].indexOf('MOCK_BB') > 0, netCheck);

  console.log('\n=== B) Formul duzenleme ===');
  await p.locator('[data-sid="' + cid + '"] .fxi').fill('=MOCK_AA * 3');
  await p.locator('[data-sid="' + cid + '"] .fxi').press('Enter');
  await sleep(700);
  pool = await p.evaluate(() => fetch('/api/sensors').then(r => r.json()).then(j => j.sensors));
  const after = pool.filter(s => s.sensor_id === cid)[0];
  chk('formul guncellendi',                  after && after.expr === 'MOCK_AA * 3', after);
  chk('formul degisince KIMLIK sabit kalir', !!after);
  chk('grafik baglantisi kopmadi',
      (await p.evaluate(() => window.CHARTS[0].series.map(s => s.sensor_id))).indexOf(cid) >= 0);

  await p.evaluate(() => window.fetchChart(window.CHARTS[0], true));
  await sleep(1200);
  const mulCheck = await p.evaluate(() => {
    const d = window.CHARTS[0].u.data;
    let checked = 0, bad = 0;
    for (let i = 0; i < d[0].length; i++) {
      if (d[1][i] == null) continue;
      checked++;
      if (d[3][i] == null || Math.abs(d[3][i] - d[1][i] * 3) > 1e-9) bad++;
    }
    return { checked, bad };
  });
  chk('yeni formul grafige yansidi (A*3)',   mulCheck.checked > 5 && mulCheck.bad === 0, mulCheck);

  console.log('\n=== B) Ad degistirme ===');
  const posBefore = pool.findIndex(s => s.sensor_id === cid);
  await p.locator('[data-sid="' + cid + '"] .nmi').fill('Yeni Ad');
  await p.locator('[data-sid="' + cid + '"] .nmi').press('Enter');
  await sleep(600);
  pool = await p.evaluate(() => fetch('/api/sensors').then(r => r.json()).then(j => j.sensors));
  const ren = pool.filter(s => s.sensor_id === 'CALC_YENI_AD')[0];
  chk('ad degisince degisken adi da degisti (CALC_YENI_AD)', !!ren, pool.map(s => s.sensor_id));
  chk('ad degisti',                          ren && ren.name === 'Yeni Ad');
  chk('ad degisince formul korundu',         ren && ren.expr === 'MOCK_AA * 3');
  chk('eski degisken adi havuzdan kalkti',   !pool.some(s => s.sensor_id === cid));
  chk('kart yerinde kaldi (sira korundu)',   pool.findIndex(s => s.sensor_id === 'CALC_YENI_AD') === posBefore);
  chk('grafik serisi yeni ada tasindi',
      (await p.evaluate(() => window.CHARTS[0].series.map(s => s.sensor_id))).indexOf('CALC_YENI_AD') >= 0);

  /* eski ada geri don: asagidaki testler CALC_TOPLAM_GUC kimligini kullanir */
  await p.locator('[data-sid="CALC_YENI_AD"] .nmi').fill('Toplam Guc');
  await p.locator('[data-sid="CALC_YENI_AD"] .nmi').press('Enter');
  await sleep(600);
  chk('geri adlandirinca kimlik geri geldi', (await p.locator('[data-sid="' + cid + '"]').count()) === 1);

  console.log('\n=== B) Ic ice hesap sensoru ===');
  await p.click('#newBtn');
  await p.locator('#poolList .scard.draft .nmi').fill('Ikinci Kat');
  await p.locator('#poolList .scard.draft .fxi').fill('=' + cid + ' + 100');
  await p.locator('#poolList .scard.draft .fxi').press('Enter');
  await sleep(700);
  const cid2 = 'CALC_IKINCI_KAT';
  chk('hesap sensoru baska hesabi kullanabilir',
      (await p.locator('[data-sid="' + cid2 + '"]').count()) === 1);

  console.log('\n=== B) Ad degisince onu kullanan formuller guncellenir ===');
  await p.locator('[data-sid="' + cid + '"] .nmi').fill('Toplam Guc Iki');
  await p.locator('[data-sid="' + cid + '"] .nmi').press('Enter');
  await sleep(700);
  pool = await p.evaluate(() => fetch('/api/sensors').then(r => r.json()).then(j => j.sensors));
  chk('baska karttaki formul yeni adi kullaniyor',
      (pool.filter(s => s.sensor_id === cid2)[0] || {}).expr === 'CALC_TOPLAM_GUC_IKI + 100',
      pool.filter(s => s.sensor_id === cid2)[0]);
  await p.locator('[data-sid="CALC_TOPLAM_GUC_IKI"] .nmi').fill('Toplam Guc');
  await p.locator('[data-sid="CALC_TOPLAM_GUC_IKI"] .nmi').press('Enter');
  await sleep(700);
  pool = await p.evaluate(() => fetch('/api/sensors').then(r => r.json()).then(j => j.sensors));
  chk('geri adlandirinca formul de geri dondu',
      (pool.filter(s => s.sensor_id === cid2)[0] || {}).expr === 'CALC_TOPLAM_GUC + 100');

  await p.evaluate((id2) => { window.addSeries(1, id2); }, cid2);
  await p.evaluate(() => window.fetchChart(window.CHARTS[1], true));
  await sleep(1200);
  const nested = await p.evaluate(() => {
    const d = window.CHARTS[1].u.data;
    let ok = 0;
    for (let i = 0; i < d[0].length; i++) if (d[1][i] != null && isFinite(d[1][i])) ok++;
    return { pts: d[0].length, ok };
  });
  chk('ic ice hesap grafikte deger uretti',  nested.ok > 5, nested);

  console.log('\n=== B) Dongu ve hata reddi ===');
  const cyc = await p.evaluate((id2) => {
    const c = window.fxCompile(id2 + ' + 1');
    return window.fxWouldCycle(id2, c.deps);
  }, cid2);
  chk('kendine referans dongu olarak gorulur', cyc === true);

  const plan = await p.evaluate((id2) => window.fxPlan([id2]), cid2);
  chk('fxPlan yalnizca yaprak sensorleri ister',
      plan.fetch.indexOf('MOCK_AA') >= 0 && plan.fetch.every(x => !/^CALC_/.test(x)), plan);
  chk('fxPlan topolojik sira verir',
      plan.order.indexOf('CALC_IKINCI_KAT') > plan.order.indexOf('CALC_TOPLAM_GUC'), plan.order);

  await p.evaluate((id) => { window.__err = null; window.showError = m => { window.__err = m; }; }, cid);
  await p.locator('[data-sid="' + cid2 + '"] .fxi').fill('=' + cid2 + ' * 2');
  await p.locator('[data-sid="' + cid2 + '"] .fxi').press('Enter');
  await sleep(500);
  chk('dongusel formul reddedilir',
      /[Dd]ongusel/.test(await p.evaluate(() => window.__err || '')),
      await p.evaluate(() => window.__err));
  pool = await p.evaluate(() => fetch('/api/sensors').then(r => r.json()).then(j => j.sensors));
  chk('dongu sunucuya YAZILMADI',
      pool.filter(s => s.sensor_id === cid2)[0].expr === 'CALC_TOPLAM_GUC + 100',
      pool.filter(s => s.sensor_id === cid2)[0]);

  await p.evaluate(() => { window.__err = null; });
  await p.locator('[data-sid="' + cid2 + '"] .fxi').fill('=MOCK_AA * * 2');
  await p.locator('[data-sid="' + cid2 + '"] .fxi').press('Enter');
  await sleep(400);
  chk('sozdizimi hatasi reddedilir',
      /[Ff]ormul hatasi/.test(await p.evaluate(() => window.__err || '')),
      await p.evaluate(() => window.__err));

  console.log('\n=== B) Alarm esigi hesap sensorunde ===');
  await p.evaluate((id) => fetch('/api/sensors/' + id + '/limits?min=-99999&max=-99998')
    .then(r => r.json()).then(j => window.applyPool(j.sensors)), cid);
  await sleep(200);
  await p.evaluate(() => window.alarmWatch());
  await sleep(700);
  chk('hesap sensoru alarm verebilir',
      await p.evaluate((id) => !!window.ALARM[id], cid),
      await p.evaluate(() => Object.keys(window.ALARM)));
  chk('hesap sensorunun son degeri hesaplandi',
      await p.evaluate((id) => window.LASTVAL[id] != null && isFinite(window.LASTVAL[id]), cid),
      await p.evaluate((id) => window.LASTVAL[id], cid));
  await p.evaluate((id) => fetch('/api/sensors/' + id + '/limits?min=&max=')
    .then(r => r.json()).then(j => window.applyPool(j.sensors)), cid);
  await sleep(200);

  console.log('\n=== B) Preset ile tasima ===');
  const preset = await p.evaluate(() => {
    const body = {
      v: 3,
      pool: window.allSensors().map(s => ({
        influx_name: s.sensor_id, name: s.name || '', unit: s.unit || '',
        min: null, max: null, expr: s.expr || '', manual: !!s.manual
      })),
      colors: {}, charts: []
    };
    return body.pool;
  });
  chk('preset govdesi formulu tasir',
      preset.filter(x => x.influx_name === cid)[0].expr === 'MOCK_AA * 3', preset);

  await p.evaluate((pl) => window.setPresetPool(pl), preset.filter(x => /^CALC_/.test(x.influx_name)));
  await sleep(300);
  chk('preset havuzuna yuklenen kart hesap karti kalir',
      await p.evaluate((id) => window.PPOOL.some(s => s.sensor_id === id && !!s.expr), cid));

  console.log('\n=== B) Kalicilik ve geri donusler ===');
  await p.reload({ waitUntil: 'domcontentloaded' });
  await sleep(1200);
  chk('sayfa yenilenince hesap karti duruyor',
      (await p.locator('[data-sid="' + cid + '"]').count()) === 1);
  chk('yenilemeden sonra formul kutusu dolu',
      (await p.locator('[data-sid="' + cid + '"] .fxi').inputValue()) === '=MOCK_AA * 3');

  await p.evaluate(() => { window.__err = null; window.showError = m => { window.__err = m; }; });
  await p.locator('[data-sid="' + cid + '"] .fxi').fill('MOCK_BB');
  await p.locator('[data-sid="' + cid + '"] .fxi').press('Enter');
  await sleep(700);
  chk('hesap karti duz ada cevrilince cakisma yakalanir',
      /zaten havuzda/.test(await p.evaluate(() => window.__err || '')),
      await p.evaluate(() => window.__err));

  await p.locator('[data-sid="' + cid + '"] .fxi').fill('YENI_ALAN_9');
  await p.locator('[data-sid="' + cid + '"] .fxi').press('Enter');
  await sleep(900);
  pool = await p.evaluate(() => fetch('/api/sensors').then(r => r.json()).then(j => j.sensors));
  chk('hesap karti duz sensore cevrilebilir',
      pool.some(s => s.sensor_id === 'YENI_ALAN_9' && !s.expr), pool.map(s => s.sensor_id));
  chk('eski CALC kimligi havuzdan kalkti',   !pool.some(s => s.sensor_id === cid));

  console.log('\n=== B) Havuz kartlari bozulmadi (gerileme) ===');
  chk('webhook kartlari hala duz gosterim',
      (await p.locator('[data-sid="MOCK_AA"] .nm').count()) === 1);
  chk('webhook kartinda fx satiri YOK',
      (await p.locator('[data-sid="MOCK_AA"] .fxi').count()) === 0);
  chk('webhook kartinda alarm satiri var',
      (await p.locator('[data-sid="MOCK_AA"] .lo').count()) === 1);
  chk('Clear All butonu duruyor',
      (await p.locator('#poolHead button:has-text("Clear All")').count()) === 1);

  await p.evaluate(() => {
    document.querySelector('[data-sid="MOCK_AA"] .fxi, [data-sid="MOCK_AA"] .nmi');
  });
  await p.click('#newBtn');
  await sleep(150);
  await p.click('#newBtn');
  await sleep(150);
  chk('New iki kez basilinca tek taslak kalir',
      (await p.locator('#poolList .scard.draft').count()) === 1);
  await p.keyboard.press('Escape');
  await sleep(150);

  console.log('\n' + '='.repeat(64));
  console.log('FORMUL TESTLERI:  ' + PASS.length + ' gecti,  ' + FAIL.length + ' kaldi');
  if (FAIL.length) FAIL.forEach(f => console.log('   KALAN: ' + f));
  console.log('='.repeat(64));
  await b.close();
  process.exit(FAIL.length ? 1 : 0);
})();
