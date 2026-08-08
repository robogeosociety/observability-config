// Read vaults.toml — the policy for what the cloud index carries and who reads what.
//
// A deliberately tiny parser rather than a dependency. Every sibling Worker and script
// in this repo is dependency-free, and this file has exactly two shapes in it: a
// top-level `key = ["a", "b"]` and `[consumers.name]` tables containing the same. A
// TOML library would be a larger commitment than the grammar it is parsing.
//
// It is strict about what it does not understand: an unrecognised line raises rather
// than being skipped, because a policy file that silently ignores half its contents is
// how a consumer ends up reading vaults nobody granted it.

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const HERE = dirname(fileURLToPath(import.meta.url));
export const POLICY_PATH = join(HERE, "..", "vaults.toml");

const ARRAY_LINE = /^([A-Za-z_][\w-]*)\s*=\s*\[(.*)\]\s*$/;
const TABLE_LINE = /^\[([A-Za-z_][\w.-]*)\]\s*$/;

/** Parse the subset of TOML this policy uses. Throws on anything else. */
export function parsePolicy(text) {
  const out = { mirrored: null, consumers: {} };
  let table = null;
  for (const raw of text.split("\n")) {
    const line = raw.replace(/#.*$/, "").trim();
    if (!line) continue;

    const t = TABLE_LINE.exec(line);
    if (t) {
      const parts = t[1].split(".");
      if (parts.length !== 2 || parts[0] !== "consumers") {
        throw new Error(`unexpected table [${t[1]}] — only [consumers.<name>] is understood`);
      }
      table = parts[1];
      out.consumers[table] = { vaults: [] };
      continue;
    }

    const a = ARRAY_LINE.exec(line);
    if (!a) throw new Error(`unparseable policy line: ${line}`);
    const values = a[2].split(",").map((s) => s.trim().replace(/^["']|["']$/g, "")).filter(Boolean);
    if (a[1] === "mirrored" && table === null) out.mirrored = values;
    else if (a[1] === "vaults" && table) out.consumers[table].vaults = values;
    else throw new Error(`unexpected key ${a[1]} ${table ? `in [consumers.${table}]` : "at top level"}`);
  }

  if (!out.mirrored?.length) throw new Error("vaults.toml: `mirrored` is missing or empty");
  for (const [name, spec] of Object.entries(out.consumers)) {
    if (!spec.vaults.length) {
      throw new Error(`consumer ${name} lists no vaults — remove the table instead of emptying it`);
    }
    // A consumer pointed at a vault the mirror does not ship reads an empty index and
    // looks exactly like a vault with no answers. Catch it here, not in production.
    for (const v of spec.vaults) {
      if (!out.mirrored.includes(v)) {
        throw new Error(`consumer ${name} reads ${v}, which is not mirrored into the index`);
      }
    }
  }
  return out;
}

export function loadPolicy(path = POLICY_PATH) {
  return parsePolicy(readFileSync(path, "utf8"));
}
