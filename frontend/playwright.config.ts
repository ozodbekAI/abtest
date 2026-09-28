import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  fullyParallel: true,
  workers: 2,
  reporter: [['list'], ['html', { outputFolder: '../audit/2026-09-27/frontend-browser-report', open: 'never' }]],
  outputDir: '../audit/2026-09-27/frontend-browser-artifacts',
  use: {
    baseURL: 'http://127.0.0.1:4173',
    headless: true,
    viewport: { width: 1440, height: 1000 },
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
    launchOptions: { executablePath: process.env.ABTEST_BROWSER || '/usr/bin/google-chrome', args: ['--no-sandbox'] },
  },
  webServer: { command: 'npm run dev -- --host 127.0.0.1 --port 4173', url: 'http://127.0.0.1:4173', reuseExistingServer: !process.env.CI },
});
