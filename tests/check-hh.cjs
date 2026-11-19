const assert = require('node:assert/strict');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const { collect } = require('../scripts/sync-hh.cjs');

async function run() {
  const browser = await chromium.launch({ executablePath: process.env.CAREER_CHROMIUM });
  try {
    const page = await browser.newPage({ viewport: { width: 900, height: 650 } });
    let listingVisits = 0;
    await page.route('https://hh.ru/applicant/negotiations**', async route => {
      listingVisits++;
      const second = new URL(route.request().url()).searchParams.has('page');
      await route.fulfill({ contentType: 'text/html; charset=utf-8', body: `
        <main id="rows"></main>
        ${second ? '' : '<a data-qa="number-pages-2" href="?page=2">Далее</a>'}
        <script>
          const rows = document.querySelector('#rows');
          function add(id) {
            rows.insertAdjacentHTML('beforeend', \`<article data-qa="negotiations-item" style="height:160px">
              \${id === 9 || id === 10
                ? '<span data-qa="negotiations-item-vacancy">Архивная вакансия</span><a data-qa="know-vacancy-responses-summary-freemium" href="/applicant-services/vacancy-summary/' + (1000 + id) + '">Статистика</a>'
                : '<a data-qa="negotiations-item-vacancy" href="/vacancy/' + id + '">Вакансия</a>'}
              <span data-qa="negotiations-tag">Приглашение</span>
              <button data-qa="open_chat" onclick="openChat(\${id})">Чат</button></article>\`);
          }
          function openChat(id) {
            const widget = document.querySelector('#chat-widget');
            if (widget) {
              widget.hidden = false;
              widget.querySelector('iframe').contentWindow.location = 'https://chatik.hh.ru/chat/' + id;
              return;
            }
            document.body.insertAdjacentHTML('afterbegin', \`<div id="chat-widget" style="position:fixed; top:0; right:0">
              <iframe src="https://chatik.hh.ru/chat/\${id}"></iframe>
              \${id < 10 ? '<button data-qa="chatik-close-chatik" onclick="this.parentElement.hidden=true">Закрыть</button>' : ''}
            </div>\`);
          }
          for (const id of ${second ? '[10]' : '[1,2,3,4,5,6,7,8]'}) add(id);
          if (!${second}) {
            let loaded = false;
            addEventListener('scroll', () => {
              if (!loaded && innerHeight + scrollY >= document.documentElement.scrollHeight - 10) {
                loaded = true;
                setTimeout(() => add(9), 120);
              }
            }, { once: false });
          }
        </script>` });
    });
    await page.route('https://chatik.hh.ru/chat/**', route => route.fulfill({
      contentType: 'text/html; charset=utf-8',
      body: '<a data-qa="chatik-header-vacancy-link" href="https://hh.ru/vacancy/'
        + new URL(route.request().url()).pathname.split('/').at(-1)
        + '">Вакансия</a><p data-qa="chat-bubble-text">Сообщение из тестового чата</p>',
    }));
    const result = await collect(page);
    assert.equal(result.pages, 2);
    assert.equal(result.records.length, 10);
    assert.ok(result.records.every(item => Number(item.id) <= 10));
    assert.equal(result.chats, 10);
    assert.equal(listingVisits, 3);
    assert.equal(result.records.find(item => item.id === '10').title, 'Архивная вакансия');
    assert.equal(new Set(result.records.map(item => item.chat_url)).size, 10);
    assert.equal(result.records.find(item => item.id === '9').chat_url, 'https://hh.ru/chat/9');
    assert.deepEqual(result.errors, []);
    console.log('HH: динамически загруженная строка, две страницы, 10 чатов.');
  } finally {
    await browser.close();
  }
}
run().catch(error => { console.error(error); process.exitCode = 1; });
