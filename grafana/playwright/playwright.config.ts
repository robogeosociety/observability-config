import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: '.',
  fullyParallel: false,
  timeout: 60_000,
  expect: {
    timeout: 10_000,
    // Visual regression defaults: tolerate sub-pixel AA noise, freeze animations.
    toHaveScreenshot: { maxDiffPixelRatio: 0.02, animations: 'disabled', caret: 'hide' },
  },
  reporter: [['list']],
  use: {
    baseURL: process.env.GRAFANA_URL ?? 'http://localhost:3001',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    trace: 'on-first-retry',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
  ],
});
