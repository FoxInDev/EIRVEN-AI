const path = require('node:path');

module.exports = {
  testDir: __dirname,
  testMatch: /playwright_mobile_lan\.spec\.cjs/,
  timeout: 30_000,
  workers: 1,
  reporter: [['line']],
  use: {
    baseURL: process.env.EIRVEN_PLAYWRIGHT_BASE_URL || 'http://127.0.0.1:8765',
    viewport: { width: 1440, height: 900 },
    launchOptions: {
      executablePath: process.env.EIRVEN_PLAYWRIGHT_CHROMIUM,
      args: ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'],
    },
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  outputDir: path.join('/tmp', 'eirven-playwright-r37-results'),
};
