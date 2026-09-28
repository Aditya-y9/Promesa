const assert = require("assert");
const os = require("os");
const path = require("path");
const fs = require("fs");
const { PermissionManager, PERMISSION_PROFILES } = require("../out/permission");

const root = fs.mkdtempSync(path.join(os.tmpdir(), "dw-perm-"));

// Default is read-only
const pm = new PermissionManager(root, "readonly");
assert.strictEqual(pm.current().profile, "readonly");
assert.ok(pm.canRead(path.join(root, "src/file.ts")), "readonly can read");
assert.ok(!pm.canWrite(path.join(root, "src/file.ts")), "readonly cannot write");
assert.ok(!pm.canExec(), "readonly cannot exec");
assert.ok(!pm.canNetwork(), "readonly cannot network");

// escalate
const g = pm.escalate("workspace-write", "user", "need to edit");
assert.ok(g, "escalation should succeed");
assert.ok(pm.canWrite(path.join(root, "README.md")), "workspace-write can write");
assert.ok(!pm.canNetwork(), "workspace-write still cannot network");

// Cannot downgrade an active grant
const downgrade = pm.escalate("readonly", "user", "want readonly back");
assert.strictEqual(downgrade, undefined, "cannot downgrade active grant");

// Def() maps
assert.strictEqual(PERMISSION_PROFILES["readonly"].allowExec, false);
assert.strictEqual(PERMISSION_PROFILES["workspace-exec"].allowExec, true);
assert.strictEqual(PERMISSION_PROFILES["elevated"].allowNetwork, true);

fs.rmSync(root, { recursive: true, force: true });
console.log("OK permission: defaults, escalation, no-downgrade, exec/network flags");