// Final diagnosis: dismiss login layer (simulate logged-in), then real mouse click on orig-page
const { chromium } = require('playwright-core');
const fs = require('fs');

(async () => {
  const out = {};
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
  const errors = [];
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', e => errors.push('PAGEERR: ' + e.message));

  await page.goto('http://127.0.0.1:8877/', { waitUntil: 'networkidle' });
  await page.waitForFunction(() => typeof selectDoc === 'function', { timeout: 8000 }).catch(()=>{});

  // Simulate logged-in: hide login layer
  await page.evaluate(() => { document.getElementById('loginLayer').classList.remove('on'); });

  await page.evaluate(() => { selectDoc('d41d8cd9'); readTab = 'orig'; renderRead(); });
  await page.waitForSelector('#readBody .orig-page img', { timeout: 8000 }).catch(()=>{});
  await page.waitForTimeout(1500);

  // Dismiss login again (renderRead might not re-show it, but be safe)
  await page.evaluate(() => { document.getElementById('loginLayer').classList.remove('on'); });

  const inspect = await page.evaluate(() => {
    const pg = document.querySelector('#readBody .orig-page');
    const r = pg.getBoundingClientRect();
    const cx = r.x + r.width/2, cy = r.y + r.height/2;
    const elAt = document.elementFromPoint(cx, cy);
    return {
      cx, cy,
      elAtPoint: elAt ? (elAt.tagName + (elAt.className ? '.'+elAt.className : '')) : null,
      loginOn: document.getElementById('loginLayer').classList.contains('on'),
      lbOpenBefore: document.getElementById('lightbox').classList.contains('open'),
    };
  });
  out.inspect = inspect;

  // Real mouse click at center
  await page.mouse.click(inspect.cx, inspect.cy);
  await page.waitForTimeout(300);
  out.afterRealClick = await page.evaluate(() => document.getElementById('lightbox').classList.contains('open'));

  // If still not open, try clicking on a specific tl span vs empty area
  if (!out.afterRealClick) {
    const probe = await page.evaluate(() => {
      const pg = document.querySelector('#readBody .orig-page');
      const r = pg.getBoundingClientRect();
      // click top-left empty corner of the page (above most text? not reliable)
      // Instead: directly check what elementFromPoint returns across a grid
      const hits = [];
      for (let gx = 0.1; gx <= 0.9; gx += 0.1) {
        for (let gy = 0.1; gy <= 0.9; gy += 0.1) {
          const x = r.x + r.width*gx, y = r.y + r.height*gy;
          const el = document.elementFromPoint(x, y);
          hits.push(el ? (el.tagName + (el.className && el.className.baseVal===undefined? '.'+el.className : '')) : 'null');
        }
      }
      return { hitSample: [...new Set(hits)] };
    });
    out.probe = probe;
  }

  out.consoleErrors = errors;
  fs.writeFileSync('D:/LiteratureLens/scripts/diag_lightbox3_out.json', JSON.stringify(out, null, 2));
  console.log(JSON.stringify(out, null, 2));
  await browser.close();
})().catch(e => { console.error('FATAL', e); process.exit(1); });
