const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

async function main() {
  const [id, name] = process.argv.slice(2);
  if (!/^[1-9]\d*$/.test(id) || !name) throw new Error('Не настроена рабочая папка Telegram');
  const browser = await chromium.connectOverCDP(process.env.CAREER_TELEGRAM_CDP_URL || 'http://127.0.0.1:9224', { timeout: 8000 });
  let page;
  try {
    page = await browser.contexts()[0].newPage();
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto('https://web.telegram.org/k/', { waitUntil: 'domcontentloaded', timeout: 20000 });
    const tab = page.locator(`.menu-horizontal-div-item[data-filter-id="${id}"]`);
    await tab.waitFor({ timeout: 12000 });
    const label = (await tab.innerText()).replace(/\s*\d+\s*$/, '').trim();
    if (label !== name) throw new Error('Рабочая папка Telegram изменилась');
    await tab.click();
    const list = page.locator(`.folders-scrollable.active[data-filter-id="${id}"]`);
    await list.waitFor({ timeout: 8000 });
    const records = new Map();
    let stable = 0;
    let previous = '';
    for (let step = 0; step < 35 && stable < 3; step++) {
      const snapshot = await list.evaluate(el => {
        const unread = Array.from(el.querySelectorAll('.chatlist-chat[data-peer-id]')).flatMap(row => {
          const peer = row.getAttribute('data-peer-id');
          const marker = row.querySelector('.dialog-subtitle-badge-unread.is-visible.unread');
          if (!/^[1-9]\d*$/.test(peer) || !marker) return [];
          const count = Number(marker.textContent.trim());
          const date = row.querySelector('.dialog-subtitle-date');
          return [{ peer, count: Number.isSafeInteger(count) && count > 0 ? count : 1,
            stamp: date?.getAttribute('title') || date?.textContent?.trim() || '' }];
        });
        el.scrollTop += Math.max(300, el.clientHeight * 0.8);
        return { unread, offset: el.scrollTop, height: el.scrollHeight };
      });
      for (const item of snapshot.unread) records.set(item.peer, item);
      const signature = JSON.stringify([snapshot.offset, snapshot.height, [...records.keys()]]);
      stable = signature === previous ? stable + 1 : 0;
      previous = signature;
      await page.waitForTimeout(250);
    }
    if (stable < 3) throw new Error('Не удалось проверить всю рабочую папку Telegram');
    process.stdout.write(JSON.stringify([...records.values()]));
  } finally {
    if (page) await page.close().catch(() => {});
    await browser.close();
  }
}

main().catch(() => {
  console.error('Не удалось открыть настроенную папку Telegram Web');
  process.exitCode = 1;
});
