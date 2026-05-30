import { test, expect } from '@playwright/test';
import fs from 'node:fs';

const ENV_FILE = process.env.GRAFANA_ENV_FILE ?? '/Volumes/dev/observability/grafana/.env';
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

  test('live-summary stat (panel-12) shows readable numbers matching the collector', async ({ page, request }) => {
    // ground truth from the collector
    const d = await (await request.get(`${DEV_STATUS_URL}/dev-status.json`)).json();
    const expected = { up: d.up, down: d.down, total: d.total };

    // log in
    await page.goto(`${BASE_URL}/login`);
    await page.locator('input[name="user"]').fill(USER);
    await page.locator('input[name="password"]').fill(PASS);
    await Promise.all([
      page.waitForURL((url) => !url.pathname.includes('/login'), { timeout: 15_000 }),
      page.locator('button[type="submit"]').click(),
    ]);

    // open the single-panel view used in the report
    await page.goto(`${BASE_URL}/d/status-page/project-status?viewPanel=panel-12&orgId=1`,
      { waitUntil: 'domcontentloaded' });
    await page.waitForLoadState('networkidle', { timeout: 30_000 });

    // For each of up/down/total, the value must render as a real number AND be
    // large enough to read — guards the two regressions we hit:
    //   (a) values:true → dozens of ~1.5px tiles, and
    //   (b) wrong root_selector → null/0 values.
    for (const [label, value] of Object.entries(expected)) {
      const found = await page.evaluate((want) => {
        let maxSize = 0;
        for (const el of Array.from(document.querySelectorAll('div,span,a'))) {
          if (el.children.length) continue;             // leaf nodes only
          if ((el.textContent || '').trim() !== String(want)) continue;
          maxSize = Math.max(maxSize, parseFloat(getComputedStyle(el).fontSize) || 0);
        }
        return maxSize;
      }, value);
      expect(found, `value for "${label}" (${value}) not rendered`).toBeGreaterThan(0);
      expect(found, `value for "${label}" is too small to read (${found}px)`).toBeGreaterThan(20);
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
    // panels only once scrolled into view. Scroll gently until the panel title
    // is visible, then STOP — over-scrolling pushes into the table's own
    // internal scroll and virtualizes the rows back out.
    const title = page.getByText('Routes & backends', { exact: true });
    await page.mouse.move(640, 400);
    for (let i = 0; i < 30 && !(await title.isVisible().catch(() => false)); i++) {
      await page.mouse.wheel(0, 600);
      await page.waitForTimeout(250);
    }
    await expect(title).toBeVisible({ timeout: 10_000 });
    await page.waitForTimeout(1500); // let the table render its rows

    // No panel-level error states on the dev-deployments panels.
    const panelErrors = page.locator('[data-testid="data-testid Panel status error"]');
    expect(await panelErrors.count()).toBe(0);

    // The table is populated from the collector: every row has a Source cell of
    // "serve" or "registry", so at least one is visible regardless of sort/scroll.
    await expect(page.getByText(/^(serve|registry)$/).first())
      .toBeVisible({ timeout: 15_000 });
    // and the routes carry the tailnet host (serve rows) — proves real data, not a shell.
    await expect(page.getByText(/tail59a169\.ts\.net/).first())
      .toBeVisible({ timeout: 15_000 });

    await page.screenshot({ path: 'status-page.png', fullPage: true });
  });
});
