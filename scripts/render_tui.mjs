// The installer's screens, captured by capture_tui.sh as terminal text with
// its colour codes, drawn as PNGs for the wiki: release-assets/tui-<name>.ans
// becomes release-assets/homestead-tui-<name>.png. A screen that did not
// capture is skipped - a missing picture never holds a release back.
import { chromium } from "playwright";
import { readdir, readFile } from "node:fs/promises";

const dir = "release-assets";
// A soft palette (Tango's), with black as the frame's own so the screen
// around a box blends into it.
const BASIC = ["#101014", "#cc3e44", "#4e9a06", "#c4a000", "#3465a4", "#75507b", "#06989a", "#d3d7cf",
  "#555753", "#ef2929", "#8ae234", "#fce94f", "#729fcf", "#ad7fa8", "#34e2e2", "#eeeeec"];
const cube = n => {
  if (n < 16) return BASIC[n];
  if (n >= 232) { const v = 8 + (n - 232) * 10; return `rgb(${v},${v},${v})`; }
  const i = n - 16, level = x => (x ? 55 + x * 40 : 0);
  return `rgb(${level(Math.floor(i / 36))},${level(Math.floor(i / 6) % 6)},${level(i % 6)})`;
};
const escape = text => text.replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
// Browser font fallback can give Braille a wider advance than terminal text.
// Terminals allocate exactly one cell; keep the preview on that same grid.
const terminalText = text => escape(text).replace(/[\u2800-\u28ff]/g, char => `<span class="dot">${char}</span>`);

/* Just the box: blank rows above and below dropped (their colour codes kept,
   as later rows rely on them), and the margin every row shares taken off. */
function crop(ans) {
  const visible = line => line.replace(/\x1b\[[0-9;?]*[A-Za-z]/g, "");
  const lines = ans.replace(/\n+$/, "").split("\n");
  const used = lines.map((line, i) => (visible(line).trim() ? i : -1)).filter(i => i >= 0);
  if (!used.length) return ans;
  const first = used[0], last = used[used.length - 1];
  const codes = lines.slice(0, first).map(line => (line.match(/\x1b\[[0-9;]*m/g) || []).join("")).join("");
  const kept = lines.slice(first, last + 1);
  const margin = Math.min(...kept.filter(line => visible(line).trim()).map(line => line.match(/^ */)[0].length));
  return codes + kept.map(line => line.slice(Math.min(margin, line.match(/^ */)[0].length))).join("\n");
}

/* Terminal text with SGR colour codes -> HTML: a row per line, each run of
   one colour a cell as tall as the row, so backgrounds meet with no gaps. */
function toHtml(ans) {
  const state = { fg: null, bg: null, bold: false, reverse: false };
  let html = "<div class=row>";
  for (const part of ans.split(/(\x1b\[[0-9;]*m)/)) {
    const sgr = part.match(/^\x1b\[([0-9;]*)m$/);
    if (!sgr) {
      if (!part) continue;
      let fg = state.fg || "#c8c8c8", bg = state.bg || "transparent";
      if (state.reverse) [fg, bg] = [bg === "transparent" ? "#101014" : bg, fg];
      const style = `color:${fg};background:${bg};${state.bold ? "font-weight:700;" : ""}`;
      const lines = part.replace(/\x1b\[[0-9;?]*[A-Za-z]/g, "").split("\n");
      html += lines.map(line => line ? `<span style="${style}">${terminalText(line)}</span>` : "").join("</div><div class=row>");
      continue;
    }
    const codes = (sgr[1] || "0").split(";").map(Number);
    for (let i = 0; i < codes.length; i++) {
      const c = codes[i];
      if (c === 0) Object.assign(state, { fg: null, bg: null, bold: false, reverse: false });
      else if (c === 1) state.bold = true;
      else if (c === 22) state.bold = false;
      else if (c === 7) state.reverse = true;
      else if (c === 27) state.reverse = false;
      else if (c >= 30 && c <= 37) state.fg = BASIC[c - 30];
      else if (c >= 90 && c <= 97) state.fg = BASIC[c - 90 + 8];
      else if (c >= 40 && c <= 47) state.bg = BASIC[c - 40];
      else if (c >= 100 && c <= 107) state.bg = BASIC[c - 100 + 8];
      else if (c === 39) state.fg = null;
      else if (c === 49) state.bg = null;
      else if ((c === 38 || c === 48) && codes[i + 1] === 5) { state[c === 38 ? "fg" : "bg"] = cube(codes[i + 2]); i += 2; }
      else if ((c === 38 || c === 48) && codes[i + 1] === 2) {
        state[c === 38 ? "fg" : "bg"] = `rgb(${codes[i + 2]},${codes[i + 3]},${codes[i + 4]})`; i += 4;
      }
    }
  }
  return html + "</div>";
}

const files = (await readdir(dir).catch(() => [])).filter(f => /^tui-.+\.ans$/.test(f));
if (!files.length) { console.log("no TUI captures to render"); process.exit(0); }
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1000, height: 700 }, deviceScaleFactor: 2 });
for (const file of files) {
  const ans = await readFile(`${dir}/${file}`, "utf8");
  await page.setContent(`<html><head><style>
      body{margin:0;background:#101014}
      #term{display:inline-block;padding:18px 20px;background:#101014;border-radius:10px;
        font:14px 'DejaVu Sans Mono',Menlo,Consolas,monospace;color:#c8c8c8}
      .row{height:18px;line-height:18px;white-space:pre}
      .row span{display:inline-block;height:18px;vertical-align:top}
      .row .dot{width:1ch;text-align:center}
    </style></head><body><div id="term">${toHtml(crop(ans))}</div></body></html>`);
  const name = `homestead-${file.replace(/\.ans$/, "")}.png`;
  await page.locator("#term").screenshot({ path: `${dir}/${name}` });
  console.log(`rendered ${name}`);
}
await browser.close();
