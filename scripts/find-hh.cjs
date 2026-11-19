const { spawnSync } = require('node:child_process');
const { join } = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const queries = [
  'frontend OR фронтенд OR React OR Vue',
  'TypeScript OR JavaScript OR Next.js OR Nuxt',
  'Fullstack OR Full-stack OR Фулстек',
];

async function search(context, text, days, pageIndex) {
  const page = await context.newPage();
  try {
    const url = new URL('https://hh.ru/search/vacancy');
    url.search = new URLSearchParams({
      text, search_field: 'name', work_format: 'REMOTE', area: '113',
      order_by: 'publication_time', search_period: days, items_on_page: '50',
    });
    if (pageIndex) url.searchParams.set('page', pageIndex);
    const response = await page.goto(url.href, { waitUntil: 'commit', timeout: 12000 });
    if (!response?.ok() || new URL(page.url()).pathname !== url.pathname) {
      throw new Error('Выдача HH недоступна. Проверьте страницу в браузере.');
    }
    const links = page.locator('a[data-qa="serp-item__title"]');
    await links.first().waitFor({ timeout: 7500 });
    const records = new Map();
    let stable = 0;
    for (let step = 0; step < 30 && stable < 4; step++) {
      const rows = await links.evaluateAll(elements => {
        const rows = elements.flatMap(anchor => {
          const href = new URL(anchor.href);
          const match = /^\/vacancy\/(\d+)\/?$/.exec(href.pathname);
          if (href.hostname !== 'hh.ru' || !match) return [];
          return [{ id: match[1], title: anchor.innerText.trim(), url: href.origin + href.pathname }];
        });
        elements.at(-1)?.scrollIntoView({ block: 'end', behavior: 'instant' });
        return rows;
      });
      const size = records.size;
      for (const row of rows) records.set(row.id, row);
      stable = rows.length && records.size === size ? stable + 1 : 0;
      if (stable < 4) await page.waitForTimeout(400);
    }
    if (!records.size || stable < 4) throw new Error('Не удалось дочитать выдачу HH.');
    return [...records.values()];
  } finally {
    await page.close();
  }
}

async function main() {
  const [days = '3', page = '1', ...extra] = process.argv.slice(2);
  if (!['3', '7', '30'].includes(days) || !/^[1-9]\d*$/.test(page) ||
      !Number.isSafeInteger(Number(page)) || extra.length) {
    throw new Error('Использование: node scripts/find-hh.cjs [3|7|30] [номер страницы]');
  }
  const started = Date.now();
  const endpoint = process.env.CAREER_CDP_URL || 'http://127.0.0.1:9222';
  const browser = await chromium.connectOverCDP(endpoint, { timeout: 12000 });
  try {
    const results = await Promise.allSettled(queries.map(query => search(browser.contexts()[0], query, days, Number(page) - 1)));
    const rows = new Map();
    const searches = results.map((result, index) => {
      if (result.status === 'rejected') return { query: queries[index], error: result.reason.message.split('\n')[0] };
      for (const row of result.value) rows.set(row.id, row);
      return { query: queries[index], found: result.value.length };
    });
    let unseen = [];
    if (rows.size) {
      const check = spawnSync('python3', [join(__dirname, '../manage.py'), 'unseen', 'hh', ...rows.keys()],
        { encoding: 'utf8' });
      if (check.error || check.status !== 0) throw new Error('Не удалось сверить выдачу с дашбордом.');
      unseen = JSON.parse(check.stdout).map(id => rows.get(id));
    }
    console.log(JSON.stringify({ seconds: (Date.now() - started) / 1000, searches, unique: rows.size, unseen }));
    if (results.some(result => result.status === 'rejected')) process.exitCode = 1;
  } finally {
    await browser.close();
  }
}

main().catch(error => {
  console.error(error.message.replace(/https?:\/\/\S+/g, '[адрес]'));
  process.exitCode = 1;
});
