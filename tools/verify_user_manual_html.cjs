// Offline authoring QA only. Uses a fresh headless browser, never user profiles.
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const {pathToFileURL} = require('url');
const {chromium} = require('playwright');

async function main() {
  const out = path.resolve(process.argv[2]);
  fs.mkdirSync(out, {recursive: false});
  const root = path.resolve(__dirname, '..');
  const url = pathToFileURL(path.join(root, 'docs/manual/index.html')).href;
  let browser;
  try { browser = await chromium.launch({headless: true}); }
  catch (error) {
    browser = await chromium.launch({headless: true, channel: 'msedge'});
  }
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
  const errors = [], requests = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('request', r => requests.push(r.url()));
  try {
    await page.goto(url);
    await page.evaluate(() => document.querySelectorAll('img[loading]').forEach(i => i.loading = 'eager'));
    await page.waitForFunction(() => [...document.querySelectorAll('figure img')].every(i => i.complete && i.naturalWidth > 0));
    assert.strictEqual(await page.locator('section').count(), 19);
    const links = await page.evaluate(() => [...document.querySelectorAll('a[href^="#"]')].map(a => a.getAttribute('href')).filter(h => !document.getElementById(h.slice(1))));
    assert.deepStrictEqual(links, []);
    await page.screenshot({path: path.join(out, 'desktop-index.png')});
    await page.locator('#manual-search').fill('核对已有采购单');
    assert(await page.locator('#c19').isVisible());
    await page.locator('#manual-search').fill('绝无此词QA未命中');
    assert.strictEqual(await page.locator('section:visible').count(), 0);
    await page.locator('aside a[href="#c17"]').click();
    assert.strictEqual(await page.locator('#manual-search').inputValue(), '');
    assert(await page.locator('#c17').isVisible());
    const practice = page.locator('.practice');
    await practice.locator('[data-weights="60,60"]').click();
    assert.strictEqual(await practice.getAttribute('data-valid'), 'false');
    assert((await practice.locator('output').innerText()).includes('-20'));
    await practice.locator('[data-weights="60,40"]').click();
    assert.strictEqual(await practice.getAttribute('data-valid'), 'true');
    assert((await practice.locator('output').innerText()).includes('w3 = 0%'));
    await practice.locator('input[name="w1"]').fill('');
    assert((await practice.locator('output').innerText()).includes('完整填写'));
    await practice.locator('[data-weights="60,20"]').click();
    await page.locator('#c17-s05').scrollIntoViewIfNeeded();
    await page.screenshot({path: path.join(out, 'desktop-weight-practice.png')});
    await page.locator('#c17 .zoom').first().click();
    assert(await page.locator('dialog').isVisible());
    assert(await page.locator('dialog img').getAttribute('src'));
    await page.locator('dialog button').click();
    assert.strictEqual(await page.locator('dialog').isVisible(), false);
    await page.locator('#c19-s05').scrollIntoViewIfNeeded();
    await page.screenshot({path: path.join(out, 'desktop-purchase-steps.png')});
    await page.setViewportSize({width: 390, height: 844});
    await page.locator('#c17-s04').scrollIntoViewIfNeeded();
    const overflow = await page.evaluate(() => ({width: innerWidth, content: document.documentElement.scrollWidth, tables: [...document.querySelectorAll('.table-scroll')].filter(e => e.scrollWidth > e.clientWidth).length}));
    assert(overflow.content <= overflow.width + 1, JSON.stringify(overflow));
    assert(overflow.tables > 0);
    await page.screenshot({path: path.join(out, 'mobile-scopes.png')});
    await page.locator('#c19-s05').scrollIntoViewIfNeeded();
    await page.screenshot({path: path.join(out, 'mobile-purchase-steps.png')});
    assert.deepStrictEqual(errors, []);
    assert.deepStrictEqual(requests.filter(u => /^https?:/i.test(u)), []);
    const report = {passed: true, chapters: 19, image_count: await page.locator('figure img').count(), checks: ['local images load', 'chapter and subsection anchors', 'search found/zero/reset', 'weight valid/invalid/blank/boundary', 'image zoom open/close', 'mobile table contained scroll', 'no page errors', 'no external requests'], overflow, browser: await browser.version(), errors, external_requests: [], files: fs.readdirSync(out)};
    fs.writeFileSync(path.join(out, 'html-report.json'), JSON.stringify(report, null, 2));
    console.log(JSON.stringify(report));
  } finally { await browser.close(); }
}

main().catch(e => {console.error(e); process.exitCode = 1;});
