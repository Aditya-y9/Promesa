const assert = require("assert");
const os = require("os");
const path = require("path");
const fs = require("fs");
const { estimateCostUsd } = require("../out/trace");

// Cost estimation is deterministic and sane.
const c = estimateCostUsd("gemini-1.5-pro", 1_000_000, 1_000_000);
// 1M in * 1.25 + 1M out * 5.0 = 6.25
assert(Math.abs(c - 6.25) < 0.001, "cost should be ~$6.25, got " + c);

const c0 = estimateCostUsd("unknown-model", 1_000_000, 1_000_000);
assert(c0 > 0, "unknown model falls back to default");

console.log("OK trace: estimateCostUsd deterministic");