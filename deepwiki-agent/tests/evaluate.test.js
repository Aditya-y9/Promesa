const assert = require("assert");
const { validatePlan, hasUnsafeActions } = require("../out/evaluate");

// Valid plan
const valid = {
  title: "Test plan",
  summary: "A valid plan",
  steps: [{ order: 1, action: "investigate", description: "Look around", filesAffected: [] }],
  risks: [{ level: "LOW", kind: "assumption", description: "none", verifiedFrom: [] }],
  mermaid: "flowchart TD\n  A --> B",
};
let vr = validatePlan(valid);
assert.ok(vr.ok, "valid plan should pass: " + JSON.stringify(vr.errors));

// Plan with no title
vr = validatePlan({ steps: [{ order: 1, action: "investigate", description: "x", filesAffected: [] }] });
assert.ok(!vr.ok, "no-title plan should fail");
assert(vr.errors.some((e) => e.includes("title")), "error should mention missing title");

// Plan with duplicate step orders
vr = validatePlan({
  title: "bad",
  summary: "dup",
  steps: [
    { order: 1, action: "test", description: "first", filesAffected: [] },
    { order: 1, action: "modify", description: "also first", filesAffected: ["x.ts"] },
  ],
});
assert.ok(!vr.ok, "duplicate order should fail");
assert(vr.errors.some((e) => e.includes("duplicate")), "should mention duplicate");

// Plan with modify but no filesAffected
vr = validatePlan({
  title: "warn",
  summary: "modify without files",
  steps: [{ order: 1, action: "modify", description: "change things", filesAffected: [] }],
});
assert.ok(vr.ok, "warning-only should still pass");
assert(vr.warnings.some((w) => w.includes("no filesAffected")), "should warn");

// hasUnsafeActions
const risks = hasUnsafeActions([
  { order: 1, action: "modify", description: "drop_table users", filesAffected: [] },
  { order: 2, action: "test", description: "run tests", filesAffected: [] },
]);
assert.strictEqual(risks.length, 1, "should flag one unsafe step");
assert(risks[0].includes("drop_table"), "should name the dangerous step");

console.log("OK evaluate: validate, duplicate/deficiency detection, unsafe action flagging");