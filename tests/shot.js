const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome' });
  const page = await b.newPage({ viewport: { width: 1920, height: 1080 } });
  await page.goto('http://127.0.0.1:8989/', { waitUntil: 'networkidle' });
  await page.evaluate(() => fetch('/api/sensors/clear', {method:'POST'}).then(r=>r.json()).then(j=>window.applyPool(j.sensors)));
  await page.evaluate(() => fetch('/api/mock/trigger', { method: 'POST' }).then(r => r.json()).then(j => window.applyPool(j.sensors)));
  await page.waitForTimeout(600);
  const s = await page.evaluate(() => window.POOL.map(x => x.sensor_id));
  const plan = [[0,[s[0],s[1]]],[1,[s[2],s[3]]],[2,[s[4]]],[3,[s[0],s[2]]]];
  for (const [ci,l] of plan) for (const sid of l) { await page.locator(`.scard[data-sid="${sid}"]`).dragTo(page.locator('#card'+ci)); await page.waitForTimeout(200); }
  await page.evaluate(() => { [0,1,3].forEach(i => { const x=window.CHARTS[i].series[1]; if (x) window.toggleAxis(i, x.sensor_id); }); });
  await page.selectOption('#rng0','10h'); await page.waitForTimeout(1500);
  await page.locator('#card0 .ygrp').first().locator('.btn:has-text("FIT")').click();
  await page.locator('#card0 .ygrp').nth(1).locator('.btn:has-text("FIT")').click();
  await page.locator('#fm0').click();
  await page.waitForTimeout(1800);
  await page.screenshot({ path: 'final_grid.png' });
  await page.locator('#fs0').click(); await page.waitForTimeout(2000);
  await page.screenshot({ path: 'final_full.png' });
  console.log('tam ekran GROUP BY =', (await page.locator('#ivl0').innerText()).trim());
  await b.close();
})();
