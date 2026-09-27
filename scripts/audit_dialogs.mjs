// Every dialog, opened against the demo data at a desktop and a phone width,
// captured whole and measured against the design rules in docs/design.md.
//
//   node scripts/audit_dialogs.mjs            all dialogs
//   node scripts/audit_dialogs.mjs vm         only those whose name has "vm"
//
// Writes release-assets/dialogs/<name>-<width>.png, and report.json with each
// dialog's measurements. It exits non-zero when a dialog breaks a rule that
// can be measured: content wider than the dialog, text too small to read, a
// dialog that could not be opened. CI runs it, so a dialog cannot regress.
import { chromium } from "playwright";
import { mkdir, writeFile } from "node:fs/promises";

const output = "release-assets/dialogs";
await mkdir(output, { recursive: true });
const base = (process.env.HOMESTEAD_URL || "http://127.0.0.1:4173") + "/?demo=1";
const only = process.argv[2] || "";
const theme = process.env.HOMESTEAD_AUDIT_THEME === "light" ? "light" : "dark";

// name, the page to open first, then the steps: JavaScript to run, or
// "click:<fn>" (the first control whose onclick calls fn), or "text:<label>"
// (the first button in the dialog with that label).
const DIALOGS = [
  ["self-data-prepare", "settings", "window.__demoDataPrepared=false;replicasMoveData()", "selfDataPrepareReview()"],
  ["self-data-final-review", "settings", "window.__demoDataPrepared=true;replicasMoveData('demo-data-prepare')", "selfDataFinalReview()"],
  ["change-password", "dash", "pwChange()"],
  ["storage-recovery-ready", "storage", "window.__demoStorageState='ready';storageRecoveryReview('op4')"],
  ["storage-recovery-running", "storage", "window.__demoStorageState='running';storageRecoveryReview('op4')"],
  ["storage-recovery-uncertain", "storage", "window.__demoStorageState='uncertain';storageRecoveryReview('op4')"],
  ["users", "settings", "manageUsers()"],
  ["compose-import", "workloads", "composeImport()"],
  ["move-workload", "workloads", "moveWorkload('frigate','lab')"],
  ["add-harvester-host", "cluster", "clusterOnboarding()"],
  ["remove-host", "cluster", "clusterRemovePick()"],
  ["add-host", "cluster", "platformJoinGuide()"],
  ["helm-release", "helm", "click:helmRelease"],
  ["helm-install", "helm", "helmInstall()"],
  ["ip-edit", "network", "ipamEdit()"],
  ["ip-subnets", "network", "ipamSubnets()"],
  ["ip-unifi", "network", "ipamUnifi()"],
  ["ip-import", "network", "ipamImport()"],
  ["workload-edit", "workloads", "wlEdit('lab','frigate')"],
  ["workload-edit-review", "workloads", "wlEdit('lab','frigate')", "text:Save"],
  ["workload-storage-copy", "workloads", "wlEdit('lab','frigate')", "editReview({ns:'lab',name:'frigate',containers:[{name:'frigate',volumes:[{path:'/media/frigate',source:'camera-data',sub_path:'recordings',copy_from:{claim:'frigate-recordings'}}]}]})"],
  ["vm-migrate", "vms", "vmMove('default','home-assistant-os')"],
  ["vm-new", "vms", "vmNew()"],
  ["vm-image-delete", "images", "click:vmImageDelete"],
  ["schedule-new", "schedules", "jobEdit()"],
  ["vm-disk-import", "imports", "vmDiskImport()"],
  ["import-source-add", "imports", "srcAdd()"],
  ["import-source-verify", "imports", "window.__demoSourceKeyChanged=false;srcVerify('unraid')"],
  ["import-source-key-changed", "imports", "window.__demoSourceKeyChanged=true;srcVerify('unraid')"],
  ["import-configure", "imports", "inspectImport('unraid','media-server')"],
  ["import-recovery", "imports", "window.__demoOps=[...(window.__demoOps||[]).filter(x=>x.id!=='import-recovery'),{id:'import-recovery',kind:'import-create',mutation_recovery:true,resource:{namespace:'lab',name:'photos'}}];powerRecoveryReview('import-recovery','import')"],
  ["import-recovery-active", "imports", "window.__demoOps=[...(window.__demoOps||[]).filter(x=>x.id!=='import-active'),{id:'import-active',kind:'import-create',mutation_recovery:true,demo_dispatching:true,resource:{namespace:'lab',name:'photos'}}];powerRecoveryReview('import-active','import')"],
  ["import-job-remove", "imports", "importRemove('homestead-import-photos','done')"],
  ["cluster-add", "imports", "clusterAdd()"],
  ["vip-add", "network", "vipAdd()"],
  ["node-detail", "nodes", "nodeDetail('harvester-node1')"],
  ["smart-disk", "nodes", "smartDisk('harvester-node1','nvme0n1')"],
  ["probe-install", "nodes", "probeInstallConfirm()"],
  ["vm-placement-checks", "nodes", "allocationProbeSettings()"],
  ["hardware-features", "nodes", "hardwareFeatureSettings()"],
  ["portal-edit", "portal", "portalEdit()"],
  ["backup-storage-setup", "protect", "objectStoreSetup()"],
  ["protect-job-new", "protect", "lhJob()"],
  ["protect-job-edit", "protect", "click:lhJob({"],
  ["protection-plans", "protect", "lhPlans()"],
  ["protect-group", "protect", "lhGroup()"],
  ["protect-covered", "protect", "click:lhCovered"],
  ["protect-assign", "protect", "click:lhAssign"],
  ["snapshots", "storage", "lhSnaps('pvc-demo-frigate','frigate-config')"],
  ["backups", "storage", "lhBackupList('pvc-demo-frigate','frigate-config')"],
  ["backup-restore", "storage", "lhBackupList('pvc-demo-frigate','frigate-config')", "click:lhRestore"],
  ["backup-target", "protect", "lhTarget()"],
  ["resource-open", "resources", "click:resOpen"],
  ["resource-create", "resources", "resCreate()"],
  ["samba-remove", "settings", "sambaRemove()"],
  ["nfs-remove", "settings", "nfsRemove()"],
  ["volume-create", "storage", "volumeCreate()"],
  ["volume-edit", "storage", "click:volumeEdit"],
  ["volume-class-review", "storage", "volumeReclass({name:'frigate-config',pvc_name:'frigate-config',namespace:'lab',storage_class:'longhorn-r2'})"],
  ["volume-delete", "storage", "click:volumeDelete"],
  ["storage-class-new", "storage", "storageClassCreate()"],
  ["volume-files", "storage", "click:volumeFiles"],
  ["volume-ownership", "storage", "volumeChown('lab','frigate-config')"],
  ["share-new", "shares", "newShare()"],
  ["share-new-user", "shares", "newShare()", "document.querySelector('#sh_identity').value='';shareAccountHint()"],
  ["share-users", "shares", "smbUsers()"],
  ["share-user-add", "shares", "smbUsers()", "smbUserEdit()"],
  ["share-user-password", "shares", "smbUsers()", "smbUserEdit('lab')"],
  ["share-user-remove", "shares", "smbUserRemove('backup')"],
  ["disks", "storage", "lhDisks()"],
  ["disk-add", "storage", "diskAdd('harvester-node1')"],
  ["vm-image-store", "vms", "vmStore()"],
  ["vm-open", "vms", "vmOpen('default','home-assistant-os')"],
  ["vm-edit", "vms", "vmEdit('default','home-assistant-os')"],
  ["vm-delete", "vms", "vmDelete('default','home-assistant-os')"],
  ["k3s-cluster", "vms", "k3sCluster()"],
  ["image-updates", "workloads", "imageUpdateCenter()"],
  ["image-update-review", "workloads", "imageUpdateReview('lab','frigate')"],
  ["image-update-batch", "workloads", "imageUpdateCenter()", "text:Stage selected"],
  ["workload-group", "workloads", "wlGroup('lab','frigate')"],
  ["workload-groups", "workloads", "manageWorkloadGroups()"],
  ["workload-start-capacity", "workloads", "wlScale('lab','doublecommander',1)"],
  ["stop-homestead", "workloads", "wlStopSelf('lab','homestead')"],
  ["failover", "workloads", "wlFailover()"],
  ["workload-delete", "workloads", "wlDelete('lab','frigate')"],
  ["workload-logs", "workloads", "click:wlLogs"],
  ["workload-console", "workloads", "wlConsole('lab','frigate')"],
  ["workload-main-port", "workloads", "wlPrimaryPort('lab','frigate')"],
  ["node-reboot", "nodes", "nodePowerReview('harvester-node1','reboot')"],
  ["app-store-app", "store", "click:storeDetails"],
  ["deploy-preview", "deploy", "click:previewYaml"],
  ["fleet-manage", "workloads", "fleetManage()"],
  ["fleet-link", "workloads", "fleetLink()"],
  ["move-to-cluster", "workloads", "moveToCluster('container','frigate')"],
];

const report = [];
const failures = [];
const browser = await chromium.launch({ headless: true });

// Each width's list is shared between a few tabs, all at once: one tab
// working through every item in turn took most of CI's time.
const WORKERS = Number(process.env.AUDIT_WORKERS || 4);
const WIDTHS = [["desktop", 1440, 900, false], ["mobile", 390, 844, true]];
const todo = DIALOGS.filter(([name]) => !only || name.includes(only));
const shares = Array.from({ length: WORKERS }, (_, i) => todo.filter((_, j) => j % WORKERS === i)).filter((share) => share.length);
await Promise.all(WIDTHS.flatMap((width) => shares.map((share) => audit(width, share))));
await browser.close();
// Back in list order, whichever tab finished first.
const order = (r) => todo.findIndex(([name]) => name === r.name) * 2 + (r.label === "mobile");
report.sort((a, b) => order(a) - order(b));

async function audit([label, width, height, mobile], items) {
  const context = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: mobile ? 2 : 1,
    isMobile: mobile, hasTouch: mobile, colorScheme: "dark" });
  const page = await context.newPage();
  // A dialog may ask the browser to confirm first (a volume in use, say): yes.
  page.on("dialog", (dialog) => dialog.accept());
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.addInitScript(theme => {
    localStorage.setItem("homestead.settings", JSON.stringify({ theme, bg: "soft", blur: 26, motion: "off", refresh: 60 }));
  }, theme);
  await page.goto(base, { waitUntil: "networkidle" });
  await page.locator("#views .phead").waitFor();
  await page.evaluate(() => document.fonts.ready);
  await page.addStyleTag({ content: "#jobTray{display:none!important}" });

  for (const [name, view, ...steps] of items) {
    try {
      await page.evaluate(() => { try { closeModal(); } catch (e) { /* none open */ } });
      await page.evaluate((v) => document.querySelector(`#nav a[data-view="${v}"]`)?.click(), view);
      await page.locator("#views .phead").waitFor();
      await page.waitForTimeout(700);
      for (const step of steps) {
        if (step.startsWith("click:")) {
          const fn = step.slice(6);
          const found = await page.evaluate((fn) => {
            const el = [...document.querySelectorAll("[onclick]")].find((e) => e.getAttribute("onclick").includes(fn.includes("(") ? fn : `${fn}(`));
            if (!el) return false;
            el.click(); return true;
          }, fn);
          if (!found) throw new Error(`no control calls ${fn}`);
        } else if (step.startsWith("text:")) {
          const text = step.slice(5);
          const found = await page.evaluate((text) => {
            const el = [...document.querySelectorAll("#modal button")].find((b) => b.textContent.trim().startsWith(text));
            if (!el) return false;
            el.click(); return true;
          }, text);
          if (!found) throw new Error(`no "${text}" button`);
        } else {
          await page.evaluate(step);
        }
        await page.waitForTimeout(900);
      }
      await page.locator("#modal:not(.hidden) #mbody").waitFor({ timeout: 4000 });
      await page.waitForTimeout(600);
      const metrics = await page.evaluate(() => {
        const box = document.querySelector(".modalbox"), body = document.querySelector("#mbody");
        const boxRect = box.getBoundingClientRect();
        const wide = [...body.querySelectorAll("*")].filter((e) => {
          const r = e.getBoundingClientRect();
          return r.width > 0 && r.right > boxRect.right + 1 && getComputedStyle(e).position !== "fixed"
            && !e.closest("pre, code, .monaco-editor, .xterm, table, .tablewrap, [data-scroll-x]");
        }).map((e) => `${e.tagName.toLowerCase()}.${[...e.classList].join(".")}`);
        const texts = [...body.querySelectorAll("*")].filter((e) => [...e.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim()));
        const tiny = texts.filter((e) => parseFloat(getComputedStyle(e).fontSize) < 10 && e.offsetParent)
          .map((e) => `${e.tagName.toLowerCase()}.${[...e.classList].join(".")} ${getComputedStyle(e).fontSize}`);
        return {
          title: document.querySelector("#mtitle").textContent,
          width: Math.round(boxRect.width), height: Math.round(box.scrollHeight),
          notes: body.querySelectorAll(".note").length,
          buttons: body.querySelectorAll("button, .btn").length,
          checkboxes: body.querySelectorAll("input[type=checkbox]").length,
          words: body.innerText.split(/\s+/).filter(Boolean).length,
          overflow: [...new Set(wide)].slice(0, 6),
          tiny: [...new Set(tiny)].slice(0, 6),
        };
      });
      // The whole dialog, not only what fits on the screen - with the page
      // behind it hidden, so nothing shows through where it runs long.
      await page.addStyleTag({ content: ".modalbox{max-height:none!important;overflow:visible!important}"
        + "#modal{overflow:visible!important;position:absolute!important;inset:0 0 auto 0!important;display:block!important;background:#060607!important}"
        + "body>*:not(#modal){visibility:hidden!important}.ui-actions{position:static!important}" });
      await page.locator(".modalbox").screenshot({ path: `${output}/${name}-${label}.png` });
      await page.evaluate(() => document.querySelectorAll("style").forEach((s) => { if (s.textContent.startsWith(".modalbox{max-height:none")) s.remove(); }));
      report.push({ name, label, ...metrics });
      if (metrics.overflow.length) failures.push(`${name} (${label}): content wider than the dialog: ${metrics.overflow.join(", ")}`);
      if (metrics.tiny.length) failures.push(`${name} (${label}): text under 10px: ${metrics.tiny.join(", ")}`);
    } catch (error) {
      const toastText = await page.evaluate(() => [...document.querySelectorAll("#toast .tst")].map((t) => t.textContent.trim()).join(" | ")).catch(() => "");
      failures.push(`${name} (${label}): could not open: ${error.message.split("\n")[0]}${toastText ? ` - toast: ${toastText}` : ""}${errors.length ? ` - error: ${errors.at(-1)}` : ""}`);
    }
  }
  await context.close();
}

await writeFile(`${output}/report.json`, JSON.stringify(report, null, 2));
for (const r of report) {
  console.log(`${r.name.padEnd(26)} ${r.label.padEnd(8)} ${String(r.height).padStart(5)}px ${String(r.words).padStart(4)} words ${r.notes} notes ${r.buttons} buttons ${r.checkboxes} checks`);
}
if (failures.length) {
  console.log(`\n${failures.length} problem(s):\n  ${failures.join("\n  ")}`);
  process.exitCode = 1;
}
