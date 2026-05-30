import { test, expect } from '@playwright/test';
import fs from 'node:fs';

// Visual regression for dashboard panels. These guard the *canvas-drawn* render
// that DOM assertions can't see — blank panels, collapsed axes, unreadable
// stats, colour/contrast regressions.
//
// Determinism: we render each panel with d-solo (just the panel, no nav chrome)
// over a FIXED PAST time range, so the InfluxDB data is immutable and the image
// is stable run-to-run. Baselines live in visual.spec.ts-snapshots/ and are
// committed. Regenerate after an intentional change with:
//     npx playwright test visual.spec.ts --update-snapshots
//
// Local-only and machine-specific (not run in CI) — the e2e tier already
// requires the live stack.

const ENV_FILE = process.env.GRAFANA_ENV_FILE ?? '/Volumes/dev/observability/grafana/.env';
const BASE_URL = process.env.GRAFANA_URL ?? 'http://localhost:3001';

// 2026-05-30 13:37:22Z .. 16:37:22Z (epoch ms) — a fixed window with real data.
const FROM = '1780148242000';
const TO = '1780159042000';

function parseEnv(p: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of fs.readFileSync(p, 'utf-8').split('\n')) {
    const t = line.trim();
    if (!t || t.startsWith('#')) continue;
    const i = t.indexOf('=');
    if (i < 0) continue;
    out[t.slice(0, i)] = t.slice(i + 1);
  }
  return out;
}
const env = parseEnv(ENV_FILE);
const USER = env.GRAFANA_ADMIN_USER;
const PASS = env.GRAFANA_ADMIN_PASSWORD;

async function login(page: import('@playwright/test').Page) {
  await page.goto(`${BASE_URL}/login`);
  await page.locator('input[name="user"]').fill(USER);
  await page.locator('input[name="password"]').fill(PASS);
  await Promise.all([
    page.waitForURL((url) => !url.pathname.includes('/login'), { timeout: 15_000 }),
    page.locator('button[type="submit"]').click(),
  ]);
}

const PANELS = [
  { dash: 'tempest-basic', id: 5, name: 'tempest-temp-humidity' },
  { dash: 'tempest-basic', id: 6, name: 'tempest-wind' },
  { dash: 'tempest-basic', id: 7, name: 'tempest-pressure' },
];

test.describe('Visual regression (fixed past range)', () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  for (const p of PANELS) {
    test(`${p.name} matches baseline`, async ({ page }) => {
      await page.goto(
        `${BASE_URL}/d-solo/${p.dash}/x?panelId=${p.id}&from=${FROM}&to=${TO}&orgId=1`,
        { waitUntil: 'domcontentloaded' },
      );
      await page.waitForLoadState('networkidle', { timeout: 30_000 });
      // sanity: the viz body mounted (not a header-only/blank render)
      await expect(page.locator('canvas')).toHaveCount(1, { timeout: 15_000 });
      await page.waitForTimeout(1500); // settle final draw
      await expect(page).toHaveScreenshot(`${p.name}.png`);
    });
  }
});
