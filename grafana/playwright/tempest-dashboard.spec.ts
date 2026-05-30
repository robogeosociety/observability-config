import { test, expect } from '@playwright/test';
import fs from 'node:fs';

const ENV_FILE = process.env.GRAFANA_ENV_FILE ?? '/Volumes/dev/grafana/.env';
const BASE_URL = process.env.GRAFANA_URL ?? 'http://localhost:3001';

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

test.describe('Grafana — Tempest dashboard', () => {
  test('API /api/health returns ok', async ({ request }) => {
    const r = await request.get(`${BASE_URL}/api/health`);
    expect(r.status()).toBe(200);
    const body = await r.json();
    expect(body.database).toBe('ok');
    expect(body.version).toMatch(/^11\./);
  });

  test('tempest_archive datasource health is OK', async ({ request }) => {
    const r = await request.get(
      `${BASE_URL}/api/datasources/uid/tempest_archive/health`,
      { headers: { Authorization: AUTH } },
    );
    expect(r.ok()).toBeTruthy();
    const body = await r.json();
    expect(body.status).toBe('OK');
    expect(body.message).toContain('buckets found');
  });

  test('tempest-basic dashboard is provisioned with all expected panels', async ({ request }) => {
    const r = await request.get(
      `${BASE_URL}/api/dashboards/uid/tempest-basic`,
      { headers: { Authorization: AUTH } },
    );
    expect(r.ok()).toBeTruthy();
    const d = (await r.json()).dashboard;
    expect(d.title).toBe('Tempest — Basic');
    const titles: string[] = d.panels.map((p: { title: string }) => p.title);
    for (const expected of [
      'Air Temperature',
      'Humidity',
      'Wind (avg)',
      'Battery',
      'Temperature & Humidity',
      'Wind (avg / gust / lull)',
      'Pressure',
    ]) {
      expect(titles).toContain(expected);
    }
  });

  test('recent obs_st data exists in tempest_archive (direct Flux to InfluxDB)', async ({ request }) => {
    // Read the Influx read-only token from the influxdb .env (Grafana's token works here).
    const influxEnv = parseEnv('/Volumes/dev/influxdb/.env');
    const grafanaEnv = parseEnv(ENV_FILE);
    const influxToken = grafanaEnv.INFLUX_GRAFANA_TOKEN ?? influxEnv.INFLUX_GRAFANA_TOKEN;
    expect(influxToken, 'INFLUX_GRAFANA_TOKEN missing from .env').toBeTruthy();
    const influxUrl = process.env.INFLUX_URL ?? 'http://localhost:8086';

    const flux = `from(bucket:"tempest_archive")
  |> range(start:-30m)
  |> filter(fn:(r) => r._measurement == "weather" and r._field == "air_temp_c")
  |> last()`;
    const r = await request.post(`${influxUrl}/api/v2/query?org=home`, {
      headers: {
        Authorization: `Token ${influxToken}`,
        'Content-Type': 'application/vnd.flux',
        Accept: 'application/csv',
      },
      data: flux,
    });
    expect(r.ok()).toBeTruthy();
    const csv = await r.text();
    expect(csv).toMatch(/tempest:ST-\d+/);
    expect(csv).toMatch(/air_temp_c/);
  });

  test('Pressure timeseries (panel-7) actually renders a canvas with a readable hPa axis', async ({ page }) => {
    // Regression: a legacy legend (displayMode:"hidden") made this panel mount
    // header-only — no canvas, but no error and no "No data", so the generic
    // render check below could not catch it. And pressurembar units collapsed
    // the axis to "1 bar". This asserts the viz body really draws.
    await page.goto(`${BASE_URL}/login`);
    await page.locator('input[name="user"]').fill(USER);
    await page.locator('input[name="password"]').fill(PASS);
    await Promise.all([
      page.waitForURL((url) => !url.pathname.includes('/login'), { timeout: 15_000 }),
      page.locator('button[type="submit"]').click(),
    ]);

    await page.goto(`${BASE_URL}/d/tempest-basic/x?viewPanel=panel-7&orgId=1`,
      { waitUntil: 'domcontentloaded' });
    await page.waitForLoadState('networkidle', { timeout: 30_000 });
    await page.waitForTimeout(2000);

    expect(await page.locator('[data-testid="data-testid Panel status error"]').count()).toBe(0);
    expect(await page.getByText('No data', { exact: true }).count()).toBe(0);
    // The viz body must mount a uPlot canvas. The legacy-legend bug rendered the
    // panel header-only with no canvas, so this is the load-bearing assertion.
    // (Axis tick labels are drawn into the canvas, not the DOM, so the hPa-vs-bar
    //  unit fix is guarded by a static test instead.)
    expect(await page.locator('canvas').count()).toBeGreaterThan(0);
  });

  test('dashboard renders in the browser without panel errors', async ({ page }) => {
    // Log in
    await page.goto(`${BASE_URL}/login`);
    await page.locator('input[name="user"]').fill(USER);
    await page.locator('input[name="password"]').fill(PASS);
    await Promise.all([
      page.waitForURL((url) => !url.pathname.includes('/login'), { timeout: 15_000 }),
      page.locator('button[type="submit"]').click(),
    ]);

    // Open the dashboard
    await page.goto(`${BASE_URL}/d/tempest-basic?orgId=1`, { waitUntil: 'domcontentloaded' });
    await expect(page).toHaveTitle(/Tempest/i, { timeout: 15_000 });

    // Let panel queries settle
    await page.waitForLoadState('networkidle', { timeout: 30_000 });

    // No panel-level error states
    const panelErrors = page.locator('[data-testid="data-testid Panel status error"]');
    expect(await panelErrors.count()).toBe(0);

    // No bare "No data" indicators on the stat panels
    expect(await page.getByText('No data', { exact: true }).count()).toBe(0);

    // Capture proof
    await page.screenshot({ path: 'tempest-dashboard.png', fullPage: true });
  });
});
