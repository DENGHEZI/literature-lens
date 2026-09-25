// Instrumented debug: what fires on micro-click? does lbClose handler run?
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
  const logs = [];
  page.on('console', m => logs.push('['+m.type()+'] '+m.text()));
  page.on('pageerror', e => logs.push('PAGEERR: ' + e.message));
  await page.goto('http://127.0.0.1:8877/', { waitUntil: 'networkidle' });
  await page.waitForFunction(() => typeof selectDoc === 'function', { timeout: 8000 }).catch(()=>{});
  await page.evaluate(() => { document.getElementById('loginLayer').classList.remove('on'); });
  await page.evaluate(() => { selectDoc('d41d8cd9'); readTab = 'orig'; renderRead(); });
  await page.waitForSelector('#readBody .orig-page img', { timeout: 8000 }).catch(()=>{});
  await page.waitForTimeout(1500);
  await page.evaluate(() => { document.getElementById('loginLayer').classList.remove('on'); });

  // Instrument openLightbox + check handler presence
  const setup = await page.evaluate(() => {
    window.__openCalls = [];
    const _ol = openLightbox;
    openLightbox = function(n){ window.__openCalls.push(n); return _ol(n); };
    const pg = document.querySelector('#readBody .orig-page');
    return {
      hasMousedown: typeof pg.ondown, // not used
      onclickType: typeof pg.onclick,
      lbCloseOnclick: typeof document.getElementById('lbClose').onclick,
    };
  });
  out.setup = setup;

  const pt = await page.evaluate(() => {
    const pg = document.querySelector('#readBody .orig-page');
    const r = pg.getBoundingClientRect();
    return { x: r.x + r.width/2, y: r.y + r.height/2 };
  });

  // micro click
  await page.evaluate(() => document.getElementById('lightbox').classList.remove('open'));
  await clickWithMove(page, pt.x, pt.y, 5, 3);
  await page.waitForTimeout(250);
  out.afterMicro = await page.evaluate(() => ({
    open: document.getElementById('lightbox').classList.contains('open'),
    openCalls: window.__openCalls,
  }));

  // direct close handler invocation
  out.directClose = await page.evaluate(() => {
    document.getElementById('lightbox').classList.add('open');
    const before = document.getElementById('lightbox').classList.contains('open');
    document.getElementById('lbClose').onclick();
    const after = document.getElementById('lightbox').classList.contains('open');
    return { before, after, handlerType: typeof document.getElementById('lbClose').onclick };
  });

  out.logs = logs;
  fs.writeFileSync('D:/LiteratureLens/scripts/debug_lightbox_out.json', JSON.stringify(out, null, 2));
  console.log(JSON.stringify(out, null, 2));
  await browser.close();
})().catch(e => { console.error('FATAL', e); process.exit(1); });
