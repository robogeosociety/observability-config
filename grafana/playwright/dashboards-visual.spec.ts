import { test, expect } from '@playwright/test';
import fs from 'node:fs';

// Visual regression for the continuously-fed dashboards (#67) — the repo-wide
// companion to campsites-visual.spec.ts. The real, drift-proof guard is the
// structural pair (canvas mounted + no "No data"); the screenshot runs at a loose
// threshold to still catch gross layout/colour/blank breaks without flapping on
// live-data wiggle.
//
// Add a dashboard by adding PANELS rows, not a new spec. Only list panels that
// render *reliably* with data — event-driven / weather-dependent panels (the
// dashboard_coverage.yaml allow-empty set) are deliberately NOT baselined here;
// they would bake a "No data" frame into the baseline. Cover those at the
// integration tier instead.
//
// Local-only and machine-specific (not run in CI) — needs the live stack.
// Refresh baselines after an intentional change:
//   npx playwright test dashboards-visual.spec.ts --update-snapshots

const ENV_FILE = process.env.GRAFANA_ENV_FILE ?? '/Volumes/dev/observability/grafana/.env';
const BASE_URL = process.env.GRAFANA_URL ?? 'http://localhost:3001';
const FROM = 'now-6h';
const TO = 'now';

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
  // infra — host metrics, continuously written
  { dash: 'mac-system', id: 5, name: 'mac-cpu-utilisation' },
  { dash: 'mac-system', id: 14, name: 'mac-memory-breakdown' },
  { dash: 'ha-pi-system', id: 5, name: 'hapi-cpu-use' },
  { dash: 'ha-pi-system', id: 14, name: 'hapi-memory' },
  { dash: 'orbstack-containers', id: 10, name: 'orbstack-cpu-per-container' },
  { dash: 'orbstack-containers', id: 11, name: 'orbstack-mem-per-container' },
  // weather — Tempest, continuously written
  { dash: 'tempest-basic', id: 5, name: 'tempest-temp-humidity' },
  { dash: 'tempest-basic', id: 16, name: 'tempest-pressure' },
];

test.describe('Dashboards visual regression (relative now-6h)', () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  for (const p of PANELS) {
    test(`${p.name} renders (not blank / no No-data)`, async ({ page }) => {
      const vars = (p as any).vars ? `&${(p as any).vars}` : '';
      await page.goto(
        `${BASE_URL}/d-solo/${p.dash}/x?panelId=${p.id}&from=${FROM}&to=${TO}&orgId=1${vars}`,
        { waitUntil: 'domcontentloaded' },
      );
      await page.waitForLoadState('networkidle', { timeout: 30_000 });

      // structural guard (drift-proof): the viz body mounted, and the panel is
      // not in the "No data" empty state.
      await expect(page.locator('canvas')).toHaveCount(1, { timeout: 15_000 });
      await expect(page.getByText('No data', { exact: true })).toHaveCount(0);

      await page.waitForTimeout(1500); // settle final draw
      // loose threshold: tolerate live-data wiggle, still catch gross breaks.
      await expect(page).toHaveScreenshot(`${p.name}.png`, { maxDiffPixelRatio: 0.25 });
    });
  }
});
