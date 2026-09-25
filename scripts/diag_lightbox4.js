// Confirm hypothesis: a real click with tiny movement (micro-selection) blocks the lightbox
const { chromium } = require('playwright-core');
const fs = require('fs');

async function clickWithMove(page, x, y, dx, dy){
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x + dx, y + dy, { steps: 3 });
  await page.mouse.up();
}

(async () => {
  const out = {};
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
  await page.goto('http://127.0.0.1:8877/', { waitUntil: 'networkidle' });
  await page.waitForFunction(() => typeof selectDoc === 'function', { timeout: 8000 }).catch(()=>{});
  await page.evaluate(() => { document.getElementById('loginLayer').classList.remove('on'); });
  await page.evaluate(() => { selectDoc('d41d8cd9'); readTab = 'orig'; renderRead(); });
  await page.waitForSelector('#readBody .orig-page img', { timeout: 8000 }).catch(()=>{});
  await page.waitForTimeout(1500);
  await page.evaluate(() => { document.getElementById('loginLayer').classList.remove('on'); });

  const pt = await page.evaluate(() => {
    const pg = document.querySelector('#readBody .orig-page');
    const r = pg.getBoundingClientRect();
    return { x: r.x + r.width/2, y: r.y + r.height/2 };
  });

  // Scenario A: realistic click with 4px movement (simulates human micro-movement over text)
  await page.evaluate(() => document.getElementById('lightbox').classList.remove('on'));
  await clickWithMove(page, pt.x, pt.y, 4, 2);
  await page.waitForTimeout(250);
  out.A_microClick_open = await page.evaluate(() => document.getElementById('lightbox').classList.contains('open'));
  out.A_microClick_sel = await page.evaluate(() => (window.getSelection?String(window.getSelection()):'').trim());

  // Scenario B: deliberate drag-select (40px) -> should NOT open, should leave selection
  await page.evaluate(() => { document.getElementById('lightbox').classList.remove('on'); window.getSelection().removeAllRanges(); });
  await clickWithMove(page, pt.x, pt.y, 40, 10);
  await page.waitForTimeout(250);
  out.B_drag_open = await page.evaluate(() => document.getElementById('lightbox').classList.contains('open'));
  out.B_drag_sel = await page.evaluate(() => (window.getSelection?String(window.getSelection()):'').trim().slice(0,30));

  fs.writeFileSync('D:/LiteratureLens/scripts/diag_lightbox4_out.json', JSON.stringify(out, null, 2));
  console.log(JSON.stringify(out, null, 2));
  await browser.close();
})().catch(e => { console.error('FATAL', e); process.exit(1); });
