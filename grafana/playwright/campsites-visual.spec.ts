import { test, expect } from '@playwright/test';
import fs from 'node:fs';

// Visual regression for the campsites dashboards — the canvas-level guard that
// the pytest suite (data layer) and DOM assertions can't see: blank panels,
// collapsed axes, a panel that silently renders "No data".
//
// Window: RELATIVE (now-7d). The campsites data is live (collector writes
// continuously, availability daily), so unlike the tempest baselines there's no
// immutable past window. The trade-off: the screenshot baselines drift with the
// data and need a periodic refresh —
//     npx playwright test campsites-visual.spec.ts --update-snapshots
// So the *real* regression guard here is the structural pair (canvas mounted +
// no "No data"), which is drift-proof; the screenshot runs at a loose threshold
// to still catch gross layout/colour/blank breaks without flapping on data wiggle.
//
// Local-only and machine-specific (not run in CI) — needs the live stack.

const ENV_FILE = process.env.GRAFANA_ENV_FILE ?? '/Volumes/dev/observability/grafana/.env';
const BASE_URL = process.env.GRAFANA_URL ?? 'http://localhost:3001';
const FROM = 'now-7d';
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

// var-* params pin the dashboard variables so the render is reproducible.
const AVAIL_VARS = 'var-target_date=2026-07-04&var-agency=$__all&var-campsite=$__all';

const PANELS = [
  // collector-history — the dynamic-range fix
  { dash: 'campsite-collector-history', id: 2, name: 'collector-collections' },
  { dash: 'campsite-collector-history', id: 3, name: 'collector-fleet-coverage' },
  { dash: 'campsite-collector-history', id: 21, name: 'collector-rate' },
  // campsite-availability (merged) — history section, trends need a pinned date
  { dash: 'campsite-availability', id: 15, name: 'availability-avail-vs-booked', vars: AVAIL_VARS },
  { dash: 'campsite-availability', id: 16, name: 'availability-booked-by-agency', vars: AVAIL_VARS },
  // campsite-availability (merged) — live section, categorical agency barchart
  { dash: 'campsite-availability', id: 6, name: 'live-site-nights-by-agency' },
];

test.describe('Campsites visual regression (relative now-7d)', () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  for (const p of PANELS) {
    test(`${p.name} renders (not blank / no No-data)`, async ({ page }) => {
      const vars = p.vars ? `&${p.vars}` : '';
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
