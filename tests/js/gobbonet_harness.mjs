// Runs the REAL gobbonet-backup.js under node's WebCrypto against an
// in-memory fake of the GitHub release API, so the Python envelope can be
// checked byte-for-byte against the browser code (not a re-typed copy).
//
//   node gobbonet_harness.mjs restore <assets_dir> <tag> <out_dir>
//   node gobbonet_harness.mjs backup  <in_dir> <out_dir>      (prints {"tag":...})
//
// The passphrase comes from $GOBBONET_HARNESS_PASSPHRASE -- never argv.
import { readFileSync, readdirSync, writeFileSync, mkdirSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const MOD = join(here, "..", "..", "awrtifact", "data", "gobbonet", "gobbonet-backup.js");
const pass = process.env.GOBBONET_HARNESS_PASSPHRASE || "";
const [mode, a1, a2, a3] = process.argv.slice(2);

const assets = new Map(); // name -> Uint8Array
let release = null;

function json(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

globalThis.window = globalThis;
globalThis.document = { body: null, addEventListener() {} };
globalThis.localStorage = { getItem() { return null; }, setItem() {} };
globalThis.fetch = async (url, opts = {}) => {
  const u = new URL(url);
  const method = opts.method || "GET";
  const p = u.pathname;
  if (p.endsWith("/commits")) return json([{ sha: "seed" }]);
  let m = p.match(/\/releases\/tags\/(.+)$/);
  if (m && method === "GET") {
    if (release && release.tag === decodeURIComponent(m[1])) return json({ id: 1, upload_url: release.upload_url });
    return json({ message: "Not Found" }, 404);
  }
  if (p.endsWith("/releases") && method === "POST") {
    const body = JSON.parse(opts.body);
    release = { tag: body.tag_name, upload_url: "https://uploads.github.com/repos/o/r/releases/1/assets{?name,label}" };
    return json({ id: 1, upload_url: release.upload_url });
  }
  if (u.host === "uploads.github.com" && method === "POST") {
    assets.set(u.searchParams.get("name"), new Uint8Array(opts.body));
    return json({ id: assets.size });
  }
  if (/\/releases\/1$/.test(p) && method === "PATCH") return json({ id: 1 });
  if (/\/releases\/1\/assets$/.test(p)) {
    return json([...assets.keys()].map((name, i) => ({ id: i + 1, name })));
  }
  m = p.match(/\/releases\/assets\/(\d+)$/);
  if (m) {
    const name = [...assets.keys()][Number(m[1]) - 1];
    return new Response(assets.get(name), { status: 200 });
  }
  return json({ message: "unexpected " + method + " " + url }, 500);
};

vm.runInThisContext(readFileSync(MOD, "utf8"), { filename: MOD });
const core = globalThis.__gobbonetBackup;

if (mode === "restore") {
  const [dir, tag, out] = [a1, a2, a3];
  release = { tag, upload_url: "" };
  for (const name of readdirSync(dir)) assets.set(name, new Uint8Array(readFileSync(join(dir, name))));
  // The gate page's call shape: tokenless, owner + repo split.
  const files = await core.restoreBackup("", "o", "r", pass, tag);
  mkdirSync(out, { recursive: true });
  for (const f of files) writeFileSync(join(out, f.name), Buffer.from(await f.blob.arrayBuffer()));
  console.log(JSON.stringify({ restored: files.map((f) => f.name) }));
} else if (mode === "backup") {
  const [dir, out] = [a1, a2];
  const files = readdirSync(dir).map((n) => new File([readFileSync(join(dir, n))], n));
  const res = await core.backupFiles("tok", "o", "r", pass, files);
  mkdirSync(out, { recursive: true });
  for (const [name, bytes] of assets) writeFileSync(join(out, name), bytes);
  console.log(JSON.stringify({ tag: res.tag }));
} else {
  console.error("usage: restore <dir> <tag> <out> | backup <in> <out>");
  process.exit(2);
}
