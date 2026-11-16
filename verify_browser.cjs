'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { chromium } = require('playwright');

async function main() {
  const [htmlPath, outputDirectory, hostileHtmlPath] = process.argv.slice(2);
  if (!htmlPath || !outputDirectory || !hostileHtmlPath) {
    throw new Error('Usage: node verify_browser.cjs REPORT.html PRIVATE_EXISTING_DIRECTORY HOSTILE.html');
  }
  assert((await fs.stat(outputDirectory)).isDirectory());
  const report = JSON.parse(await fs.readFile(path.join(path.dirname(htmlPath), 'aistor-diag-report.json'), 'utf8'));
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
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
    assert.equal(await page.locator('details > :not(summary):visible').count(), await page.locator('details > :not(summary)').count());
    await page.screenshot({ path: path.join(outputDirectory, 'print.png'), fullPage: true });
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
    assert.equal(await plain.locator('details > :not(summary):visible').count(), await plain.locator('details > :not(summary)').count());
    await plain.screenshot({ path: path.join(outputDirectory, 'nojs-print.png'), fullPage: true });
    await noJs.close();
    await page.goto(pathToFileURL(hostileHtmlPath).href, { waitUntil: 'load' });
    assert.equal(await page.locator('img,iframe,object,embed').count(), 0);
    assert.equal(await page.evaluate(() => window.__aistorInjection === undefined), true);
    await page.locator('details summary').first().click();
    assert.equal(externalRequests.length, 0);
    assert.equal(pageErrors.length, 0);
    assert.equal(consoleErrors.length, 0);
    const result = {
      browser: browser.version(), protocol: 'file:', toolVersion: report.tool_version,
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
