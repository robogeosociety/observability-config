// Shape test for the host-vitals ingest Worker, run with `node --test`.
//
// sample-batch.ndjson is NOT hand-written: the metric + weather lines were
// captured from a live vector 0.57.0 http sink (host_metrics with cpu, memory,
// disk, filesystem, load, network on macOS arm64 + a real WeatherFlow Tempest
// broadcasting on LAN UDP 50222), decompressed verbatim. The hub_status line
// is a real UDP capture wrapped as the codec would ship it — it tests the
// skip path (the vector-side filter normally drops it; the Worker must too).
//
// Exercises the whole fetch path in Node (>=18 has Request/Response/
// DecompressionStream/crypto.subtle): auth, gzip body, NDJSON parse, and the
// exact Analytics Engine data-point shapes.

import assert from "node:assert/strict";
import { test } from "node:test";
import { readFileSync } from "node:fs";
import { gzipSync } from "node:zlib";
import worker, { collapseTags, metricPoint, parseBatch } from "../src/index.js";

const SAMPLE = readFileSync(new URL("./sample-batch.ndjson", import.meta.url), "utf8");
const KEY = "testkey-not-a-secret";

function envStub() {
  const vitals = [], weather = [];
  return {
    env: {
      VITALS_INGEST_KEY: KEY,
      VITALS: { writeDataPoint: (p) => vitals.push(p) },
      WEATHER: { writeDataPoint: (p) => weather.push(p) },
    },
    vitals,
    weather,
  };
}

const post = (body, headers = {}) =>
  new Request("https://host-vitals.example.workers.dev/ingest", {
    method: "POST",
    headers: { authorization: `Bearer ${KEY}`, ...headers },
    body,
  });

test("gzip NDJSON batch → correct AE points, wire-faithful", async () => {
  const { env, vitals, weather } = envStub();
  const res = await worker.fetch(
    post(gzipSync(SAMPLE), { "content-encoding": "gzip" }),
    env,
  );
  assert.equal(res.status, 200);
  const body = await res.json();
  // 6 metric lines, one null-gauge (autofs used_ratio) skipped → 5 vitals.
  // obs_st (1 row) + rapid_wind → 2 weather. hub_status skipped.
  assert.deepEqual(body.written, { host_vitals: 5, weather_obs: 2 });
  assert.equal(body.skipped, 2);
  assert.equal(vitals.length, 5);
  assert.equal(weather.length, 2);

  // host_vitals column map
  const cpu = vitals.find((p) => p.blobs[1] === "cpu_seconds_total");
  assert.ok(cpu);
  assert.equal(cpu.blobs[0], "MacBook-Air.local"); // blob1 host
  assert.equal(cpu.blobs[2], "cpu=0,mode=idle"); // blob3 tags collapsed
  assert.equal(cpu.blobs[3], "cpu"); // blob4 collector
  assert.equal(typeof cpu.doubles[0], "number"); // double1 value
  assert.ok(cpu.doubles[1] > 1780000000); // double2 source epoch secs
  assert.deepEqual(cpu.indexes, ["MacBook-Air.local"]);

  // weather_obs column map — obs_st: 18 doubles, [0] = epoch
  const obs = weather.find((p) => p.blobs[0] === "obs_st");
  assert.ok(obs);
  assert.equal(obs.blobs[1], "ST-00204728");
  assert.equal(obs.doubles.length, 18);
  assert.ok(obs.doubles[0] > 1780000000);
  assert.deepEqual(JSON.parse(obs.blobs[2]).length, 18); // raw row survives
  assert.deepEqual(obs.indexes, ["ST-00204728"]);

  // rapid_wind: ob = [epoch, m/s, deg]
  const rw = weather.find((p) => p.blobs[0] === "rapid_wind");
  assert.ok(rw);
  assert.equal(rw.doubles.length, 3);
});

test("uncompressed body also accepted", async () => {
  const { env } = envStub();
  const res = await worker.fetch(post(SAMPLE), env);
  assert.equal(res.status, 200);
});

test("bad auth → 401, nothing written", async () => {
  const { env, vitals, weather } = envStub();
  for (const headers of [
    { authorization: "Bearer wrong" },
    { authorization: "" },
    {},
  ]) {
    const req = new Request("https://x.example/ingest", { method: "POST", headers, body: SAMPLE });
    assert.equal((await worker.fetch(req, env)).status, 401);
  }
  assert.equal(vitals.length + weather.length, 0);
});

test("malformed batch → 400", async () => {
  const { env } = envStub();
  const res = await worker.fetch(post('{"metric": {truncated'), env);
  assert.equal(res.status, 400);
});

test("GET /health and GET /ingest (sink healthcheck) → 200, no auth", async () => {
  const { env } = envStub();
  for (const path of ["/health", "/ingest"]) {
    const res = await worker.fetch(new Request(`https://x.example${path}`), env);
    assert.equal(res.status, 200);
  }
});

test("unit: tag collapse + null gauge rejection", () => {
  assert.equal(
    collapseTags({ host: "h", collector: "cpu", mode: "idle", cpu: "0" }),
    "cpu=0,mode=idle",
  );
  assert.equal(metricPoint({ name: "x", tags: {}, gauge: { value: null } }), null);
  const { skipped } = parseBatch('{"neither":1}\n');
  assert.equal(skipped, 1);
});
