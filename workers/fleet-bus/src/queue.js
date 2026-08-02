// QueueDO — durable work queue for the `work` delivery class.
//
// The other two classes are drop-safe by contract: telemetry keeps a retained
// last-value, events append to a capped stream, and losing either costs nothing
// because a fresher one is along shortly. Work is not like that. A GitHub
// notification that should become a summary has no successor — drop it and the
// summary never exists — so this class is at-least-once with explicit acks and a
// durable backlog.
//
// The reason it is its own class rather than a flag on the stream: the failure it
// has to survive is a TOKEN QUOTA OUTAGE, which can last hours. A capped stream
// would quietly discard the backlog exactly when the backlog matters, and naive
// per-item retry would burn the whole queue against a quota that is still
// exhausted, converting one outage into N terminal failures.
//
// So the queue distinguishes three outcomes, and only one of them counts against
// an item:
//
//   ack       2xx           done, delete it
//   parked    429 / quota   NOT the item's fault — park the WHOLE queue until
//                           `parked_until`, leave attempts untouched, resume
//                           mid-backlog afterwards
//   failed    4xx           terminal, dead-letter after MAX_ATTEMPTS
//             5xx           transient, retry with exponential backoff
//
// Parking is queue-wide on purpose. Quota is a shared resource: if item 1 was
// rejected for quota, items 2..N will be too, and trying them just deepens the
// hole while spending the retry budget of items that were never broken.
import { DurableObject } from "cloudflare:workers";

const MAX_ATTEMPTS = 5;
const BATCH = 10; // items drained per alarm — bounded so one alarm cannot run long
const BASE_BACKOFF_MS = 30_000;
const QUOTA_COOLDOWN_MS = 15 * 60_000; // how long to park before probing again

export class QueueDO extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    ctx.blockConcurrencyWhile(async () => {
      this.ctx.storage.sql.exec(`
        CREATE TABLE IF NOT EXISTS jobs (
          id        INTEGER PRIMARY KEY AUTOINCREMENT,
          envelope  TEXT NOT NULL,
          attempts  INTEGER NOT NULL DEFAULT 0,
          not_before INTEGER NOT NULL DEFAULT 0,
          created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS dead (
          id        INTEGER PRIMARY KEY,
          envelope  TEXT NOT NULL,
          reason    TEXT NOT NULL,
          failed_at INTEGER NOT NULL
        );
        -- Single-row queue state. parked_until is the whole-queue quota gate.
        CREATE TABLE IF NOT EXISTS qstate (
          k            TEXT PRIMARY KEY,
          parked_until INTEGER NOT NULL DEFAULT 0,
          parked_reason TEXT
        );
      `);
    });
  }

  /** Durable enqueue. Returns immediately; draining happens on the alarm. */
  async enqueue(envelope) {
    const now = Date.now();
    this.ctx.storage.sql.exec(
      "INSERT INTO jobs (envelope, created_at) VALUES (?, ?)",
      JSON.stringify(envelope),
      now,
    );
    const id = this.ctx.storage.sql.exec("SELECT last_insert_rowid() AS id").one().id;
    await this.#ensureAlarm(now);
    return { ok: true, id, depth: this.#depth() };
  }

  /** Queue health for the #ops digest. */
  async stat() {
    const parked = this.#parked();
    const oldest = this.ctx.storage.sql
      .exec("SELECT created_at FROM jobs ORDER BY id LIMIT 1")
      .toArray();
    return {
      depth: this.#depth(),
      dead: this.ctx.storage.sql.exec("SELECT COUNT(*) AS n FROM dead").one().n,
      parkedUntil: parked.until > Date.now() ? parked.until : null,
      parkedReason: parked.until > Date.now() ? parked.reason : null,
      oldestAgeSec: oldest.length ? Math.round((Date.now() - oldest[0].created_at) / 1000) : null,
    };
  }

  /** Drain a bounded batch. Re-arms itself while work remains. */
  async alarm() {
    const now = Date.now();
    const parked = this.#parked();
    if (parked.until > now) {
      // Still inside the quota cooldown — come back, do not touch the backlog.
      await this.ctx.storage.setAlarm(parked.until);
      return;
    }

    const rows = this.ctx.storage.sql
      .exec(
        "SELECT id, envelope, attempts FROM jobs WHERE not_before <= ? ORDER BY id LIMIT ?",
        now,
        BATCH,
      )
      .toArray();

    for (const row of rows) {
      const outcome = await this.#process(JSON.parse(row.envelope));

      if (outcome.kind === "ack") {
        this.ctx.storage.sql.exec("DELETE FROM jobs WHERE id = ?", row.id);
        continue;
      }

      if (outcome.kind === "parked") {
        // Whole-queue stop. Crucially: attempts is NOT incremented — the item did
        // nothing wrong, and charging it here would dead-letter healthy work for
        // the duration of an outage.
        const until = Date.now() + (outcome.retryAfterMs || QUOTA_COOLDOWN_MS);
        this.#setParked(until, outcome.reason || "quota exhausted");
        await this.ctx.storage.setAlarm(until);
        return;
      }

      const attempts = row.attempts + 1;
      if (outcome.terminal || attempts >= MAX_ATTEMPTS) {
        this.ctx.storage.sql.exec(
          "INSERT OR REPLACE INTO dead (id, envelope, reason, failed_at) VALUES (?, ?, ?, ?)",
          row.id,
          row.envelope,
          outcome.reason || "max attempts",
          Date.now(),
        );
        this.ctx.storage.sql.exec("DELETE FROM jobs WHERE id = ?", row.id);
      } else {
        // Exponential backoff, per item.
        const delay = BASE_BACKOFF_MS * 2 ** (attempts - 1);
        this.ctx.storage.sql.exec(
          "UPDATE jobs SET attempts = ?, not_before = ? WHERE id = ?",
          attempts,
          Date.now() + delay,
          row.id,
        );
      }
    }

    await this.#ensureAlarm(Date.now(), true);
  }

  /**
   * Hand one job to its configured handler and classify the response.
   *
   * The queue stays generic — it does not know what a summary is. The handler URL
   * comes from env (`HANDLER_<ROUTE>`), so the same machinery serves any work
   * topic. The classification is the part that matters, and it is deliberately
   * explicit about quota: a 429, or an explicit `{parked:true}`, parks the queue.
   */
  async #process(envelope) {
    const route = envelope?.data?.handler || "default";
    const url = this.env[`HANDLER_${route.toUpperCase()}`];
    if (!url) return { kind: "failed", terminal: true, reason: `no handler for route ${route}` };

    let res;
    try {
      res = await fetch(url, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          ...(this.env.HANDLER_TOKEN ? { authorization: `Bearer ${this.env.HANDLER_TOKEN}` } : {}),
        },
        body: JSON.stringify(envelope),
      });
    } catch (e) {
      return { kind: "failed", terminal: false, reason: `fetch failed: ${e}` };
    }

    if (res.status === 429) {
      // Honour Retry-After when the upstream tells us; it knows the window and we
      // are guessing otherwise.
      const ra = Number(res.headers.get("retry-after"));
      return {
        kind: "parked",
        reason: "429 from handler",
        retryAfterMs: Number.isFinite(ra) && ra > 0 ? ra * 1000 : undefined,
      };
    }

    if (res.ok) {
      // A handler that is up but out of quota can say so without a 429.
      let body = null;
      try {
        body = await res.json();
      } catch {
        /* handler need not return JSON */
      }
      if (body && body.parked) {
        return { kind: "parked", reason: body.reason || "handler reported quota", retryAfterMs: body.retryAfterMs };
      }
      return { kind: "ack" };
    }

    // 4xx is the job's fault and will not improve on retry; 5xx might.
    return {
      kind: "failed",
      terminal: res.status >= 400 && res.status < 500,
      reason: `handler ${res.status}`,
    };
  }

  #depth() {
    return this.ctx.storage.sql.exec("SELECT COUNT(*) AS n FROM jobs").one().n;
  }

  #parked() {
    const rows = this.ctx.storage.sql
      .exec("SELECT parked_until, parked_reason FROM qstate WHERE k = 'q'")
      .toArray();
    return rows.length
      ? { until: rows[0].parked_until, reason: rows[0].parked_reason }
      : { until: 0, reason: null };
  }

  #setParked(until, reason) {
    this.ctx.storage.sql.exec(
      `INSERT INTO qstate (k, parked_until, parked_reason) VALUES ('q', ?, ?)
       ON CONFLICT(k) DO UPDATE SET parked_until = excluded.parked_until,
                                    parked_reason = excluded.parked_reason`,
      until,
      reason,
    );
  }

  /** Arm the alarm for the next due job, if any. */
  async #ensureAlarm(now, force = false) {
    const next = this.ctx.storage.sql
      .exec("SELECT MIN(not_before) AS t FROM jobs")
      .toArray();
    if (!next.length || next[0].t == null) return; // queue empty — no alarm
    const at = Math.max(now, next[0].t);
    const existing = await this.ctx.storage.getAlarm();
    if (force || existing == null || at < existing) await this.ctx.storage.setAlarm(at);
  }
}
