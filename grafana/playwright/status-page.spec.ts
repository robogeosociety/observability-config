import { test, expect } from '@playwright/test';
import fs from 'node:fs';

const ENV_FILE = process.env.GRAFANA_ENV_FILE ?? '/Volumes/dev/grafana/.env';
const BASE_URL = process.env.GRAFANA_URL ?? 'http://localhost:3001';
const DEV_STATUS_URL = process.env.DEV_STATUS_URL ?? 'http://localhost:8077';

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
const AUTH = 'Basic ' + Buffer.from(`${USER}:${PASS}`).toString('base64');

test.describe('Status page + dev-status collector', () => {
  test('dev-status collector serves the contract the Infinity panels depend on', async ({ request }) => {
    const r = await request.get(`${DEV_STATUS_URL}/dev-status.json`);
    expect(r.ok()).toBeTruthy();
    expect(r.headers()['content-type']).toContain('application/json');
    const d = await r.json();
    for (const k of ['generated_at', 'total', 'up', 'down', 'deployments']) {
      expect(d, `missing key ${k}`).toHaveProperty(k);
    }
    expect(Array.isArray(d.deployments)).toBeTruthy();
    expect(d.total).toBe(d.deployments.length);
    expect(d.up + d.down).toBe(d.total);
    for (const row of d.deployments) {
      for (const k of ['name', 'url', 'port', 'up', 'source', 'latency_ms']) {
        expect(row).toHaveProperty(k);
      }
    }
  });

  test('status-page dashboard is provisioned with the dev-deployments panels', async ({ request }) => {
    const r = await request.get(`${BASE_URL}/api/dashboards/uid/status-page`, {
      headers: { Authorization: AUTH },
    });
    expect(r.ok()).toBeTruthy();
    const body = await r.json();
    expect(body.meta.provisioned).toBe(true); // UI lock in effect
    const d = body.dashboard;
    expect(d.title).toBe('Project Status');
    const titles: string[] = d.panels.map((p: { title: string }) => p.title);
    for (const expected of [
      'Dev Deployments & Tailscale Serves (this machine)',
      'Deployments — live summary',
      'Routes & backends',
    ]) {
      expect(titles).toContain(expected);
    }
  });

  test('status page renders in the browser with the routes table populated', async ({ page }) => {
    // Log in
    await page.goto(`${BASE_URL}/login`);
    await page.locator('input[name="user"]').fill(USER);
    await page.locator('input[name="password"]').fill(PASS);
    await Promise.all([
      page.waitForURL((url) => !url.pathname.includes('/login'), { timeout: 15_000 }),
      page.locator('button[type="submit"]').click(),
    ]);

    // Open the status page
    await page.goto(`${BASE_URL}/d/status-page?orgId=1`, { waitUntil: 'domcontentloaded' });
    await expect(page).toHaveTitle(/Project Status/i, { timeout: 15_000 });

    // The Routes & backends table is below the fold and Grafana lazy-renders
    // panels only once their place in the scroll container comes into view.
    // Scroll the dashboard's scroll container to the bottom in steps until the
    // collector-fed row shows up.
    const row = page.getByText('realitycapture', { exact: true }).first();
    await page.mouse.move(640, 400); // put the pointer over the dashboard content
    for (let i = 0; i < 25 && !(await row.count()); i++) {
      await page.mouse.wheel(0, 1500); // real wheel events scroll Grafana's canvas
      await page.waitForTimeout(400);
    }

    // No panel-level error states anywhere. (We do NOT assert absence of "No data" —
    // dev backends are legitimately down and freshness panels may be empty.)
    const panelErrors = page.locator('[data-testid="data-testid Panel status error"]');
    expect(await panelErrors.count()).toBe(0);

    // The table pulled live rows from the collector. realitycapture is a stable,
    // registered serve, so its row should be present and visible.
    await expect(row).toBeVisible({ timeout: 15_000 });

    await page.screenshot({ path: 'status-page.png', fullPage: true });
  });
});
