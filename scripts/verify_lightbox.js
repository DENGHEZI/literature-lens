// Verify lightbox: open (clean + micro), NOT on drag, close via X / backdrop, prev/next
const { chromium } = require('playwright-core');
const fs = require('fs');

async function clickWithMove(page, x, y, dx, dy){
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x + dx, y + dy, { steps: 3 });
  await page.mouse.up();
}
const isOpen = p => p.evaluate(() => document.getElementById('lightbox').classList.contains('open'));

(async () => {
  const out = {};
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
  const errors = [];
  page.on('pageerror', e => errors.push('PAGEERR: ' + e.message));
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

  // T1 clean click -> open + image loaded
  await page.evaluate(() => document.getElementById('lightbox').classList.remove('open'));
  await page.mouse.click(pt.x, pt.y);
  await page.waitForTimeout(250);
  out.T1_clean_open = await isOpen(page);
  out.T1_imgLoaded = await page.evaluate(() => { const i=document.getElementById('lbImg'); return i.complete && i.naturalWidth>0; });
  out.T1_pg = await page.evaluate(() => document.getElementById('lbPg').textContent);

  // T2 micro-movement click -> open (the real fix)
  await page.evaluate(() => document.getElementById('lightbox').classList.remove('open'));
  await clickWithMove(page, pt.x, pt.y, 5, 3);
  await page.waitForTimeout(250);
  out.T2_micro_open = await isOpen(page);

  // T3 drag-select -> should NOT open, and selection persists
  await page.evaluate(() => { document.getElementById('lightbox').classList.remove('open'); window.getSelection().removeAllRanges(); });
  await clickWithMove(page, pt.x, pt.y, 45, 12);
  await page.waitForTimeout(250);
  out.T3_drag_open = await isOpen(page);
  out.T3_selLen = await page.evaluate(() => (window.getSelection?String(window.getSelection()):'').trim().length);

  // T4 close via X
  await page.evaluate(() => document.getElementById('lightbox').classList.add('open'));
  await page.waitForTimeout(100);
  await page.click('#lbClose');
  await page.waitForTimeout(150);
  out.T4_closeX = !(await isOpen(page));

  // T5 close via backdrop click
  await page.evaluate(() => document.getElementById('lightbox').classList.add('open'));
  await page.waitForTimeout(100);
  await page.mouse.click(20, 20); // top-left corner = backdrop
  await page.waitForTimeout(150);
  out.T5_closeBackdrop = !(await isOpen(page));

  // T6 prev/next navigation
  await page.evaluate(() => { document.getElementById('lightbox').classList.add('open'); });
  await page.click('#lbNext');
  await page.waitForTimeout(150);
  out.T6_nextPg = await page.evaluate(() => document.getElementById('lbPg').textContent);
  await page.click('#lbPrev');
  await page.waitForTimeout(150);
  out.T6_prevPg = await page.evaluate(() => document.getElementById('lbPg').textContent);

  out.pageErrors = errors;
  fs.writeFileSync('D:/LiteratureLens/scripts/verify_lightbox_out.json', JSON.stringify(out, null, 2));
  console.log(JSON.stringify(out, null, 2));
  await browser.close();
})().catch(e => { console.error('FATAL', e); process.exit(1); });
