// The captures from audit_dialogs.mjs (or audit_pages.mjs) laid out side by
// side, for reviewing many at once: release-assets/sheets/<set>-<width>-NN.png.
//
//   node scripts/dialog_sheets.mjs mobile 8 4           width, captures per sheet, columns
//   node scripts/dialog_sheets.mjs mobile 4 4 pages     the pages instead of the dialogs
import { chromium } from "playwright";
import { readdirSync, readFileSync, mkdirSync } from "node:fs";
const set = process.argv[5] || "dialogs";
const dir = `release-assets/${set}`, out = "release-assets/sheets";
mkdirSync(out, { recursive: true });
const kind = process.argv[2] || "desktop", per = Number(process.argv[3] || 6), cols = Number(process.argv[4] || 3);
const files = readdirSync(dir).filter(f => f.endsWith(`-${kind}.png`)).sort();
const b = await chromium.launch(); const p = await b.newPage({ viewport: { width: 1800, height: 1000 } });
for (let i = 0; i < files.length; i += per) {
  const cells = files.slice(i, i + per).map(f => `<figure><figcaption>${f.replace(`-${kind}.png`, "")}</figcaption><img src="data:image/png;base64,${readFileSync(`${dir}/${f}`).toString("base64")}"></figure>`).join("");
  await p.setContent(`<style>body{margin:0;background:#222;font:14px sans-serif;color:#fff}#g{display:grid;grid-template-columns:repeat(${cols},1fr);gap:14px;padding:14px;align-items:start}img{width:100%;display:block}figure{margin:0}figcaption{padding:4px 0;color:#fc6}</style><div id="g">${cells}</div>`);
  await p.locator("#g").screenshot({ path: `${out}/${set}-${kind}-${String(i / per + 1).padStart(2, "0")}.png` });
}
await b.close(); console.log(Math.ceil(files.length / per), "sheets");
