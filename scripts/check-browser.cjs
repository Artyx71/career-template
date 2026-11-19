const assert = require('node:assert/strict');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

async function main() {
  const base = process.env.CAREER_QA_URL || 'http://127.0.0.1:4317';
  const browser = await chromium.launch({ executablePath: process.env.CAREER_CHROMIUM });
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));

    for (const width of [390, 1440]) {
      await page.setViewportSize({ width, height: 844 });
      await page.goto(base);
      await page.locator('#stat-total').filter({ hasText: /\d/ }).waitFor();
      assert.equal(await page.locator('#error-banner').isVisible(), false);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);

      await page.locator('[data-view="documents"]').click();
      await page.locator('#documents-list').getByText(/PDF-резюме пока не найдены|локальный файл/).first().waitFor();

      await page.locator('[data-view="inbox"]').click();
      await page.locator('#inbox-page').filter({ hasText: /\d+ \/ \d+/ }).waitFor();

      await page.locator('a[href="/trainer"]').click();
      await page.locator('#question-card[aria-busy="false"]').waitFor();
      assert.equal(await page.locator('#trainer-error').isVisible(), false);
    }

    const csrf = await page.request.post(base + '/api/settings', { data: { autoHH: true } });
    assert.equal(csrf.status(), 403);
    const badHost = await page.request.get(base + '/api/stats', { headers: { Host: 'evil.example' } });
    assert.equal(badHost.status(), 403);
    const csv = await page.request.get(base + '/api/export.csv');
    assert.equal(csv.status(), 200);
    assert.deepEqual(errors, [], 'В браузере нет необработанных ошибок');
    console.log('Проверены дашборд, резюме, входящие, тренажёр и защита API.');
  } finally {
    await browser.close();
  }
}

main().catch(error => {
  console.error(error.message);
  process.exitCode = 1;
});
