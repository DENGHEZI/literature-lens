// Deeper diagnosis: is onclick assigned? what is at the click point?
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

  await page.evaluate(() => { selectDoc('d41d8cd9'); readTab = 'orig'; renderRead(); });
  await page.waitForSelector('#readBody .orig-page img', { timeout: 8000 }).catch(()=>{});
  await page.waitForTimeout(1500);

  // Inspect assignment + what's at center
  const inspect = await page.evaluate(() => {
    const pg = document.querySelector('#readBody .orig-page');
    const r = pg.getBoundingClientRect();
    const cx = r.x + r.width/2, cy = r.y + r.height/2;
    const elAt = document.elementFromPoint(cx, cy);
    const tl = pg.querySelector('.tl');
    const spans = tl ? tl.querySelectorAll('span') : [];
    let spanAtCenter = null;
    if (tl) {
      const s = document.elementFromPoint(cx, cy);
      spanAtCenter = s ? (s.closest('.tl span') ? 'span' : s.tagName + (s.className?('.'+s.className):'')) : null;
    }
    return {
      center: {cx, cy, rectTop:r.y, rectH:r.height, rectW:r.width},
      onclickType: typeof pg.onclick,
      elAtPoint: elAt ? (elAt.tagName + (elAt.className ? '.'+elAt.className : '')) : null,
      spanAtCenter,
      tlPointerEvents: tl ? getComputedStyle(tl).pointerEvents : null,
      spanCount: spans.length,
      firstSpanPE: spans[0] ? getComputedStyle(spans[0]).pointerEvents : null,
      lbOpenBefore: document.getElementById('lightbox').classList.contains('open'),
    };
  });
  out.inspect = inspect;

  // Try a synthetic dispatched click on the orig-page element
  const synth = await page.evaluate(() => {
    const pg = document.querySelector('#readBody .orig-page');
    pg.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, clientX: 700, clientY: 300 }));
    return { lbOpenAfterSynthetic: document.getElementById('lightbox').classList.contains('open') };
  });
  out.synth = synth;
  // reset
  await page.evaluate(() => document.getElementById('lightbox').classList.remove('on'));

  // Try Playwright real click on the selector
  await page.click('#readBody .orig-page').catch(e => out.clickErr = e.message);
  await page.waitForTimeout(300);
  out.afterPwClick = await page.evaluate(() => document.getElementById('lightbox').classList.contains('open'));

  out.consoleErrors = errors;
  fs.writeFileSync('D:/LiteratureLens/scripts/diag_lightbox2_out.json', JSON.stringify(out, null, 2));
  console.log(JSON.stringify(out, null, 2));
  await browser.close();
})().catch(e => { console.error('FATAL', e); process.exit(1); });
