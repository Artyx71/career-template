const fs = require('node:fs');
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.CAREER_CHROMIUM });
  try {
    const page = await browser.newPage();
    await page.setContent(fs.readFileSync(process.argv[2], 'utf8'));
    await page.pdf({ path: process.argv[3], format: 'A4', preferCSSPageSize: true });
  } finally {
    await browser.close();
  }
})().catch(error => {
  console.error(error.message);
  process.exitCode = 1;
});
