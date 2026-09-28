// Minimal test harness — no framework, runs in plain Node.
// Run with: node tests/run-all.js
const { execFileSync } = require("child_process");
const path = require("path");
const fs = require("fs");

const files = ["guardrail", "injection", "evaluate", "permission", "trace", "approvals"];
let failures = 0;
let passes = 0;

for (const f of files) {
  const p = path.join(__dirname, f + ".test.js");
  if (!fs.existsSync(p)) {
    console.log("SKIP " + f + " (no " + f + ".test.js)");
    continue;
  }
  try {
    const out = execFileSync(process.execPath, [p], { encoding: "utf-8" });
    passes++;
    console.log("PASS " + f + "\n" + out.trim().split("\n").map((l) => "  " + l).join("\n"));
  } catch (e) {
    failures++;
    console.log("FAIL " + f + "\n" + (e.stdout || "(no stdout)") + "\n" + (e.stderr || ""));
  }
}

console.log("\n---\n" + passes + " test file(s) passed, " + failures + " failed");
process.exit(failures ? 1 : 0);
