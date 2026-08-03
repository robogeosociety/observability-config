// dispatch — hand a summary job to GitHub Actions.
//
// THE MODEL DOES NOT RUN HERE, and that is deliberate rather than incidental.
// Claude work in this fleet runs under subscription auth, which lives in the
// Claude CLI; a Worker cannot run the CLI. The settled shape is "Actions owns the
// model and the words, the Worker owns the Discord identity". Calling the
// Anthropic API from here instead would be a second, metered path to the same
// answers — which is exactly what that decision exists to avoid, and what an
// earlier version of this Worker got wrong (it reached the API fine and then
// failed on an empty credit balance, because the subscription is not the API).
//
// So this Worker's job in the summary lane is one HTTP call: fire a
// `repository_dispatch` and report the job as LEASED, so fleet-bus keeps it until
// the workflow acks. The queue stays the durability layer; Actions is the runtime.
//
// The payload deliberately carries the queue coordinates (`topic`, `jobId`) so the
// workflow can ack the exact job. Without them a callback could only say "some
// summary finished", which is not enough to delete the right row.

// GitHub caps client_payload at 10 fields and ~64 KB. Notification bodies can be
// long, so the text is truncated here rather than failing the dispatch — a
// truncated summary beats no summary, and the workflow re-fetches from the API
// when it needs the full thing.
const MAX_PAYLOAD_CHARS = 30_000;

// GitHub auth is the rgs-deploy-gate GitHub App (JWT → installation token), the
// same mechanism deploy-gate already uses to fire `dev-card-posted`. Reusing it
// means no new credential to mint: APP_ID and APP_PRIVATE_KEY are already org
// secrets, synced here by CD. A fresh PAT would have been a second long-lived
// GitHub credential for one call.
const b64url = (buf) =>
  btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

/** Import a PKCS#8 PEM for RS256 signing. */
async function importKey(pem) {
  const body = pem
    .replace(/-----BEGIN [^-]+-----/, "")
    .replace(/-----END [^-]+-----/, "")
    .replace(/\s+/g, "");
  const der = Uint8Array.from(atob(body), (c) => c.charCodeAt(0));
  return crypto.subtle.importKey(
    "pkcs8",
    der,
    { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
    false,
    ["sign"],
  );
}

async function appJwt(env) {
  const now = Math.floor(Date.now() / 1000);
  const enc = new TextEncoder();
  const header = b64url(enc.encode(JSON.stringify({ alg: "RS256", typ: "JWT" })));
  // iat backdated 30s: GitHub rejects a JWT whose iat is even slightly ahead of
  // its clock, and Workers' clock is not guaranteed to agree with theirs.
  const payload = b64url(
    enc.encode(JSON.stringify({ iat: now - 30, exp: now + 540, iss: env.APP_ID })),
  );
  const key = await importKey(env.APP_PRIVATE_KEY);
  const sig = await crypto.subtle.sign(
    "RSASSA-PKCS1-v1_5",
    key,
    enc.encode(`${header}.${payload}`),
  );
  return `${header}.${payload}.${b64url(sig)}`;
}

async function installationToken(env, repo) {
  const jwt = await appJwt(env);
  const headers = {
    accept: "application/vnd.github+json",
    authorization: `Bearer ${jwt}`,
    "user-agent": "ops-summarizer",
  };
  const inst = await fetch(`https://api.github.com/repos/${repo}/installation`, { headers });
  if (!inst.ok) throw new Error(`installation lookup ${inst.status}`);
  const { id } = await inst.json();
  const tok = await fetch(`https://api.github.com/app/installations/${id}/access_tokens`, {
    method: "POST",
    headers,
  });
  if (!tok.ok) throw new Error(`installation token ${tok.status}`);
  return (await tok.json()).token;
}

/** Lease long enough for a cold runner to install the CLI and run a model. */
export const LEASE_MS = 20 * 60_000;

function trim(value) {
  if (typeof value !== "string") return value;
  return value.length > MAX_PAYLOAD_CHARS ? `${value.slice(0, MAX_PAYLOAD_CHARS)}…` : value;
}

/**
 * Fire a repository_dispatch for one job.
 *
 * @returns {Promise<{ok: true, leased: true, leaseMs: number}
 *                  | {ok: false, status: number, error: string}>}
 */
export async function dispatchSummary(env, { topic, jobId, data }) {
  const repo = env.DISPATCH_REPO;
  if (!repo || !env.APP_ID || !env.APP_PRIVATE_KEY) {
    return { ok: false, status: 500, error: "DISPATCH_REPO, APP_ID or APP_PRIVATE_KEY unset" };
  }

  let token;
  try {
    token = await installationToken(env, repo);
  } catch (e) {
    return { ok: false, status: 502, error: `github app auth: ${e.message}` };
  }

  const payload = {};
  for (const [k, v] of Object.entries(data || {})) payload[k] = trim(v);

  const res = await fetch(`https://api.github.com/repos/${repo}/dispatches`, {
    method: "POST",
    headers: {
      accept: "application/vnd.github+json",
      authorization: `Bearer ${token}`,
      "content-type": "application/json",
      // GitHub rejects dispatches without a User-Agent.
      "user-agent": "ops-summarizer",
    },
    body: JSON.stringify({
      event_type: "summarize",
      client_payload: { topic, jobId, data: payload },
    }),
  });

  if (res.status === 204) return { ok: true, leased: true, leaseMs: LEASE_MS };

  // 404 here usually means the token cannot see the repo rather than that the repo
  // is missing — worth saying, because the two look identical from the caller.
  const body = await res.text();
  return {
    ok: false,
    status: res.status,
    error: `github dispatch ${res.status}: ${body.slice(0, 200)}`,
  };
}
