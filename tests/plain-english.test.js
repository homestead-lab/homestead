/* The house style (docs/design.md, Clear, professional English) on every
   screen: neutral voice, no contractions, and the words the app settled on.
   A phrasing that comes back fails here with the file and the text. */
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const DIR = path.join(__dirname, "..", "web", "js");
// demo.js stands in for the server's messages, which have their own pass.
const FILES = fs.readdirSync(DIR).filter(f => f.endsWith(".js") && f !== "demo.js");

/* Every string literal in a file, comments left out: '…', "…" and the
   literal parts of `…` (what sits between ${…} is code, not words). */
function strings(source) {
  const out = [];
  for (let i = 0; i < source.length; i++) {
    const c = source[i], next = source[i + 1];
    if (c === "/" && next === "/") { while (i < source.length && source[i] !== "\n") i++; continue; }
    if (c === "/" && next === "*") { i = source.indexOf("*/", i + 2); if (i < 0) break; i++; continue; }
    if (c !== '"' && c !== "'" && c !== "`") continue;
    let text = "", depth = 0;
    for (i++; i < source.length; i++) {
      const d = source[i];
      if (d === "\\") { text += source[i + 1] || ""; i++; continue; }
      if (c === "`" && d === "$" && source[i + 1] === "{") {
        // Skip the code in ${…}, nested templates and braces included.
        depth = 1; i += 2; text += " ";
        for (; i < source.length && depth; i++) {
          if (source[i] === "{") depth++;
          else if (source[i] === "}") depth--;
          else if (source[i] === "`") { i = source.indexOf("`", i + 1); if (i < 0) break; }
        }
        i--; continue;
      }
      if (d === c) break;
      if (d === "\n" && c !== "`") break;
      text += d;
    }
    out.push(text);
  }
  return out;
}

/* Text a person reads: words with spaces, not a selector, a URL or code. */
const prose = text => /[A-Za-z]{2,} [A-Za-z]{2,}/.test(text) && !/^[\s\w.#:[\]=-]*$/.test(text) || /^[A-Z][a-z]+'?[a-z]*$/.test(text.trim());

const BANNED = [
  [/\b(can't|couldn't|don't|doesn't|isn't|won't|wasn't|aren't|haven't|hasn't|shouldn't|wouldn't)\b/i, "write it out (cannot, could not, do not…)"],
  [/\byou(r|rs|rself)?\b/i, "neutral voice: no you or your"],
  [/\bYour VIPs\b|\bworkload VIP\b|\bService VIP\b/, "say virtual IP (VIP only in chips and table headers)"],
  [/\bCarry on\b|\bInspect outcome\b|\bMake ready\b/, "use the current button names (Resume job, Check outcome, Prepare)"],
  [/\brecurring jobs?\b/i, "Longhorn recurring jobs are schedules (keep the term in a tip)"],
];
// Strings that keep a banned word on purpose: what a tip or a quoted term says.
const ALLOWED = [/Longhorn calls these recurring jobs/, /RTNETLINK answers/];

test("no screen uses a contraction, says you, or brings back a retired phrasing", () => {
  const found = [];
  for (const file of FILES) {
    for (const text of strings(fs.readFileSync(path.join(DIR, file), "utf8"))) {
      if (!prose(text) || ALLOWED.some(re => re.test(text))) continue;
      for (const [re, why] of BANNED) if (re.test(text)) found.push(`${file}: ${why}: ${text.trim().slice(0, 120)}`);
    }
  }
  assert.deepEqual(found, []);
});
