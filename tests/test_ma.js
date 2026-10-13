/* ma() -- HAREKETLI ORTALAMA testi.
   1) grafik yolu (fxFillSeries)  2) canli/alarm yolu (fxFillValues + FXSNAP)
   3) derleme hatalari            4) gerileme: diger fonksiyonlar bozulmadi mi */
const { chromium } = require('playwright');
const sleep = ms => new Promise(r => setTimeout(r, ms));
const PASS = [], FAIL = [];
function chk(n, c, x) {
  (c ? PASS : FAIL).push(n);
  console.log((c ? '  OK   ' : '  FAIL ') + n + ((!c && x !== undefined) ? '  -> ' + JSON.stringify(x) : ''));
}
const near = (a, b) => a != null && b != null && Math.abs(a - b) < 1e-9;

(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1400, height: 900 } });
  const errs = [];
  p.on('pageerror', e => errs.push(String(e)));
  await p.goto('http://127.0.0.1:8989', { waitUntil: 'domcontentloaded' });
  await sleep(800);

  /* ---------------------------------------------------------------- 0) GERILEME
     Onceki denemede fxRun icinde `var window = ...` global window'u golgeliyordu;
     bu, fonksiyon ve sabit iceren HER formulu bozuyordu. */
  console.log('\n=== 0) Gerileme: diger fonksiyonlar bozulmadi mi ===');
  const base = await p.evaluate(() => {
    const run = (src, vals) => {
      const c = window.fxCompile(src);
      if (c.err) return { err: c.err };
      return { v: window.fxRun(c.ast, n => vals[n]) };
    };
    return {
      sqrt:  run('sqrt(16)', {}),
      avg:   run('avg(2,4,6)', {}),
      pi:    run('pi*2', {}),
      mix:   run('P1*2 + sqrt(T3)', { P1: 10, T3: 9 }),
      iff:   run('if(P1 > 5, 100, 200)', { P1: 10 }),
      coal:  run('coalesce(A, B, 7)', { A: null, B: null })
    };
  });
  chk('sqrt(16) = 4', near(base.sqrt.v, 4), base.sqrt);
  chk('avg(2,4,6) = 4', near(base.avg.v, 4), base.avg);
  chk('pi*2 = 6.283...', near(base.pi.v, Math.PI * 2), base.pi);
  chk('P1*2 + sqrt(T3) = 23', near(base.mix.v, 23), base.mix);
  chk('if(...) = 100', near(base.iff.v, 100), base.iff);
  chk('coalesce(null,null,7) = 7', near(base.coal.v, 7), base.coal);

  /* ---------------------------------------------------------------- 1) DERLEME */
  console.log('\n=== 1) ma() derlemesi ===');
  const comp = await p.evaluate(() => ({
    ok:      window.fxCompile('ma(P1, 5)'),
    expr:    window.fxCompile('ma(P1*2 + T3, 10)'),
    nested:  window.fxCompile('ma(ma(P1, 3), 3)'),
    varWin:  window.fxCompile('ma(P1, T3)'),
    zeroWin: window.fxCompile('ma(P1, 0)'),
    bigWin:  window.fxCompile('ma(P1, 99999)'),
    oneArg:  window.fxCompile('ma(P1)')
  }));
  chk('ma(P1,5) derlendi', !comp.ok.err, comp.ok.err);
  chk('ma(P1,5) bagimliligi P1', JSON.stringify(comp.ok.deps) === '["P1"]', comp.ok.deps);
  chk('ma(ifade,10) derlendi', !comp.expr.err, comp.expr.err);
  chk('ma(ifade,10) bagimliligi P1+T3', JSON.stringify(comp.expr.deps) === '["P1","T3"]', comp.expr.deps);
  chk('ic ice ma() derlendi', !comp.nested.err, comp.nested.err);
  chk('degisken pencere REDDEDILDI', !!comp.varWin.err, comp.varWin);
  chk('sifir pencere REDDEDILDI', !!comp.zeroWin.err, comp.zeroWin);
  chk('asiri buyuk pencere REDDEDILDI', !!comp.bigWin.err, comp.bigWin);
  chk('tek argumanli ma() REDDEDILDI', !!comp.oneArg.err, comp.oneArg);

  /* ---------------------------------------------------------------- 2) GRAFIK YOLU */
  console.log('\n=== 2) Grafik yolu (fxFillSeries) ===');
  const chart = await p.evaluate(() => {
    window.POOL = [
      { sensor_id: 'P1', name: 'Basinc', unit: 'Bar' },
      { sensor_id: 'T3', name: 'Sicaklik', unit: 'C' },
      { sensor_id: 'MA5',  name: 'MA5',  expr: 'ma(P1, 5)' },
      { sensor_id: 'MAX',  name: 'MAX',  expr: 'ma(P1*2 + T3, 3)' },
      { sensor_id: 'MAN',  name: 'MAN',  expr: 'ma(GAP, 4)' },
      { sensor_id: 'MNEST', name: 'MNEST', expr: 'ma(MA5, 2)' }
    ];
    const j = {
      t: [0,1,2,3,4,5,6,7,8,9],
      series: {
        P1:  [10, 20, 30, 40, 50, 60, 70, 80, 90, 100],
        T3:  [ 1,  1,  1,  1,  1,  1,  1,  1,  1,   1],
        /* ortasinda BOSLUK olan seri */
        GAP: [ 4,  null, 8, 12, null, 20, 24, 28, 32, 36]
      }
    };
    window.fxFillSeries(j, ['MA5', 'MAX', 'MAN', 'MNEST']);
    return { ma5: j.series.MA5, max: j.series.MAX, man: j.series.MAN, mnest: j.series.MNEST };
  });

  /* ma(P1,5): index 0..3 bos; index4=(10..50)/5=30; index5=40; ... index9=80 */
  chk('ilk 4 nokta BOS (pencere dolmadi)',
      chart.ma5.slice(0, 4).every(v => v === null), chart.ma5.slice(0, 4));
  chk('index4 = 30', near(chart.ma5[4], 30), chart.ma5[4]);
  chk('index5 = 40', near(chart.ma5[5], 40), chart.ma5[5]);
  chk('index9 = 80', near(chart.ma5[9], 80), chart.ma5[9]);

  /* ma(P1*2 + T3, 3): index2 = ((21)+(41)+(61))/3 = 41 */
  chk('ifade argumani: index2 = 41', near(chart.max[2], 41), chart.max[2]);
  chk('ifade argumani: ilk 2 nokta BOS',
      chart.max[0] === null && chart.max[1] === null, chart.max.slice(0, 2));

  /* ma(GAP,4): index3 = (4 + BOS + 8 + 12)/3 = 8   (bosluk ATLANIR) */
  chk('penceredeki bosluk atlanir: index3 = 8', near(chart.man[3], 8), chart.man[3]);
  /* index4 = (BOS + 8 + 12 + BOS)/2 = 10 */
  chk('iki bosluklu pencere: index4 = 10', near(chart.man[4], 10), chart.man[4]);

  /* ic ice: ma(MA5,2)
     index3 -> pencere [MA5[2], MA5[3]] = [BOS, BOS]          -> BOS
     index4 -> pencere [MA5[3], MA5[4]] = [BOS, 30] -> 30/1   -> 30 (bosluk atlanir)
     index5 -> pencere [MA5[4], MA5[5]] = [30, 40]  -> 35              */
  chk('ic ice ma(): index3 BOS (ikisi de bos)', chart.mnest[3] === null, chart.mnest[3]);
  chk('ic ice ma(): index4 = 30 (tek dolu ornek)', near(chart.mnest[4], 30), chart.mnest[4]);
  chk('ic ice ma(): index5 = 35', near(chart.mnest[5], 35), chart.mnest[5]);

  /* ---------------------------------------------------------------- 3) CANLI YOL */
  console.log('\n=== 3) Canli / alarm yolu (fxFillValues) ===');
  const live = await p.evaluate(() => {
    window.FXSNAP = [];
    window.POOL = [
      { sensor_id: 'P1', name: 'Basinc', unit: 'Bar' },
      { sensor_id: 'LMA', name: 'LMA', expr: 'ma(P1, 3)' }
    ];
    const seen = [];
    [10, 20, 30, 40, 50].forEach(function (v) {
      const values = { P1: v };
      window.fxFillValues(values, ['LMA']);
      seen.push(values.LMA);
    });
    return { seen: seen, snapLen: window.FXSNAP.length };
  });
  /* yoklama 1,2 -> bos;  3 -> (10+20+30)/3=20;  4 -> 30;  5 -> 40 */
  chk('canli: ilk 2 yoklama BOS', live.seen[0] === null && live.seen[1] === null, live.seen);
  chk('canli: 3. yoklama = 20', near(live.seen[2], 20), live.seen);
  chk('canli: 4. yoklama = 30', near(live.seen[3], 30), live.seen);
  chk('canli: 5. yoklama = 40', near(live.seen[4], 40), live.seen);
  chk('canli: gecmis tamponu doldu', live.snapLen === 5, live.snapLen);

  /* tampon ust siniri */
  const capped = await p.evaluate(() => {
    window.FXSNAP = [];
    window.FXSNAP_MAX = 10;
    for (let i = 0; i < 50; i++) window.fxSnapPush({ P1: i });
    const n = window.FXSNAP.length;
    window.FXSNAP_MAX = 1200;
    return n;
  });
  chk('gecmis tamponu ust sinirda kesiliyor', capped === 10, capped);

  /* ---------------------------------------------------------------- 4) ONIZLEME */
  console.log('\n=== 4) Gecmissiz cagri (kart onizlemesi) ===');
  const noctx = await p.evaluate(() => {
    const c = window.fxCompile('ma(P1, 3)');
    return window.fxRun(c.ast, n => ({ P1: 42 })[n]);   /* ctx YOK */
  });
  chk('ctx yoksa ma() cakilmaz, BOS doner', noctx === null, noctx);

  chk('sayfada JS hatasi yok', errs.length === 0, errs);

  console.log('\n' + '='.repeat(62));
  console.log('MA TESTI:  ' + PASS.length + ' gecti,  ' + FAIL.length + ' kaldi');
  if (FAIL.length) FAIL.forEach(f => console.log('   KALAN: ' + f));
  console.log('='.repeat(62));
  await b.close();
  process.exit(FAIL.length ? 1 : 0);
})();
