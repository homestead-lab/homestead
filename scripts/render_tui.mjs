// The installer's screens, captured by capture_tui.sh as terminal text with
// its colour codes, drawn as PNGs for the wiki: release-assets/tui-<name>.ans
// becomes release-assets/homestead-tui-<name>.png. A screen that did not
// capture is skipped - a missing picture never holds a release back.
import { chromium } from "playwright";
import { readdir, readFile } from "node:fs/promises";

const dir = "release-assets";
const BASIC = ["#000000", "#aa0000", "#00aa00", "#aa5500", "#0000aa", "#aa00aa", "#00aaaa", "#aaaaaa",
  "#555555", "#ff5555", "#55ff55", "#ffff55", "#5555ff", "#ff55ff", "#55ffff", "#ffffff"];
const cube = n => {
  if (n < 16) return BASIC[n];
  if (n >= 232) { const v = 8 + (n - 232) * 10; return `rgb(${v},${v},${v})`; }
  const i = n - 16, level = x => (x ? 55 + x * 40 : 0);
  return `rgb(${level(Math.floor(i / 36))},${level(Math.floor(i / 6) % 6)},${level(i % 6)})`;
};
const escape = text => text.replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

/* Terminal text with SGR colour codes -> HTML spans. */
function toHtml(ans) {
  const state = { fg: null, bg: null, bold: false, reverse: false };
  let html = "";
  for (const part of ans.split(/(\x1b\[[0-9;]*m)/)) {
    const sgr = part.match(/^\x1b\[([0-9;]*)m$/);
    if (!sgr) {
      if (!part) continue;
      let fg = state.fg || "#c8c8c8", bg = state.bg || "transparent";
      if (state.reverse) [fg, bg] = [bg === "transparent" ? "#101014" : bg, fg];
      html += `<span style="color:${fg};background:${bg};${state.bold ? "font-weight:700;" : ""}">${escape(part.replace(/\x1b\[[0-9;?]*[A-Za-z]/g, ""))}</span>`;
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
  return html;
}

const files = (await readdir(dir).catch(() => [])).filter(f => /^tui-.+\.ans$/.test(f));
if (!files.length) { console.log("no TUI captures to render"); process.exit(0); }
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1000, height: 700 }, deviceScaleFactor: 2 });
for (const file of files) {
  const ans = await readFile(`${dir}/${file}`, "utf8");
  await page.setContent(`<html><body style="margin:0;background:#101014">
    <div id="term" style="display:inline-block;padding:18px 20px;background:#101014;border-radius:10px">
      <pre style="margin:0;font:14px/1.25 'DejaVu Sans Mono',Menlo,Consolas,monospace;color:#c8c8c8">${toHtml(ans)}</pre></div></body></html>`);
  const name = `homestead-${file.replace(/\.ans$/, "")}.png`;
  await page.locator("#term").screenshot({ path: `${dir}/${name}` });
  console.log(`rendered ${name}`);
}
await browser.close();
