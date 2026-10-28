"use strict";

/* The browser checks run against whatever demo server HOMESTEAD_URL names,
   and each one is run somewhere, so none drifts from the UI unnoticed. */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), path = require("node:path");

const read = file => fs.readFileSync(file, "utf8");
const checks = fs.readdirSync("scripts").filter(f => /^(check|audit)_.*\.mjs$/.test(f)).map(f => `scripts/${f}`)
  .concat(fs.readdirSync("tests/integration").filter(f => f.endsWith(".mjs")).map(f => `tests/integration/${f}`));

test("ci_ui_checks.sh keeps a HOMESTEAD_URL that is already set", () => {
  assert.match(read("scripts/ci_ui_checks.sh"), /^export HOMESTEAD_URL="\$\{HOMESTEAD_URL:-http:\/\/127\.0\.0\.1:4173\}"$/m);
});

test("every Playwright install names one version, 1.63 or later", () => {
  // Before 1.63 (Chromium 153) a garbage collection could lose a settled
  // page.evaluate answer, which read as "Execution context was destroyed"
  // (tests/integration/evaluate-survives-gc.mjs). The CI cache key must follow.
  const places = [...fs.readdirSync(".github/workflows").map(f => `.github/workflows/${f}`), "CONTRIBUTING.md", "Dockerfile",
    ...fs.readdirSync("docs").filter(f => f.endsWith(".md")).map(f => `docs/${f}`)];
  const pins = places.flatMap(file => [...read(file).matchAll(/playwright(?:[@-]|:v)(\d+\.\d+\.\d+)/g)].map(m => `${file}: ${m[1]}`));
  const count = file => pins.filter(pin => pin.startsWith(`${file}: `)).length;
  // Both installs in ci.yml and its browser cache key, the demo and release builds, and the contributors' guide.
  assert.deepEqual([".github/workflows/ci.yml", ".github/workflows/demo.yml", ".github/workflows/release.yml", "CONTRIBUTING.md"].map(count),
    [3, 1, 1, 1], pins.join("\n"));
  const unpinned = places.flatMap(file => read(file).split("\n").filter(line => /npm (install|i) .*\bplaywright\b(?![@-])/.test(line)).map(line => `${file}: ${line.trim()}`));
  assert.deepEqual(unpinned, [], "every install names its version");
  const versions = new Set(pins.map(pin => pin.split(": ")[1]));
  assert.equal(versions.size, 1, pins.join("\n"));
  const [major, minor] = [...versions][0].split(".").map(Number);
  assert.ok(major > 1 || minor >= 63, `Playwright ${[...versions][0]} loses evaluate answers to garbage collection`);
});

test("every browser check opens the demo at HOMESTEAD_URL", () => {
  const fixed = checks.filter(file => /goto\(\s*["'`]http:\/\/127\.0\.0\.1:\d+/.test(read(file)));
  assert.deepEqual(fixed, [], "read process.env.HOMESTEAD_URL, with the old address as the fallback");
});

test("every check script is run by CI, a release, or another check", () => {
  const runners = ["scripts/ci_ui_checks.sh", ...fs.readdirSync(".github/workflows").map(f => `.github/workflows/${f}`)]
    .concat(checks).map(file => [file, read(file)]);
  const orphans = checks.filter(file => file.startsWith("scripts/check_")).filter(file =>
    !runners.some(([other, text]) => other !== file && text.includes(path.basename(file))));
  assert.deepEqual(orphans, []);
});
