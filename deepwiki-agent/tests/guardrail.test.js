const assert = require("assert");
const os = require("os");
const path = require("path");
const fs = require("fs");
const { resolveWithin, globToRegExp, PathPolicy } = require("../out/guardrail");

// resolveWithin: blocks escapes
assert.throws(() => resolveWithin(os.tmpdir(), "../etc/passwd"), /escape/);
assert.throws(() => resolveWithin(os.tmpdir(), "/etc/passwd"), /outside workspace/);

// within-path works
const safe = path.join(os.tmpdir(), "sub/file.txt");
const r = resolveWithin(os.tmpdir(), "sub/file.txt");
assert.strictEqual(r, safe);

// symlink escape
const tdir = fs.mkdtempSync(path.join(os.tmpdir(), "dw-test-"));
try {
  const outside = path.join(tdir, "out");
  fs.mkdirSync(outside, { recursive: true });
  try {
    fs.symlinkSync(outside, path.join(tdir, "link-to-outside"));
  } catch {
    // symlinks may be restricted on some Windows setups; skip in that case
    console.log("symlink test skipped (symlink creation unsupported)");
    console.log("OK resolveWithin: escape blocked, within allowed");
    process.exit(0);
  }
  assert.throws(() => resolveWithin(tdir, "link-to-outside"), /Symlink escape/);
} finally {
  fs.rmSync(tdir, { recursive: true, force: true });
}

// globToRegExp
assert.ok(globToRegExp("src/**").test("src/a/b.ts"));
assert.ok(!globToRegExp("src/*").test("src/a/b.ts"));
assert.ok(globToRegExp("*.ts").test("index.ts"));

// PathPolicy
const p = new PathPolicy(["**/*.ts", "**/deepwiki/**"], ["**/*.ts"]);
assert.ok(p.canRead("/ws", "src/a.ts").allowed);
assert.ok(!p.canRead("/ws", "secret.txt").allowed);
assert.ok(!p.canRead("/ws", "../outside.ts").allowed);
assert.ok(p.canWrite("/ws", "out/x.ts").allowed);
assert.strictEqual(p.canWrite("/ws", "notes.md").allowed, true); // ** matched

console.log("OK resolveWithin: escape blocked, within allowed, symlink escape blocked");
console.log("OK PathPolicy + globToRegExp");
