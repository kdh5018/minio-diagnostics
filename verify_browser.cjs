'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const playwright = require('playwright');

// BROWSER=chrome(기본)|msedge|chromium|firefox|webkit
const KIND = process.env.BROWSER || 'chrome';
function launch() {
  if (KIND === 'chrome' || KIND === 'msedge') return playwright.chromium.launch({ channel: KIND, headless: true });
  if (!['chromium', 'firefox', 'webkit'].includes(KIND)) throw new Error('unknown BROWSER ' + KIND);
  return playwright[KIND].launch({ headless: true });
}

// Playwright의 :visible은 WebKit에서 닫힌 <details> 안의 요소를 CSS와 무관하게 항상 숨김으로 본다(엔진 우회 규칙).
// 인쇄 펼침은 실제 배치(상자 크기·checkVisibility)로 센다.
const laidOutDetailsChildren = target => target.evaluate(() => {
  const kids = [...document.querySelectorAll('details > :not(summary)')];
  const shown = kids.filter(el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility === 'visible' && (!el.checkVisibility || el.checkVisibility()); });
  return { shown: shown.length, all: kids.length };
});

async function main() {
  const [htmlPath, outputDirectory, hostileHtmlPath] = process.argv.slice(2);
  if (!htmlPath || !outputDirectory || !hostileHtmlPath) {
    throw new Error('Usage: node verify_browser.cjs REPORT.html PRIVATE_EXISTING_DIRECTORY HOSTILE.html');
  }
  assert((await fs.stat(outputDirectory)).isDirectory());
  const report = JSON.parse(await fs.readFile(path.join(path.dirname(htmlPath), 'aistor-diag-report.json'), 'utf8'));
  const browser = await launch();
  const externalRequests = [];
  const pageErrors = [];
  const consoleErrors = [];
  const cases = [];
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    await context.route(/^https?:\/\//, async route => {
      externalRequests.push(route.request().url());
      await route.abort();
    });
    const page = await context.newPage();
    page.on('pageerror', error => pageErrors.push(error.message));
    page.on('console', message => {
      if (message.type() === 'error') consoleErrors.push(message.text());
    });
    await page.goto(pathToFileURL(htmlPath).href, { waitUntil: 'load' });
    assert.equal(await page.locator('article').count(), report.findings.length);
    assert((await page.locator('body').innerText()).includes(report.tool_version));
    for (const [select, field] of [['#s', 'official_status'], ['#p', 'priority'], ['#c', 'category']]) {
      const values = await page.locator(`${select} option`).evaluateAll(options => options.map(option => option.value));
      for (const value of values) {
        await page.locator(select).selectOption(value);
        const expected = report.findings.filter(finding => {
          const actualField = field === 'official_status' && finding.origin === 'custom_rule'
            ? '해당 없음 / 자체 결과: ' + (finding.rule_result || '미평가') : finding[field];
          return !value || actualField === value;
        }).length;
        const actual = await page.locator('article:visible').count();
        assert.equal(actual, expected);
        cases.push({ select, value, expected, actual });
      }
      await page.locator(select).selectOption('');
    }
    await page.locator('#q').fill('no-such-finding-7fbf28');
    assert.equal(await page.locator('article:visible').count(), 0);
    if (report.findings.length) {
      await page.locator('#q').fill(report.findings[0].title);
      assert((await page.locator('article:visible').count()) > 0);
    }
    await page.locator('#q').fill('');
    await page.locator('#q').focus();
    await page.keyboard.press('Tab');
    assert.equal(await page.locator('#s').evaluate(element => element === document.activeElement), true);
    // 처음부터 접힌 details를 위치로 고정한다(열린 뒤 다른 요소로 바뀌지 않도록).
    const closedIndex = await page.locator('details').evaluateAll(items => items.findIndex(item => !item.open));
    const disclosure = page.locator('details').nth(Math.max(closedIndex, 0));
    const hasDisclosure = closedIndex >= 0;
    if (hasDisclosure) {
      await disclosure.locator('summary').focus();
      await page.keyboard.press('Enter');
      assert.equal(await disclosure.getAttribute('open'), '');
    }
    if (report.findings.some(finding => finding.evidence_blocks.some(block => block.type === 'table'))) {
      assert((await page.locator('table').count()) > 0);
    }
    const layout = async () => page.evaluate(() => ({
      horizontalOverflow: document.documentElement.scrollWidth > window.innerWidth,
      articles: document.querySelectorAll('article').length,
      textLength: document.body.innerText.length,
    }));
    const desktop = await layout();
    assert.equal(desktop.horizontalOverflow, false);
    assert(desktop.textLength > 100);
    await page.screenshot({ path: path.join(outputDirectory, 'desktop.png'), fullPage: false });
    await page.setViewportSize({ width: 390, height: 844 });
    const mobile = await layout();
    assert.equal(mobile.horizontalOverflow, false);
    await page.screenshot({ path: path.join(outputDirectory, 'mobile.png'), fullPage: false });
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.emulateMedia({ media: 'print' });
    assert.equal(await page.locator('.filters').isVisible(), false);
    assert.equal(await page.locator('article:visible').count(), report.findings.length);
    // 0.5.0: 인쇄 시 접힌 근거·부록이 모두 펼쳐져야 한다.
    { const d = await laidOutDetailsChildren(page); assert.equal(d.shown, d.all); }
    await page.screenshot({ path: path.join(outputDirectory, 'print.png'), fullPage: true });
    // 인쇄 PDF는 Chromium 계열만 지원(Playwright page.pdf). 다른 엔진은 인쇄 매체 화면으로 대신한다.
    const printPdf = KIND === 'firefox' || KIND === 'webkit' ? 'not_supported_by_engine' : 'written';
    if (printPdf === 'written') await page.pdf({ path: path.join(outputDirectory, 'print.pdf'), printBackground: true });
    await page.emulateMedia({ media: 'screen' });
    // 0.5.0: 심각도 버튼(전체/오류/경고/통과)은 data-sev 기준으로 카드를 거른다.
    const severityCases = [];
    for (const button of await page.locator('#filters button[data-sev]').all()) {
      const sev = await button.getAttribute('data-sev');
      await button.click();
      const expected = await page.locator(sev ? `article[data-sev="${sev}"]` : 'article').count();
      assert.equal(await page.locator('article:visible').count(), expected);
      assert.equal(await button.getAttribute('aria-pressed'), 'true');
      severityCases.push({ sev, expected });
    }
    await page.locator('#filters button[data-sev=""]').click();
    for (const scheme of ['light', 'dark']) {
      await page.emulateMedia({ colorScheme: scheme });
      await page.screenshot({ path: path.join(outputDirectory, `full-${scheme}.png`), fullPage: true });
    }
    await page.emulateMedia({ colorScheme: 'light' });
    // 0.5.0: JS를 꺼도 모든 카드·도식이 보이고, 동작하지 않는 필터는 숨겨진다. 인쇄도 CSS만으로 펼쳐진다.
    const noJs = await browser.newContext({ viewport: { width: 1440, height: 1000 }, javaScriptEnabled: false });
    const plain = await noJs.newPage();
    await plain.goto(pathToFileURL(htmlPath).href, { waitUntil: 'load' });
    assert.equal(await plain.locator('article:visible').count(), report.findings.length);
    assert.equal(await plain.locator('#filters').isVisible(), false);
    assert.equal(await plain.locator('#dashboard').isVisible(), true);
    await plain.screenshot({ path: path.join(outputDirectory, 'nojs.png'), fullPage: true });
    await plain.emulateMedia({ media: 'print' });
    { const d = await laidOutDetailsChildren(plain); assert.equal(d.shown, d.all); }
    await plain.screenshot({ path: path.join(outputDirectory, 'nojs-print.png'), fullPage: true });
    // 구형 엔진 대비: CSS를 무시(style 블록 제거)하고 JS도 끈 사본에서 카드·핵심 숫자가 모두 글자로 보이는지.
    const html = await fs.readFile(htmlPath, 'utf8');
    const bare = html.replace(/<style[\s\S]*?<\/style>/gi, '').replace(/\sstyle="[^"]*"/gi, '');
    assert(!/<style/i.test(bare));
    const barePath = path.join(outputDirectory, 'nocss-copy.html');
    await fs.writeFile(barePath, bare);
    const plain2 = await noJs.newPage();
    await plain2.goto(pathToFileURL(barePath).href, { waitUntil: 'load' });
    assert.equal(await plain2.locator('article:visible').count(), report.findings.length);
    const bareText = await plain2.locator('body').innerText();
    const s = report.official_summary || {};
    const keyNumbers = {};
    for (const [label, value] of [['통과', s.passed], ['오류', s.errors], ['경고', s.warnings]]) {
      if (value === undefined) continue;
      keyNumbers[label] = new RegExp(label + '\\s*' + value + '(?!\\d)').test(bareText);
      assert(keyNumbers[label], 'no-CSS text lacks ' + label + ' ' + value);
    }
    await plain2.screenshot({ path: path.join(outputDirectory, 'nocss-nojs.png'), fullPage: false });
    await fs.unlink(barePath);
    await noJs.close();
    await page.goto(pathToFileURL(hostileHtmlPath).href, { waitUntil: 'load' });
    assert.equal(await page.locator('img,iframe,object,embed').count(), 0);
    assert.equal(await page.evaluate(() => window.__aistorInjection === undefined), true);
    await page.locator('details summary').first().click();
    assert.equal(externalRequests.length, 0);
    assert.equal(pageErrors.length, 0);
    assert.equal(consoleErrors.length, 0);
    const result = {
      engine: KIND, browser: browser.version(), protocol: 'file:', printPdf, noCssNoJs: { articlesVisible: 'passed', keyNumbers }, toolVersion: report.tool_version,
      inputGeneration: path.basename(path.dirname(htmlPath)), filters: cases,
      search: 'passed', severityButtons: severityCases, noJs: 'passed', printExpandsDetails: 'passed', keyboardDisclosure: hasDisclosure ? 'passed' : 'not_applicable', printLayout: 'passed',
      hostileMarkup: 'passed', desktop, mobile, externalRequests, pageErrors, consoleErrors,
      scope: 'Application-page requests only; not Chrome background services or every Markdown viewer.',
    };
    await fs.writeFile(path.join(outputDirectory, 'result.json'), JSON.stringify(result, null, 2) + '\n');
    console.log(JSON.stringify(result));
    await context.close();
  } finally {
    await browser.close();
  }
}

main().catch(error => {
  console.error(error.stack || error.message);
  process.exitCode = 1;
});
