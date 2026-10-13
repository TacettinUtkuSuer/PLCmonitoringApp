/* Gorsel dogrulama: kaynak rozeti + ma() karti gercekten ciziliyor mu */
const { chromium } = require('playwright');
const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1600, height: 950 } });
  const errs = [];
  p.on('pageerror', e => errs.push(String(e)));
  await p.goto('http://127.0.0.1:8989', { waitUntil: 'domcontentloaded' });
  await sleep(900);

  await p.evaluate(async () => {
    await fetch('/api/sensors/clear', { method: 'POST' });
    await fetch('/api/webhook/sensor?influx_name=P1&name=Pompa Basinci&unit=Bar&min=0&max=250');
    await fetch('/api/webhook/sensor?influx_name=T3&name=Yag Sicakligi&unit=C&db=hat2');
    await fetch('/api/webhook/sensor?influx_name=N5&name=Devir&unit=rpm&measurement=press&influx_url=http://10.0.0.9:8086');
    const j = await fetch('/api/webhook/sensor?influx_name=P1_MA&name=Basinc MA5&unit=Bar&expr=ma(P1,5)')
      .then(r => r.json());
    window.applyPool(j.sensors);
  });
  await sleep(700);

  const info = await p.evaluate(() => {
    const cards = Array.from(document.querySelectorAll('#poolList .scard'));
    return {
      badges: cards.map(c => ({
        sid: c.getAttribute('data-sid'),
        src: !!c.querySelector('.srctag'),
        tip: (c.querySelector('.srctag') || {}).title || '',
        fx: !!c.querySelector('.fxtag')
      })),
      maStatus: window.fxStatus('P1_MA')
    };
  });
  console.log(JSON.stringify(info, null, 1));
  console.log('JS hatasi:', errs.length ? errs : 'yok');

  await p.locator('#poolList').screenshot({ path: 'src_badges.png' });
  await b.close();
})();
