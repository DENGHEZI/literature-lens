// Diagnose lightbox (zoom) not opening in page-image mode
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
  // Wait for app boot
  await page.waitForFunction(() => typeof selectDoc === 'function', { timeout: 8000 }).catch(()=>{});

  // Enter page-image mode for an UPLOAD doc
  const docId = 'd41d8cd9';
  await page.evaluate((id) => {
    selectDoc(id);
    readTab = 'orig';
    renderRead();
  }, docId);

  // Wait for page image to load
  await page.waitForSelector('#readBody .orig-page img', { timeout: 8000 }).catch(()=>{});
  await page.waitForTimeout(1500);

  const before = await page.evaluate(() => {
    const pg = document.querySelector('#readBody .orig-page');
    const img = pg && pg.querySelector('img');
    return {
      hasOrigPage: !!pg,
      imgComplete: img ? img.complete : null,
      imgNaturalW: img ? img.naturalWidth : null,
      lightboxOpenBefore: document.getElementById('lightbox').classList.contains('open'),
      selText: (window.getSelection ? String(window.getSelection()) : '').trim(),
      lbSrcBefore: document.getElementById('lbImg').src,
    };
  });

  // Click the center of the orig-page
  const box = await page.evaluate(() => {
    const pg = document.querySelector('#readBody .orig-page');
    const r = pg.getBoundingClientRect();
    return { x: r.x + r.width/2, y: r.y + r.height/2 };
  });
  await page.mouse.click(box.x, box.y);
  await page.waitForTimeout(400);

  const after = await page.evaluate(() => {
    const lb = document.getElementById('lightbox');
    const img = document.getElementById('lbImg');
    return {
      lightboxOpenAfter: lb.classList.contains('open'),
      lbDisplay: getComputedStyle(lb).display,
      lbSrc: img.src,
      lbNaturalW: img.naturalWidth,
      selTextAfter: (window.getSelection ? String(window.getSelection()) : '').trim(),
    };
  });

  out.before = before;
  out.after = after;
  out.consoleErrors = errors;

  // Also test clicking via dispatching on the span and on empty area
  const probe = await page.evaluate(() => {
    const res = {};
    // try calling openLightbox directly
    try { openLightbox(1); res.directOpenWorks = document.getElementById('lightbox').classList.contains('open'); }
    catch(e){ res.directOpenErr = e.message; }
    document.getElementById('lightbox').classList.remove('open');
    return res;
  });
  out.probe = probe;

  fs.writeFileSync('D:/LiteratureLens/scripts/diag_lightbox_out.json', JSON.stringify(out, null, 2));
  console.log(JSON.stringify(out, null, 2));
  await browser.close();
})().catch(e => { console.error('FATAL', e); process.exit(1); });
