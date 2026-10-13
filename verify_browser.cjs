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
        const expected = report.findings.filter(finding => !value || finding[field] === value).length;
        const actual = await page.locator('article:visible').count();
        assert.equal(actual, expected);
        cases.push({ select, value, expected, actual });
      }
      await page.locator(select).selectOption('');
    }
    await page.locator('#q').fill('no-such-finding-7fbf28');
    assert.equal(await page.locator('article:visible').count(), 0);
    await page.locator('#q').fill(report.findings[0].title);
    assert((await page.locator('article:visible').count()) > 0);
    await page.locator('#q').fill('');
    await page.locator('#q').focus();
    await page.keyboard.press('Tab');
    assert.equal(await page.locator('#s').evaluate(element => element === document.activeElement), true);
    const disclosure = page.locator('details').first();
    await disclosure.locator('summary').focus();
    await page.keyboard.press('Enter');
    assert.equal(await disclosure.getAttribute('open'), '');
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
    await page.screenshot({ path: path.join(outputDirectory, 'print.png'), fullPage: false });
    await page.emulateMedia({ media: 'screen' });
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
      search: 'passed', keyboardDisclosure: 'passed', printLayout: 'passed',
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
  console.error(error.message);
  process.exitCode = 1;
});
