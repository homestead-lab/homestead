// The README's main image and phone gallery, from the demo data.
//
// Each screen is taken at a phone's size (393 x 852 points, as an iPhone 15 or
// 16), then drawn inside a phone frame - status bar, Dynamic Island, side
// buttons - on a gradient in Homestead's colours. The main image puts the
// desktop dashboard, in a browser window, behind two phones:
//
//   release-assets/homestead-hero.jpg           desktop and phones, for the top of the README
//   release-assets/homestead-mobile.jpg         a row of five phones with captions
//   release-assets/homestead-mobile-<name>.jpg  one phone per screen
//
// They are JPEGs sized for where they are shown - the gradients and screens
// would make PNGs several megabytes each.
//
// Like capture_screenshots.mjs, a screen that cannot be taken is reported and
// left out: a missing picture never holds a release back.
import { chromium } from "playwright";
import { mkdir, readFile } from "node:fs/promises";

const output = "release-assets";
const raw = `${output}/mobile`;
await mkdir(raw, { recursive: true });
const base = (process.env.HOMESTEAD_URL || "http://127.0.0.1:4173") + "/?demo=1";

const SCREEN = { width: 393, height: 852 };
const STATUS = 54; // the status bar above the app, drawn by the frame
const SCALE = 2;
const QUALITY = 84;

const browser = await chromium.launch({ headless: true });
const missed = [];

// ------------------------------------------------------------ the screens
const phone = await browser.newContext({
  viewport: { width: SCREEN.width, height: SCREEN.height - STATUS },
  deviceScaleFactor: SCALE, isMobile: true, hasTouch: true, colorScheme: "dark",
});
const page = await phone.newPage();
await page.addInitScript(() => {
  localStorage.setItem("homestead.settings", JSON.stringify({ theme: "dark", bg: "soft", blur: 26, motion: "off", refresh: 60 }));
  localStorage.setItem("homestead.network.tab", "services");
});
await page.goto(base, { waitUntil: "networkidle" });
await page.locator("#views .phead").waitFor();
await page.evaluate(() => document.fonts.ready);
// Running jobs and scroll bars belong to a live session, not a picture.
await page.addStyleTag({ content: "#jobTray{display:none!important} ::-webkit-scrollbar{display:none}" });

const SHOTS = [
  { name: "dashboard", view: "dash", caption: "Dashboard" },
  { name: "containers", view: "workloads", caption: "Containers" },
  // Without the explanation at the top, so the load balancer and VIP cards show.
  { name: "networking", view: "network", caption: "VIPs and services", hide: "Networking roles" },
  { name: "volumes", view: "storage", caption: "Replicated volumes" },
  { name: "protection", view: "protect", caption: "Snapshots and backups" },
  { name: "nodes", view: "nodes", caption: "Nodes" },
];
const taken = [];
for (const shot of SHOTS) {
  try {
    await page.evaluate((view) => {
      if (!document.querySelector("#modal")?.classList.contains("hidden")) closeModal();
      document.querySelector(`#nav a[data-view="${view}"]`).click();
      window.scrollTo(0, 0);
    }, shot.view);
    await page.locator("#views .phead").waitFor();
    await page.waitForTimeout(1200);
    if (shot.hide) {
      await page.evaluate((text) => {
        const lead = [...document.querySelectorAll("#views *")].find((e) => e.children.length === 0 && e.textContent.trim().startsWith(text));
        const note = lead?.closest(".note");
        if (note) note.style.display = "none";
      }, shot.hide);
      await page.waitForTimeout(300);
    }
    await page.screenshot({ path: `${raw}/${shot.name}.png` });
    taken.push(shot);
  } catch (error) {
    missed.push(`${shot.name}: ${error.message.split("\n")[0]}`);
  }
}
await phone.close();

// The desktop dashboard, for the browser window in the main image.
let desktop = false;
try {
  const wide = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1.5, colorScheme: "dark" });
  const view = await wide.newPage();
  await view.addInitScript(() => {
    localStorage.setItem("homestead.settings", JSON.stringify({ theme: "dark", bg: "soft", blur: 26, motion: "off", refresh: 60 }));
  });
  await view.goto(base, { waitUntil: "networkidle" });
  await view.locator("#views .phead").waitFor();
  await view.evaluate(() => document.fonts.ready);
  await view.addStyleTag({ content: "#jobTray{display:none!important} ::-webkit-scrollbar{display:none}" });
  await view.waitForTimeout(1500);
  await view.screenshot({ path: `${raw}/desktop.png` });
  await wide.close();
  desktop = true;
} catch (error) {
  missed.push(`desktop: ${error.message.split("\n")[0]}`);
}

// ------------------------------------------------------------ the frames
const dataUrl = async (name) => `data:image/png;base64,${(await readFile(`${raw}/${name}.png`)).toString("base64")}`;
const images = Object.fromEntries(await Promise.all(taken.map(async (s) => [s.name, await dataUrl(s.name)])));
if (desktop) images.desktop = await dataUrl("desktop");

const STYLE = `
@import url("https://fonts.googleapis.com/css2?family=Inter+Tight:wght@500;600;700;800&display=swap");
*{box-sizing:border-box;margin:0}
body{background:#0b0f14}
#canvas{position:relative;overflow:hidden;font-family:"Inter Tight",-apple-system,"Segoe UI",Roboto,sans-serif;
  background:
    radial-gradient(60% 70% at 14% 12%, rgba(245,158,11,.55), transparent 60%),
    radial-gradient(50% 60% at 92% 92%, rgba(34,211,238,.30), transparent 60%),
    radial-gradient(45% 55% at 62% 0%, rgba(139,92,246,.30), transparent 60%),
    linear-gradient(140deg,#161226 0%,#0f1522 45%,#0a1418 100%)}
#canvas::before{content:"";position:absolute;inset:0;
  background-image:radial-gradient(rgba(255,255,255,.07) 1px, transparent 1.2px);background-size:26px 26px;
  mask-image:radial-gradient(80% 80% at 50% 45%, #000 30%, transparent 85%)}
.phone{position:absolute;width:425px;height:884px;padding:16px;border-radius:70px;
  background:linear-gradient(150deg,#4a4f57 0%,#1b1e23 22%,#0e1013 60%,#30343b 100%);
  box-shadow:inset 0 0 0 2px #07080a, inset 0 0 0 5px #23262c, 0 0 0 1.5px rgba(255,255,255,.18),
    0 70px 120px -30px rgba(0,0,0,.75), 0 30px 60px -20px rgba(0,0,0,.55)}
.phone .btn{position:absolute;width:5px;border-radius:3px;background:linear-gradient(90deg,#2a2d33,#565b63)}
.phone .b1{left:-4px;top:190px;height:34px}.phone .b2{left:-4px;top:250px;height:64px}
.phone .b3{left:-4px;top:330px;height:64px}.phone .b4{right:-4px;top:280px;height:100px}
.screen{position:relative;width:393px;height:852px;border-radius:55px;overflow:hidden;background:#000}
.screen img{position:absolute;left:0;top:${STATUS}px;width:393px;height:${SCREEN.height - STATUS}px}
.status{position:absolute;inset:0 0 auto 0;height:${STATUS}px;display:flex;align-items:center;justify-content:space-between;
  padding:4px 30px 0 50px;color:#fff;font-weight:600;font-size:17px;letter-spacing:-.01em;z-index:2}
.status .icons{display:flex;gap:7px;align-items:center}
.island{position:absolute;top:11px;left:50%;transform:translateX(-50%);width:124px;height:36px;border-radius:20px;background:#000;z-index:3}
.home{position:absolute;bottom:8px;left:50%;transform:translateX(-50%);width:138px;height:5px;border-radius:3px;background:rgba(255,255,255,.75);z-index:3}
.glare{position:absolute;inset:0;border-radius:55px;z-index:4;pointer-events:none;
  background:linear-gradient(118deg,rgba(255,255,255,.10) 0%,rgba(255,255,255,0) 28%)}
.window{position:absolute;border-radius:14px;overflow:hidden;background:#0d1014;
  box-shadow:0 0 0 1px rgba(255,255,255,.14), 0 60px 120px -30px rgba(0,0,0,.75), 0 24px 50px -20px rgba(0,0,0,.5)}
.window .bar{height:40px;display:flex;align-items:center;gap:8px;padding:0 16px;background:linear-gradient(#23262d,#1a1d22);
  border-bottom:1px solid rgba(255,255,255,.06)}
.window .dot{width:12px;height:12px;border-radius:50%}
.window .url{margin:0 auto;transform:translateX(-30px);display:flex;align-items:center;gap:7px;height:24px;padding:0 14px;
  border-radius:7px;background:rgba(255,255,255,.07);color:rgba(255,255,255,.72);font-size:13px;font-weight:500}
.window img{display:block;width:100%}
.caption{position:absolute;color:#f7f4ea;font-weight:600;font-size:26px;letter-spacing:-.01em;text-align:center;opacity:.92}
`;

const ICONS = `
<svg width="19" height="12" viewBox="0 0 19 12"><rect x="0" y="8" width="3.2" height="4" rx="1" fill="#fff"/><rect x="5" y="5.5" width="3.2" height="6.5" rx="1" fill="#fff"/><rect x="10" y="3" width="3.2" height="9" rx="1" fill="#fff"/><rect x="15" y="0" width="3.2" height="12" rx="1" fill="#fff"/></svg>
<svg width="17" height="12" viewBox="0 0 17 12"><path d="M8.5 11.5l2.4-2.9a3.6 3.6 0 0 0-4.8 0z" fill="#fff"/><path d="M3.9 6.9a6.8 6.8 0 0 1 9.2 0l1.5-1.8a9.2 9.2 0 0 0-12.2 0z" fill="#fff"/><path d="M1.2 3.6a10.9 10.9 0 0 1 14.6 0L17 2.1A13 13 0 0 0 0 2.1z" fill="#fff"/></svg>
<svg width="27" height="13" viewBox="0 0 27 13"><rect x=".5" y=".5" width="23" height="12" rx="3.8" fill="none" stroke="rgba(255,255,255,.45)"/><rect x="2.2" y="2.2" width="17.5" height="8.6" rx="2.4" fill="#fff"/><path d="M25 4.4v4.2a2.2 2.2 0 0 0 0-4.2z" fill="rgba(255,255,255,.45)"/></svg>`;

const windowHtml = (style) => `
<div class="window" style="${style}">
  <div class="bar"><i class="dot" style="background:#ff5f57"></i><i class="dot" style="background:#febc2e"></i><i class="dot" style="background:#28c840"></i>
    <span class="url"><svg width="10" height="12" viewBox="0 0 10 12"><rect x="1" y="5" width="8" height="6.5" rx="1.5" fill="currentColor"/><path d="M2.8 5V3.6a2.2 2.2 0 0 1 4.4 0V5" fill="none" stroke="currentColor" stroke-width="1.4"/></svg>homestead.lan</span></div>
  <img src="${images.desktop}" alt="">
</div>`;

const phoneHtml = (name, style = "") => `
<div class="phone" style="${style}">
  <i class="btn b1"></i><i class="btn b2"></i><i class="btn b3"></i><i class="btn b4"></i>
  <div class="screen" data-shot="${name}">
    <div class="status"><span>9:41</span><span class="icons">${ICONS}</span></div>
    <div class="island"></div>
    <img src="${images[name]}" alt="">
    <div class="home"></div><div class="glare"></div>
  </div>
</div>`;

// The status bar takes the colour of the top of the app beneath it, so the
// two read as one screen.
const matchStatusBars = () => Promise.all([...document.querySelectorAll(".screen")].map((screen) => new Promise((done) => {
  const img = screen.querySelector("img");
  const paint = () => {
    const canvas = document.createElement("canvas");
    canvas.width = img.naturalWidth; canvas.height = 4;
    const g = canvas.getContext("2d");
    g.drawImage(img, 0, 0);
    const [r, gr, b] = g.getImageData(Math.floor(img.naturalWidth / 2), 1, 1, 1).data;
    screen.style.background = `rgb(${r},${gr},${b})`;
    done();
  };
  img.complete ? paint() : img.addEventListener("load", paint, { once: true });
})));

const compose = async (width, height, body, file, scale = 1) => {
  const page = await browser.newPage({ viewport: { width, height }, deviceScaleFactor: scale });
  await page.setContent(`<html><head><style>${STYLE}</style></head><body>
    <div id="canvas" style="width:${width}px;height:${height}px">${body}</div></body></html>`, { waitUntil: "networkidle" });
  await page.evaluate(() => document.fonts.ready);
  await page.evaluate(matchStatusBars);
  await page.locator("#canvas").screenshot({ path: `${output}/${file}`, type: "jpeg", quality: QUALITY });
  await page.close();
  console.log(`composed ${file}`);
};

const has = (name) => Boolean(images[name]);
try {
  // The main image: the desktop dashboard in a browser window, and in front
  // of it two phones - networking behind, the dashboard nearest.
  if (has("dashboard") && images.desktop) {
    const W = 1600, H = 1000;
    let body = windowHtml("left:60px;top:96px;width:1130px;z-index:1");
    if (has("networking")) body += phoneHtml("networking", "left:938px;top:92px;transform:rotate(-5deg) scale(.74);z-index:2");
    body += phoneHtml("dashboard", "left:1150px;top:150px;transform:scale(.8);z-index:3");
    await compose(W, H, body, "homestead-hero.jpg", 1.25);
  }

  // The gallery: five screens in a row, each with its caption.
  const row = taken.filter((s) => s.name !== "nodes").slice(0, 5);
  if (row.length) {
    const gap = 60, scale = .78, w = 425 * scale, W = row.length * w + (row.length + 1) * gap, H = 884 * scale + 170;
    const body = row.map((s, i) => {
      const x = gap + i * (w + gap);
      return phoneHtml(s.name, `left:${x - (425 - w) / 2}px;top:${50 - (884 - 884 * scale) / 2}px;transform:scale(${scale})`)
        + `<div class="caption" style="left:${x}px;width:${w}px;top:${884 * scale + 78}px">${s.caption}</div>`;
    }).join("");
    await compose(Math.round(W), Math.round(H), body, "homestead-mobile.jpg");
  }

  // One phone per screen, for pages that show a single screen.
  for (const s of taken) {
    await compose(545, 1004, phoneHtml(s.name, "left:60px;top:60px"), `homestead-mobile-${s.name}.jpg`, 1.5);
  }
} catch (error) {
  missed.push(`compose: ${error.message.split("\n")[0]}`);
}

await browser.close();
if (missed.length) console.log(`skipped ${missed.length}:\n  ${missed.join("\n  ")}`);
