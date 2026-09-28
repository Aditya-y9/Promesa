const assert = require("assert");
const os = require("os");
const path = require("path");
const fs = require("fs");
const { PermissionManager } = require("../out/permission");
const { TraceService } = require("../out/trace");
const { ApprovalEngine } = require("../out/approvals");

const root = fs.mkdtempSync(path.join(os.tmpdir(), "dw-app-"));

const perms = new PermissionManager(root, "readonly");
const trace = new TraceService(path.join(root, "traces"), "gemini-1.5-pro", root, "readonly");
const engine = new ApprovalEngine(perms, trace, [{ risk: "low", action: "read" }]);

// Read in readonly → auto-approved low risk
const read = engine.evaluate("read", "read a file", { risk: "low", tool: "read" });
assert.strictEqual(read.requiresHuman, false, "low-risk read should auto-approve");
assert.strictEqual(read.allowed, true);

// Exec in readonly → blocked (not capability)
const exec = engine.evaluate("run", "run npm test", { risk: "high", tool: "exec" });
assert.strictEqual(exec.allowed, false, "exec not allowed under readonly");
assert.strictEqual(exec.requiresHuman, true, "exec requires human");

// Irreversible critical always requires human
const crit = engine.evaluate("drop", "drop_table", { risk: "critical" });
assert.strictEqual(crit.requiresHuman, true, "critical irreversible always human");
assert.strictEqual(crit.canApprove, false, "critical irreversible cannot auto-approve");

// In workspace-exec, medium is auto-approvable
const perms2 = new PermissionManager(root, "workspace-exec");
const trace2 = new TraceService(path.join(root, "traces2"), "gemini-1.5-pro", root, "workspace-exec");
const engine2 = new ApprovalEngine(perms2, trace2, []);
const exe2 = engine2.evaluate("modify", "edit file", { risk: "medium", tool: "write" });
assert.strictEqual(exe2.requiresHuman, false, "workspace-exec allows medium write");

fs.rmSync(root, { recursive: true, force: true });
console.log("OK approvals: auto-approve low, block exec readonly, always-gate critical, profile matrix");