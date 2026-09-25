// Verify "标记关键句": mark selection -> highlight + persist -> re-render reapplies -> panel delete
const { chromium } = require('playwright-core');
const fs = require('fs');

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
  await page.waitForSelector('#readBody .orig-page .tl span', { timeout: 8000 }).catch(()=>{});
  await page.waitForTimeout(2500); // let upload word layers load
  await page.evaluate(() => { document.getElementById('loginLayer').classList.remove('on'); });

  // 1) Create a selection over several word-spans on page 1, then click 标记
  const mkRes = await page.evaluate(() => {
    const tl = document.querySelector('#readBody .orig-page .tl');
    const spans = [...tl.children].slice(5, 9);
    const range = document.createRange();
    range.setStartBefore(spans[0]);
    range.setEndAfter(spans[spans.length - 1]);
    const sel = window.getSelection();
    sel.removeAllRanges(); sel.addRange(range);
    const txt = sel.toString();
    document.getElementById('selMark').click();
    const mkNow = [...document.querySelectorAll('#readBody .orig-page .tl span.mk')].length;
    const stored = JSON.parse(localStorage.getItem('lens.marks.d41d8cd9') || '[]');
    const badge = document.getElementById('markCnt').textContent;
    return { selLen: txt.length, mkNow, storedCount: stored.length, storedIdxs: stored[0] && stored[0].idxs, badge };
  });
  out.mark = mkRes;

  // 2) Persistence: re-render (selectDoc again) and check highlights reapplied
  await page.evaluate(() => { selectDoc('d41d8cd9'); readTab = 'orig'; renderRead(); });
  await page.waitForSelector('#readBody .orig-page .tl span', { timeout: 8000 }).catch(()=>{});
  await page.waitForTimeout(2500);
  out.afterRerender = await page.evaluate(() => {
    const mk = [...document.querySelectorAll('#readBody .orig-page .tl span.mk')].length;
    const badge = document.getElementById('markCnt').textContent;
    return { mkCount: mk, badge };
  });

  // 3) Open panel, check list, then delete the mark
  out.panel = await page.evaluate(() => {
    openMarkPanel();
    const items = document.querySelectorAll('#mpList .mp-item').length;
    const empty = !!document.querySelector('.mp-empty');
    return { items, empty, panelOpen: document.getElementById('markPanel').classList.contains('on') };
  });
  out.afterDelete = await page.evaluate(() => {
    const del = document.querySelector('#mpList .del');
    if (del) del.click();
    const mk = [...document.querySelectorAll('#readBody .orig-page .tl span.mk')].length;
    const stored = JSON.parse(localStorage.getItem('lens.marks.d41d8cd9') || '[]').length;
    const badge = document.getElementById('markCnt').textContent;
    return { mkCount: mk, storedCount: stored, badge };
  });

  out.pageErrors = errors;
  fs.writeFileSync('D:/LiteratureLens/scripts/verify_marks_out.json', JSON.stringify(out, null, 2));
  console.log(JSON.stringify(out, null, 2));
  await browser.close();
})().catch(e => { console.error('FATAL', e); process.exit(1); });
