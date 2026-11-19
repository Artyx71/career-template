const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const rowSelector = '[data-qa="negotiations-item"]';
const listing = 'https://hh.ru/applicant/negotiations';

async function scrollRows(page, read, visit) {
  await page.locator(rowSelector).first().waitFor();
  // Прокручиваем область со строками небольшими шагами, включая виртуализированные списки.
  const rows = page.locator(rowSelector);
  let previous = '';
  let stable = 0;
  for (let step = 0; step < 100 && stable < 4; step++) {
    const batch = await rows.evaluateAll(read);
    if (await visit(batch)) return true;
    await rows.last().scrollIntoViewIfNeeded();
    const box = await rows.last().boundingBox();
    if (box) {
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      await page.mouse.wheel(0, 500);
    }
    await page.waitForTimeout(400);
    const position = await rows.evaluateAll(items => items.map(item => Math.round(item.getBoundingClientRect().top)));
    const signature = JSON.stringify([batch.map(item => item.rowKey), position]);
    stable = signature === previous ? stable + 1 : 0;
    previous = signature;
  }
  if (stable < 4) throw new Error('Список HH не достиг конца прокрутки');
  return false;
}

function readRows(elements) {
  return elements.map(e => {
    const vacancy = e.querySelector('[data-qa="negotiations-item-vacancy"]');
    const anchor = vacancy?.closest('a') || vacancy?.querySelector('a');
    const href = anchor?.href || '';
    const summary = e.querySelector('a[data-qa="know-vacancy-responses-summary-freemium"]');
    const id = href.match(/\/vacancy\/(\d+)/)?.[1];
    const summaryId = summary?.href.match(/\/vacancy-summary\/(\d+)/)?.[1];
    return {
      id,
      summaryId,
      rowKey: id ? 'vacancy:' + id : summaryId ? 'negotiation:' + summaryId : '',
      title: vacancy?.innerText?.trim() || anchor?.innerText?.trim() || '',
      company: e.querySelector('[data-qa="negotiations-item-company"]')?.innerText?.trim() || '',
      platform_status: e.querySelector('[data-qa*="negotiations-tag"]')?.innerText?.trim() || '',
      url: id ? href : '',
      hasChat: !!e.querySelector('[data-qa="open_chat"]'),
      at: new Date().toISOString(),
    };
  });
}

async function collect(page) {
  const records = new Map();
  const errors = [];
  const visited = new Set();
  let pages = 0;
  let chats = 0;
  let lastChatPath = '';
  await page.goto(listing, { waitUntil: 'domcontentloaded' });
  while (true) {
    const batch = new Map();
    try {
      await scrollRows(page, readRows, items => {
        for (const item of items) if (item.rowKey) batch.set(item.rowKey, item);
      });
    } catch (error) {
      if (await page.locator('input[name*="captcha"]').count()) {
        throw new Error('HH просит капчу. Откройте HH и решите её вручную.');
      }
      throw new Error('Не удалось полностью прочитать список HH. Проверьте вход и доступность страницы.');
    }
    const signature = [...batch.keys()].sort().join(',');
    if (!signature || visited.has(signature)) throw new Error('Страница HH пуста или повторилась.');
    visited.add(signature);
    pages++;
    const pageUrl = page.url();
    for (const row of batch.values()) {
      if (!row.hasChat) continue;
      const link = row.id ? `/vacancy/${row.id}` : `/vacancy-summary/${row.summaryId}`;
      const item = page.locator(rowSelector).filter({
        has: page.locator(`a:is([href$="${link}"], [href*="${link}?"], [href*="${link}#"])`),
      });
      try {
        const found = await scrollRows(page, readRows, items => items.some(value => value.rowKey === row.rowKey));
        if (!found) throw new Error('Строка не найдена');
        await item.locator('[data-qa="open_chat"]').click({ timeout: 5000 });
        const handle = await page.waitForFunction(() => {
          const frame = [...document.querySelectorAll('iframe')].find(item => {
            const url = new URL(item.src || 'about:blank');
            return item.checkVisibility({ visibilityProperty: true }) && url.hostname === 'chatik.hh.ru' && /^\/chat\/\d+\/?$/.test(url.pathname);
          });
          return frame || null;
        }, null, { timeout: 8000 });
        const frame = await handle.asElement().contentFrame();
        await handle.dispose();
        if (!frame) throw new Error('Чат не открылся');
        await frame.waitForURL(url => url.hostname === 'chatik.hh.ru'
          && /^\/chat\/\d+\/?$/.test(url.pathname) && url.pathname !== lastChatPath, { timeout: 8000 });
        // Виджет переиспользует iframe: атрибут src остаётся от первого чата.
        lastChatPath = new URL(frame.url()).pathname;
        row.chat_url = 'https://hh.ru' + lastChatPath;
        if (!row.id) {
          const vacancyLink = frame.locator('a[data-qa="chatik-header-vacancy-link"]');
          await vacancyLink.waitFor({ timeout: 3500 });
          const url = new URL(await vacancyLink.getAttribute('href'), frame.url());
          const id = /^\/vacancy\/(\d+)\/?$/.exec(url.pathname)?.[1];
          if (!id || !/(^|\.)hh\.ru$/.test(url.hostname)) throw new Error('Не найдена подтверждённая ссылка на вакансию');
          row.id = id;
          row.url = 'https://hh.ru/vacancy/' + id;
        }
        const bubbles = frame.locator('[data-qa="chat-bubble-text"]');
        await bubbles.first().waitFor({ timeout: 8000 });
        const messages = await bubbles.evaluateAll(items => items.map(item => ({
          text: item.textContent.trim(),
          outgoing: [...(item.closest('[data-qa~="chatik-chat-message-bubble"]')?.classList || [])]
            .some(name => name.startsWith('magritte-outgoing___')),
        })).filter(item => item.text));
        row.messages = messages.map(message => message.text);
        row.last_message_outgoing = messages.at(-1)?.outgoing === true;
        row.response = row.messages.slice(-4).join('\n\n─────\n\n');
        row.response_kind = 'chat_excerpt';
        chats++;
      } catch {
        errors.push(`Не удалось прочитать чат ${row.rowKey}`);
      }
      const close = page.locator('[data-qa="chatik-close-chatik"]:visible');
      if (await close.isVisible()) {
        try {
          await close.click({ timeout: 2500 });
          await page.locator('iframe[src*="chatik.hh.ru/chat/"]:visible').first()
            .waitFor({ state: 'hidden', timeout: 2500 });
          continue;
        } catch {}
      }
      await page.goto(pageUrl, { waitUntil: 'domcontentloaded' });
    }
    for (const item of batch.values()) {
      if (!item.id) continue;
      const {rowKey, summaryId, ...record} = item;
      records.set(item.id, record);
    }
    const next = page.locator(`[data-qa~="number-pages-${pages + 1}"]`);
    if (!await next.count()) break;
    const previousIds = [...batch.keys()];
    await next.click();
    await page.waitForFunction(({ selector, previousIds }) => {
      return [...document.querySelectorAll(selector)].some(item => {
        const vacancy = item.querySelector('[data-qa="negotiations-item-vacancy"]');
        const href = (vacancy?.closest('a') || vacancy?.querySelector('a'))?.href;
        const summary = item.querySelector('a[data-qa="know-vacancy-responses-summary-freemium"]');
        const id = href?.match(/\/vacancy\/(\d+)/)?.[1];
        const summaryId = summary?.href.match(/\/vacancy-summary\/(\d+)/)?.[1];
        const key = id ? 'vacancy:' + id : summaryId ? 'negotiation:' + summaryId : '';
        return key && !previousIds.includes(key);
      });
    }, { selector: rowSelector, previousIds });
  }
  if (!records.size) throw new Error('HH не вернул ни одной подтверждённой ссылки на вакансию.');
  return { records: [...records.values()], pages, chats, errors, at: new Date().toISOString() };
}

async function main() {
  const endpoint = process.env.CAREER_CDP_URL || 'http://127.0.0.1:9222';
  const browser = await chromium.connectOverCDP(endpoint, { timeout: 12000 });
  let page;
  try {
    page = await browser.contexts()[0].newPage();
    page.setDefaultTimeout(12000);
    process.stdout.write(JSON.stringify(await collect(page)));
  } finally {
    if (page) await page.close().catch(() => {});
    await browser.close();
  }
}

if (require.main === module) main().catch(error => {
  console.error(error.message.replace(/https?:\/\/\S+/g, '[адрес]'));
  process.exitCode = 1;
});
module.exports = { collect };
