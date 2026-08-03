// TopicDO — one Durable Object per bus topic.
//
// The topic IS the coordination atom: every operation on `fleet.supervisor.tick`
// is serialized against that topic and nothing else. Routing is
// `getByName(topic)`, so a topic name always lands on the same object with no
// registry to keep in sync. A single bus-wide DO would have been the obvious
// shape and is exactly the bottleneck the platform warns about — every publish on
// every topic queued behind one object.
//
// Storage is SQLite because both things we persist are relational-shaped: one
// retained row per topic, and an append-only stream we range-scan by id.
//
// Delivery classes, from the contract (supervisor/bus_contract.py):
//
//   telemetry — retained last-value with a TTL. Replaces `SET retain:<topic> EX n`.
//   event     — append to a capped stream. Replaces `XADD stream:<name>`.
//
// FAN-OUT: subscribers are server-side, which is what makes this cheap. The
// consumers are Discord webhook posters running in Cloudflare, so the DO calls
// them directly on publish — no WebSockets, no persistent mini→Cloudflare
// connection, no reconnect path to own on a host whose disk wedges.
//
// LIVENESS falls out of the same mechanism rather than needing a poller. A
// telemetry topic's retained value has a TTL; if a heartbeat stops arriving, the
// TTL alarm fires with nothing to refresh it, and *that* is the silence signal.
// A heartbeat that must be observed is therefore just a topic with a TTL and a
// subscriber — no cron, no separate liveness service.
import { DurableObject } from "cloudflare:workers";
import { deliver } from "./deliver.js";

// A stream is a catch-up and debugging buffer, not the log of record — Analytics
// Engine holds history. Capped so a runaway producer costs bounded storage.
const STREAM_CAP = 1000;

export class TopicDO extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    // Schema setup only — the one place blockConcurrencyWhile belongs.
    ctx.blockConcurrencyWhile(async () => {
      this.ctx.storage.sql.exec(`
        CREATE TABLE IF NOT EXISTS retained (
          topic      TEXT PRIMARY KEY,
          envelope   TEXT NOT NULL,
          expires_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS stream (
          id       INTEGER PRIMARY KEY AUTOINCREMENT,
          envelope TEXT NOT NULL,
          ts       INTEGER NOT NULL
        );
        -- Tracks whether we have already announced silence for this topic, so a
        -- late heartbeat produces exactly one recovery and a long outage does not
        -- re-announce on every alarm.
        CREATE TABLE IF NOT EXISTS liveness (
          topic     TEXT PRIMARY KEY,
          silent    INTEGER NOT NULL DEFAULT 0,
          since     INTEGER
        );
      `);
    });
  }

  /** Retained last-value write. `ttl` seconds, per the topic's CATALOG entry. */
  async publishRetained(topic, envelope, ttl) {
    const now = Date.now();
    const expiresAt = now + ttl * 1000;
    this.ctx.storage.sql.exec(
      `INSERT INTO retained (topic, envelope, expires_at, updated_at)
       VALUES (?, ?, ?, ?)
       ON CONFLICT(topic) DO UPDATE SET
         envelope = excluded.envelope,
         expires_at = excluded.expires_at,
         updated_at = excluded.updated_at`,
      topic,
      JSON.stringify(envelope),
      expiresAt,
      now,
    );
    // One alarm per DO, and setAlarm replaces any existing one — correct here:
    // the newest write owns the expiry, so a live heartbeat keeps pushing it out.
    await this.ctx.storage.setAlarm(expiresAt);

    // Recovery edge: we had announced silence and the producer is back.
    const wasSilent = this.#silentState(topic);
    if (wasSilent.silent) {
      this.#setSilent(topic, false, null);
      await deliver(this.env, topic, {
        kind: "recovered",
        topic,
        envelope,
        silentForSec: wasSilent.since
          ? Math.round((now - wasSilent.since) / 1000)
          : null,
      });
    }
    return { ok: true, expiresAt, recovered: wasSilent.silent };
  }

  /** Append to the capped stream, then fan out to subscribers. */
  async publishEvent(topic, envelope) {
    const now = Date.now();
    this.ctx.storage.sql.exec(
      "INSERT INTO stream (envelope, ts) VALUES (?, ?)",
      JSON.stringify(envelope),
      now,
    );
    const id = this.ctx.storage.sql
      .exec("SELECT last_insert_rowid() AS id")
      .one().id;
    // Trim by id, not by count, so concurrent appends cannot race the cap.
    this.ctx.storage.sql.exec("DELETE FROM stream WHERE id <= ?", id - STREAM_CAP);

    await deliver(this.env, topic, { kind: "event", topic, envelope, id });
    return { ok: true, id };
  }

  /** Retained read. Verifies expiry itself — an alarm can be late. */
  async retained() {
    const rows = this.ctx.storage.sql
      .exec("SELECT envelope, expires_at, updated_at FROM retained LIMIT 1")
      .toArray();
    if (!rows.length) return null;
    const row = rows[0];
    if (row.expires_at <= Date.now()) return null;
    return {
      envelope: JSON.parse(row.envelope),
      expiresAt: row.expires_at,
      ageSec: Math.round((Date.now() - row.updated_at) / 1000),
    };
  }

  /** Range-scan the stream. `since` is exclusive, matching XREAD semantics. */
  async readStream(since = 0, limit = 100) {
    const capped = Math.min(Math.max(1, limit), STREAM_CAP);
    const rows = this.ctx.storage.sql
      .exec(
        "SELECT id, envelope, ts FROM stream WHERE id > ? ORDER BY id LIMIT ?",
        since,
        capped,
      )
      .toArray();
    return rows.map((r) => ({ id: r.id, ts: r.ts, envelope: JSON.parse(r.envelope) }));
  }

  /** Health for the #ops digest. */
  async stat(topic) {
    const ret = await this.retained();
    const depth = this.ctx.storage.sql.exec("SELECT COUNT(*) AS n FROM stream").one().n;
    const live = this.#silentState(topic);
    return {
      retained: ret ? { ageSec: ret.ageSec, expiresAt: ret.expiresAt } : null,
      streamDepth: depth,
      silent: Boolean(live.silent),
      silentSince: live.since,
    };
  }

  /**
   * TTL expiry — and the liveness signal.
   *
   * Reaching this alarm with an expired retained value means no publish arrived
   * in time to push it out. For a heartbeat topic that is precisely "the producer
   * has gone quiet", so the expiry and the alarm are the same event and there is
   * nothing to poll.
   */
  async alarm() {
    const now = Date.now();
    const expired = this.ctx.storage.sql
      .exec("SELECT topic, updated_at FROM retained WHERE expires_at <= ?", now)
      .toArray();
    this.ctx.storage.sql.exec("DELETE FROM retained WHERE expires_at <= ?", now);

    for (const row of expired) {
      // Announce once per outage, not once per alarm.
      if (this.#silentState(row.topic).silent) continue;
      this.#setSilent(row.topic, true, now);
      await deliver(this.env, row.topic, {
        kind: "silent",
        topic: row.topic,
        lastSeenSec: Math.round((now - row.updated_at) / 1000),
      });
    }
  }

  #silentState(topic) {
    const rows = this.ctx.storage.sql
      .exec("SELECT silent, since FROM liveness WHERE topic = ?", topic)
      .toArray();
    return rows.length ? rows[0] : { silent: 0, since: null };
  }

  #setSilent(topic, silent, since) {
    this.ctx.storage.sql.exec(
      `INSERT INTO liveness (topic, silent, since) VALUES (?, ?, ?)
       ON CONFLICT(topic) DO UPDATE SET silent = excluded.silent, since = excluded.since`,
      topic,
      silent ? 1 : 0,
      since,
    );
  }
}
