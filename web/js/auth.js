/* Sign-in gate, account controls, and the 401 handler that wraps every call */

let ME = null, ROLE = null;
// A response sent by an old session must not sign out a newly signed-in user.
let authGeneration = 0;
const RANK = { viewer: 0, operator: 1, admin: 2 };
window.can = need => RANK[ROLE] >= RANK[need];

/* Every mutating call carries this header. The cookie is SameSite=Strict, so a
   cross-site form cannot ride along; the header a cross-site form cannot set. */
const _rawFetch = window.fetch.bind(window);
window.fetch = async (url, opts = {}) => {
  if (typeof url !== "string" || !url.startsWith("/api/")) return _rawFetch(url, opts);
  if (opts.method && opts.method !== "GET") {
    opts.headers = Object.assign({ "X-Homestead-Auth": "1" }, opts.headers || {});
  }
  // Homestead's API never redirects. A redirect is a sign-in in front of it -
  // Cloudflare Access sending an expired session to Google - which a fetch
  // cannot follow: the browser blocks the other site, and every call failed
  // with a bare network error until the page was reloaded by hand.
  const r = await _rawFetch(url, { redirect: "manual", ...opts });
  if (r.type === "opaqueredirect") {
    signInLapsed();
    throw Object.assign(new Error("Your sign-in has expired; signing you in again"), { status: 0, signIn: true });
  }
  const served = r.headers?.get?.("X-Homestead-Version");
  if (served && typeof HOMESTEAD_VERSION === "string" && served !== HOMESTEAD_VERSION) homesteadUpdated(served);
  return r;
};

/* The Homestead answering is not the one this page was loaded from: it has
   been updated, and this page still runs the old one - its version, its
   update card, its code. Right after updating Homestead from this page it
   reloads by itself; otherwise (another tab, an update made elsewhere) it
   says so and offers to. During a rolling update the old copy may still
   answer in between, so it only ever moves forward to the new one. */
window.homesteadUpdated = served => {
  if (window.__homesteadUpdated === served) return;
  window.__homesteadUpdated = served;
  let mine = false;
  try { mine = Date.now() - (+sessionStorage.getItem("homestead.selfUpdate") || 0) < 30 * 60 * 1000; } catch (e) { /* no storage: ask */ }
  if (mine) {
    try { sessionStorage.removeItem("homestead.selfUpdate"); } catch (e) { /* it reloads anyway */ }
    setTimeout(() => location.reload(), 1500);
  }
  document.querySelector(".homestead-updated")?.remove();
  const bar = document.createElement("div");
  bar.className = "signin-lapsed homestead-updated";
  bar.setAttribute("role", "status");
  const version = String(served).replace(/[^0-9A-Za-z.+-]/g, "");
  bar.innerHTML = mine ? `<span class="spin2"></span><span>Homestead is now v${version}. Loading it…</span>`
    : `<span>Homestead was updated to v${version}.</span><button class="btn sm pri" type="button" onclick="location.reload()">Reload</button>`;
  document.body.appendChild(bar);
};
const _fetch = window.fetch;

/* The sign-in in front of Homestead has lapsed: reload the page, which is a
   navigation Cloudflare Access can send through Google and back. Once a
   minute at most, so a sign-in that keeps failing asks rather than loops. */
window.signInLapsed = () => {
  if (window.__signInLapsed) return;
  window.__signInLapsed = true;
  let last = 0;
  try { last = +sessionStorage.getItem("homestead.signInReload") || 0; } catch (e) { /* no storage: ask */ last = Date.now(); }
  const again = Date.now() - last > 60000;
  const bar = document.createElement("div");
  bar.className = "signin-lapsed";
  bar.setAttribute("role", "alert");
  bar.innerHTML = again ? '<span class="spin2"></span><span>Your sign-in has expired. Signing you in again…</span>'
    : '<span>Your sign-in has expired.</span><button class="btn sm pri" type="button" onclick="signInAgain()">Sign in</button>';
  document.body.appendChild(bar);
  if (again) setTimeout(signInAgain, 700);
};
window.signInAgain = () => {
  try { sessionStorage.setItem("homestead.signInReload", String(Date.now())); } catch (e) { /* it reloads anyway */ }
  location.replace(location.href);
};

/* The last answer from /api/auth/state, so Settings can describe this session
   without asking again. */
window.AUTH_STATE = {};
/* The namespace new apps go in: chosen when Homestead was installed. */
window.defaultNamespace = () => window.AUTH_STATE?.default_namespace || "lab";
async function authState() {
  try {
    const r = await _fetch("/api/auth/state");
    const body = await r.json();
    // The cluster did not answer: that is not "no accounts yet", and must
    // never be offered as first-time setup.
    if (!r.ok) return { unavailable: true, error: body.error || r.statusText, cause: body.cause || "" };
    window.AUTH_STATE = body;
    return window.AUTH_STATE;
  } catch (e) {
    if (e.signIn) return { signIn: true };
    return { unavailable: true, error: e.message };
  }
}

/* Homestead is up but its cluster is not answering: say so, and keep trying. */
function clusterUnavailable(error, cause = "") {
  gate(`<img class="mark" src="/assets/homestead-mark.svg?v=2.8.318" alt="">
    <h2>Homestead</h2><p class="sub">Waiting for the cluster</p>
    <div class="gateerr">${esc(error || "The Kubernetes API did not answer.")}</div>
    ${cause ? `<p class="dim xs gatecause"><b>Cause:</b> ${esc(cause)}</p>` : ""}
    <p class="dim small">This page tries again every few seconds.</p>
    <button class="btn wide" onclick="location.reload()">Try now</button>`);
  clearTimeout(window.__authRetry);
  window.__authRetry = setTimeout(boot, 5000);
}

/* While a paused data move holds every write, a signed-out admin can still
   sign in - the server writes nothing for it - and go on to the review. */
window.recoverySignIn = async () => {
  const err = document.getElementById("rg_err");
  try {
    const r = await fetch("/api/auth/login", { method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-Homestead-Auth": "1" },
      body: JSON.stringify({ username: document.getElementById("rg_user").value.trim(), password: document.getElementById("rg_pass").value }) });
    const out = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(out.error || "Sign-in failed");
    location.assign(window.__recoveryReview);
  } catch (e) { err.textContent = e.message; err.hidden = false; }
};

function dataHandoffStarting(state = {}) {
  if (state.recovery && /^[a-f0-9]{24}$/.test(state.operation || "")) {
    const review = window.__recoveryReview = `/api/self/data/handoff/${state.operation}/view`;
    gate(`<img class="mark" src="/assets/homestead-mark.svg" alt="">
      <h2>Data move paused</h2><p>Homestead is still on its original volume. No copy or shutdown has been authorized.</p>
      <p class="dim small">Review the preparation or keep using the original volume. Both volumes will be retained.</p>
      ${state.signed_in === false ? `<p class="small">Sign in as an administrator to review it.</p>
        <input id="rg_user" autocomplete="username" placeholder="Username">
        <input id="rg_pass" type="password" autocomplete="current-password" placeholder="Password" style="margin-top:8px"
          onkeydown="if (event.key === 'Enter') recoverySignIn()">
        <div class="gateerr" id="rg_err" hidden></div>
        <button class="btn wide" style="margin-top:10px" onclick="recoverySignIn()">Sign in and review</button>`
      : `<a class="btn wide" href="${review}">Review preparation</a>`}`);
    return;
  }
  gate(`<img class="mark" src="/assets/homestead-mark.svg" alt="">
    <h2>Checking the new data volume</h2>
    <p class="sub">Homestead is starting after its data move.</p>
    <p>Changes stay paused until the move coordinator confirms the restart. Both volumes are retained.</p>
    <p class="dim small">This page will continue automatically. Do not start the old copy.</p>`);
  clearTimeout(window.__authRetry);
  window.__authRetry = setTimeout(boot, 3000);
}

window.sessionSummary = (state = {}) => {
  if (!state.session_expires) return "";
  const days = Math.round((state.session_expires * 1000 - Date.now()) / 86400000);
  const hours = Math.round((state.session_expires * 1000 - Date.now()) / 3600000);
  const left = days >= 2 ? `${days} days` : `${Math.max(1, hours)} hour${hours === 1 ? "" : "s"}`;
  return (state.remember
    ? `Kept signed in on this device. This session lapses after ${left} unused`
    : `This session lapses after ${left} unused`)
    + `, and ends for good ${state.session_max_days} days after you signed in. `
    + "Signing out everywhere ends it now, on every device.";
};

window.signOutEverywhere = async () => {
  if (!(await ask("Sign out of every device, including this one?" + String.fromCharCode(10, 10)
      + "Every session for your account stops working immediately."))) return;
  try {
    await _fetch("/api/auth/signout-everywhere", { method: "POST",
      headers: { "Content-Type": "application/json", "X-Homestead-Auth": "1" } });
  } catch (e) { }
  location.reload();
};

function gate(html) {
  $("#gatebox").innerHTML = html;
  $("#gate").classList.remove("hidden");
}
function ungate() {
  authGeneration++;
  clearTimeout(window.__authRetry);
  $("#gate").classList.add("hidden");
}

function stopAuthenticatedWork() {
  window.Dashboard?.invalidate();
  window.stopAlertChecks?.();
  authGeneration++;
  ME = null; ROLE = null;
  clearInterval(window.__loopTimer);
  clearInterval(window.__imageUpdateLoop);
  clearTimeout(window.__imageUpdateStart);
  clearTimeout(window.__operationTimer);
}

function loginForm(err, setup) {
  gate(`
    <img class="mark" src="/assets/homestead-mark.svg?v=2.8.318" alt="">
    <h2>${setup ? "Set up Homestead" : "Homestead"}</h2>
    <p class="sub">${setup ? "Create the first administrator account" : "Sign in to continue"}</p>
    ${err ? `<div class="gateerr">${esc(err)}</div>` : ""}
    <div class="f"><label>Username</label>
      <input type="text" id="lg_user" autocomplete="username" autocapitalize="none" spellcheck="false"></div>
    <div class="f"><label>Password</label>
      <input type="password" id="lg_pass" autocomplete="${setup ? "new-password" : "current-password"}"></div>
    ${setup ? `<div class="f"><label>Confirm password</label>
      <input type="password" id="lg_pass2" autocomplete="new-password"></div>` : ""}
    <label class="switch gateremember"><input type="checkbox" id="lg_remember"
      ${localStorage.getItem("homestead.remember") === "0" ? "" : "checked"}>Keep me signed in on this device</label>
    <button class="btn pri wide" id="lg_go">${setup ? "Create account" : "Sign in"}</button>
    ${setup ? `<div class="gatehint">Minimum 10 characters. Stored as PBKDF2-SHA256 with a
      per-user salt in a Kubernetes Secret — never in plain text.</div>`
      : `<div class="gatehint">Homestead can deploy, move and delete workloads.<br>
        A session stays signed in while you keep using it. Leave the box unticked on a
        shared computer.</div>`}`);
  const go = () => setup ? doSetup() : doLogin();
  $("#lg_go").onclick = go;
  ["lg_user", "lg_pass", "lg_pass2"].forEach(id => {
    const el = $("#" + id);
    if (el) el.addEventListener("keydown", e => { if (e.key === "Enter") go(); });
  });
  const username = $("#lg_user");
  setTimeout(() => {
    if ($("#lg_user") === username && !$("#gate").classList.contains("hidden") &&
        !$("#gatebox").contains(document.activeElement)) username.focus();
  }, 60);
}

async function doLogin() {
  const username = $("#lg_user").value.trim(), password = $("#lg_pass").value;
  if (!username || !password) return loginForm("Enter a username and password");
  const remember = !!$("#lg_remember")?.checked;
  // Remembered for next time, so the box comes back the way it was left.
  try { localStorage.setItem("homestead.remember", remember ? "1" : "0"); } catch (e) { }
  $("#lg_go").textContent = "Signing in…"; $("#lg_go").disabled = true;
  try {
    const r = await _fetch("/api/auth/login", { method: "POST",
      headers: { "Content-Type": "application/json", "X-Homestead-Auth": "1" },
      body: JSON.stringify({ username, password, remember }) });
    const b = await r.json();
    if (!r.ok) return loginForm(b.error || "Sign-in failed");
    ME = b.user; ROLE = b.role || "admin"; ungate(); afterAuth();
  } catch (e) { loginForm(e.message); }
}

async function doSetup() {
  const username = $("#lg_user").value.trim(), password = $("#lg_pass").value,
        confirm = $("#lg_pass2").value;
  if (password !== confirm) return loginForm("Passwords do not match", true);
  if (password.length < 10) return loginForm("Password must be at least 10 characters", true);
  $("#lg_go").textContent = "Creating…"; $("#lg_go").disabled = true;
  try {
    const r = await _fetch("/api/auth/setup", { method: "POST",
      headers: { "Content-Type": "application/json", "X-Homestead-Auth": "1" },
      body: JSON.stringify({ username, password, remember: !!$("#lg_remember")?.checked }) });
    const b = await r.json();
    if (!r.ok) return loginForm(b.error || "Setup failed", true);
    ME = b.user; ROLE = "admin"; ungate(); afterAuth();
  } catch (e) { loginForm(e.message, true); }
}

window.doLogout = async () => {
  try { await pwaForgetDevice(); } catch (e) { }
  try { await fetch("/api/auth/logout", { method: "POST" }); } catch (e) { }
  stopAuthenticatedWork();
  loginForm();
};

window.pwChange = () => modal("Change password", `
  <div class="f"><label>Current password</label><input type="password" id="pw_old" autocomplete="current-password"></div>
  <div class="f"><label>New password</label><input type="password" id="pw_new" autocomplete="new-password"></div>
  <div class="f"><label>Confirm new password</label><input type="password" id="pw_new2" autocomplete="new-password"></div>
  ${UI.actions(`<button class="btn pri" onclick="doPwChange()">Change password</button>
    <button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>`)}
  <div class="note" style="margin-top:14px">Changing your password signs out every other
  session, including on other devices.</div>`);

window.doPwChange = async () => {
  const a = $("#pw_new").value, b = $("#pw_new2").value;
  if (a !== b) return toast("new passwords do not match", "bad");
  try {
    await api("/api/auth/password", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ old: $("#pw_old").value, new: a }) });
    toast("password changed — other sessions signed out", "ok"); closeModal();
  } catch (e) { toast(e.message, "bad"); }
};

/* One person's sign-ins, under Events. */
window.userSignins = name => {
  closeModal();
  STATE.eventsTab = "signins";
  STATE.q = name;
  const search = $("#globalSearch");
  if (search) search.value = name;
  go("events", { keepSearch: true });
};

window.manageUsers = async () => {
  modal("Users", `<div class="empty"><span class="spin2"></span>loading</div>`);
  try {
    const us = await api("/api/auth/users");
    $("#mbody").innerHTML = `
      <div class="card flat pad0" style="margin-bottom:16px"><div class="tblwrap"><table class="tbl stack">
        <thead><tr><th>User</th><th>Role</th><th>Last sign-in</th><th></th></tr></thead><tbody>
        ${us.map(u => `<tr><td><div class="row" style="gap:9px">
            <div class="av">${esc(u.name.slice(0, 2).toUpperCase())}</div><b>${esc(u.name)}</b>
            ${u.name === ME ? '<span class="tag ok">you</span>' : ""}</div></td>
          <td><select onchange="setRole(${jsq(u.name)},this.value)" ${u.name === ME ? "disabled" : ""}
              style="padding:5px 9px;font-size:12px;width:auto">
            ${["viewer", "operator", "admin"].map(r =>
              `<option value="${r}" ${u.role === r ? "selected" : ""}>${r}</option>`).join("")}
          </select></td>
          <td class="dim small mono">${esc(u.last_login || "never")}
            <div><a class="linkish xs" onclick="userSignins(${jsq(u.name)})">Sign-in history</a></div></td>
          <td>${u.name === ME || us.length === 1 ? '<span class="dim xs">—</span>'
            : actionBar([{ label: "Remove", run: `delUser(${jsq(u.name)})`, danger: true }])}</td>
        </tr>`).join("")}</tbody></table></div></div>
      <div class="sec">Add a user</div>
      <div class="f2">
        <div class="f"><label>Username</label><input type="text" id="nu_user" autocapitalize="none"></div>
        <div class="f"><label>Password</label><input type="password" id="nu_pass" autocomplete="new-password"></div>
      </div>
      <div class="f"><label>Role</label><select id="nu_role">
        <option value="viewer">viewer — read only</option>
        <option value="operator" selected>operator — manage workloads</option>
        <option value="admin">admin — everything, including hosts and import</option>
      </select></div>
      <button class="btn pri" onclick="addUser()">Add user</button>
      <div class="note" style="margin-top:16px"><b>viewer</b> can look but not touch.
      <b>operator</b> can deploy, edit, move, start and stop workloads and VMs.
      <b>admin</b> adds user management, host cordon/drain/power, shares, and import —
      which stores credentials for other machines.</div>`;
  } catch (e) { $("#mbody").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
};
window.addUser = async () => {
  try {
    await api("/api/auth/users", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: $("#nu_user").value.trim(), password: $("#nu_pass").value,
        role: $("#nu_role").value }) });
    toast("user added", "ok"); manageUsers();
  } catch (e) { toast(e.message, "bad"); }
};
window.delUser = async name => {
  if (!(await ask(`Remove user "${name}"?`))) return;
  try {
    await api("/api/auth/users/delete", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: name }) });
    toast("removed", "ok"); manageUsers();
  } catch (e) { toast(e.message, "bad"); }
};

/* any 401 anywhere drops straight back to the sign-in gate */
const _api = window.api;
window.api = async (path, opts) => {
  const generation = authGeneration;
  try { return await _api(path, opts); }
  catch (e) {
    if (/not signed in/i.test(e.message)) {
      if (ME && generation === authGeneration) {
        stopAuthenticatedWork();
        loginForm("Session expired — sign in again");
      }
    }
    else if (/cannot do this/i.test(e.message)) toast(e.message, "bad");
    throw e;
  }
};

/* A role is not a state, so it is not coloured: the word says which. */
function roleClass(r) {
  return "pill neutral";
}
function paintWho() {
  if (!ME) return;
  $("#whonm").textContent = ME;
  $("#whoav").textContent = ME.slice(0, 2).toUpperCase();
  const wr = $("#whorole");
  if (wr) { wr.textContent = ROLE; wr.className = roleClass(ROLE) + " rolechip"; }
  const su = $("#setUser"); if (su) su.textContent = ME;
  const sr = $("#setRole");
  if (sr) { sr.textContent = ROLE; sr.className = roleClass(ROLE) + " rolechip"; }
  document.body.dataset.role = ROLE;
  // hide anything the signed-in role cannot use. The server enforces it too;
  // this only keeps the UI honest.
  $$("[data-need]").forEach(el => el.classList.toggle("hidden", !can(el.dataset.need)));
}
window.applyRole = paintWho;
$("#whoami").onclick = () => go("settings");

/* boot: decide between setup, sign-in, and running the app */
async function boot() {
  clearTimeout(window.__authRetry);
  const st = await authState();
  if (st.signIn) return;         // signInLapsed is taking the page through sign-in
  if (st.unavailable) return clusterUnavailable(st.error, st.cause);
  if (st.data_handoff) return dataHandoffStarting(st);
  if (st.setup) return loginForm(null, true);
  if (!st.user) return loginForm();
  ME = st.user; ROLE = st.role || "admin"; ungate(); afterAuth();
}
boot();

async function afterAuth() {
  const generation = authGeneration;
  paintWho();
  if (window.setupOffer) setupOffer();
  await Promise.all([loadHealthSettings(), window.loadPlatform ? loadPlatform() : null,
    window.fleetLoad ? fleetLoad() : null]);
  if (!ME || generation !== authGeneration) return;
  const route = HomesteadRouter.resolve(window.location.pathname);
  if (!route.known) {
    // A mistyped or outdated address: land on the dashboard, and say so.
    const asked = window.location.pathname;
    window.history.replaceState({ view: "dash" }, "", HomesteadRouter.urlFor("dash"));
    toast(`There is no page at ${asked}, so here is the dashboard`, "warn");
    go("dash", { history: false });
  } else {
    go(route.view, { history: false, fromLocation: true });
  }
  startLoop();
  if (window.startOperationChecks) window.startOperationChecks();
  if (window.startAlertChecks) window.startAlertChecks();
  if (window.startUpdateChecks) window.startUpdateChecks();
  // Load this user's guide reminder choice without opening or replacing a page.
  if (window.welcomeCheck) setTimeout(() => welcomeCheck(), 900);
}

/* Settings › Users and access (#350): a role changed where it is shown, and a
   user added from the card. Manage users stays for doing several at once. */
window.userRoleSet = async (name, select) => {
  const role = select.value, was = select.dataset.was;
  select.disabled = true;
  try {
    await api("/api/auth/role", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: name, role }) });
    select.dataset.was = role;
    select.closest("tr")?.setAttribute("data-role", role);
    toast(`${name} is now ${role}`, "ok");
  } catch (e) { select.value = was; toast(e.message, "bad"); }
  finally { select.disabled = false; }
};

window.userAdd = () => modal("Add a user", `<div class="ui-stack">
  ${UI.fields(UI.field("Username", '<input type="text" id="nu_user" autocapitalize="none" autocomplete="off">'),
    UI.field("Password", '<input type="password" id="nu_pass" autocomplete="new-password">'))}
  ${UI.field("Role", `<select id="nu_role">${["viewer", "operator", "admin"].map(r =>
    `<option value="${r}"${r === "operator" ? " selected" : ""}>${typeof ROLE_NAMES === "object" ? ROLE_NAMES[r] : r} - ${typeof ROLE_SHORT === "object" ? esc(ROLE_SHORT[r]) : ""}</option>`).join("")}</select>`,
    { help: "Compare roles on the card says what each can do." })}
  ${UI.actions(UI.cancel() + UI.button("Add user", "userAddSave(this)", { kind: "pri" }))}</div>`);

window.userAddSave = async button => {
  if (button) button.disabled = true;
  try {
    await api("/api/auth/users", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: $("#nu_user").value.trim(), password: $("#nu_pass").value, role: $("#nu_role").value }) });
    toast("User added", "ok"); closeModal();
    if (STATE.view === "settings") { resetPaint(); viewSettings(); }
  } catch (e) { toast(e.message, "bad"); if (button) button.disabled = false; }
};

window.setRole = async (name, role) => {
  try {
    await api("/api/auth/role", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: name, role }) });
    toast(name + " is now " + role, "ok"); manageUsers();
  } catch (e) { toast(e.message, "bad"); manageUsers(); }
};

/* ---------------- API keys ----------------
   Settings › Users and access: keys for Home Assistant, scripts and AI
   agents. Each expires, holds only the scopes it is given and works on
   /api/v1 alone; the token is shown once, when it is made. */
const API_DOCS = "https://github.com/homestead-lab/homestead/wiki/API";
const API_TTLS = [["1 day", 86400], ["7 days", 7 * 86400], ["30 days", 30 * 86400], ["90 days", 90 * 86400], ["1 year", 365 * 86400]];
const keyUntil = t => {
  const left = t - Date.now() / 1000;
  if (left <= 0) return "expired";
  const days = Math.floor(left / 86400), hours = Math.floor(left / 3600);
  return days >= 2 ? `expires in ${days} days` : hours >= 2 ? `expires in ${hours} hours` : "expires within 2 hours";
};

window.apiKeysPaint = async () => {
  const card = $("#apiKeysCard");
  if (!card || !can("admin")) return;
  let found;
  try { found = await api("/api/auth/keys"); }
  catch (e) { card.innerHTML = `<div class="ctitle">API keys</div><div class="dim small">${esc(e.message)}</div>`; return; }
  STATE.data.apiKeys = found;
  // A list of like things: a stacked table, its buttons an actionBar.
  const rows = found.keys.map(k => `<tr>
      <td><b>${esc(k.name)}</b> ${k.expired ? '<span class="tag bad">expired</span>' : ""}<div class="dim xs">made by ${esc(k.owner)}${k.networks.length ? ` · only from ${esc(k.networks.join(", "))}` : ""}</div></td>
      <td data-label="May">${k.scopes.map(s => `<span class="tag">${esc(s)}</span>`).join(" ")}</td>
      <td data-label="Expires" class="small">${esc(keyUntil(k.expires))}</td>
      <td data-label="Last used" class="small">${k.last_used ? `${esc(agoText(k.last_used))}${k.last_ip ? `<div class="dim xs mono">${esc(k.last_ip)}</div>` : ""}` : '<span class="dim">never</span>'}</td>
      <td>${actionBar([{ label: "Revoke", run: `apiKeyRevoke(${jsq(k.id)},${jsq(k.name)})`, danger: true }], { shown: 1 })}</td></tr>`).join("");
  card.innerHTML = `${UI.moduleHeader(`API keys`, `For Home Assistant, scripts and AI agents. Each key expires, can do only what it is given, and works only on the API`, `<a class="btn sm" href="${API_DOCS}" target="_blank" rel="noopener">API guide</a>
        <button class="btn sm pri" onclick="apiKeyNew()">＋ New key</button>`)}
    ${rows ? `<div class="tblwrap"><table class="tbl stack dense"><thead><tr><th>Key</th><th>May</th><th>Expires</th><th>Last used</th><th></th></tr></thead>
      <tbody>${rows}</tbody></table></div>` : '<div class="dim small">No keys yet.</div>'}
    <div class="dim xs" style="margin-top:8px">A key cannot manage users or keys, reach a host's shell, or change settings, whatever its scopes.
      Its full description is at <span class="mono">/api/v1/openapi.json</span> on this Homestead.</div>`;
};

window.apiKeyNew = () => {
  const scopes = STATE.data.apiKeys?.scopes || {};
  modal("New API key", UI.lead("A key can do only what its scopes allow, and stops working when it expires or is revoked. You see it once.")
    + UI.field("Name", '<input id="ak_name" placeholder="Home Assistant" maxlength="60">', { help: "What uses it, so you know which to revoke." })
    + `<div class="sec">What it may do</div>`
    + Object.entries(scopes).map(([scope, text]) => settingRow(`<span class="mono">${esc(scope)}</span>`, esc(text),
      `<label class="toggle"><input type="checkbox" class="ak-scope" value="${esc(scope)}" ${scope === "read" ? "checked" : ""}><span></span></label>`)).join("")
    + UI.fields(UI.field("Expires after", `<select id="ak_ttl">${API_TTLS.map(([label, s]) => `<option value="${s}" ${s === 90 * 86400 ? "selected" : ""}>${label}</option>`).join("")}</select>`),
      UI.field("Only from (optional)", '<input id="ak_nets" placeholder="192.0.2.20, 198.51.100.0/24">', { help: "Addresses or networks it may be used from; anywhere if empty. Homestead sees the address a request arrives from - behind the cluster's load balancer that can be a node's - so check \"used from\" on the card first." }))
    + UI.actions(UI.cancel() + UI.button("Make key", "apiKeyMake(this)", { kind: "pri" })));
};

window.apiKeyMake = async button => {
  const body = { name: $("#ak_name").value.trim(), scopes: $$(".ak-scope").filter(c => c.checked).map(c => c.value),
    ttl_seconds: +$("#ak_ttl").value, networks: $("#ak_nets").value.split(/[\s,]+/).filter(Boolean) };
  if (!body.name) return toast("give the key a name", "bad");
  if (!body.scopes.length) return toast("choose at least one thing it may do", "bad");
  button.disabled = true;
  let made;
  try {
    made = await api("/api/auth/keys", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  } catch (e) { button.disabled = false; return toast(e.message, "bad"); }
  const example = `curl -H "Authorization: Bearer ${made.token}" ${location.origin}/api/v1/status`;
  window.__apiToken = made.token;
  modal(`Key for ${made.key.name}`, UI.callout("warn", "Copy it now: it is not shown again.", "Homestead keeps only a hash of it. If it is lost, revoke it and make another.")
    + `<div class="f"><label>The key</label><div class="row" style="gap:8px;flex-wrap:nowrap"><input class="mono" readonly value="${esc(made.token)}" onclick="this.select()">
      <button class="btn" onclick="copyText(window.__apiToken).then(ok => toast(ok ? 'Copied' : 'Select it and copy', ok ? 'ok' : 'bad'))">Copy</button></div></div>`
    + UI.field("Try it", `<pre class="mono small" style="white-space:pre-wrap;word-break:break-all;margin:0">${esc(example)}</pre>`)
    + UI.more("Using it from Home Assistant", `<p>A REST sensor, for example, in configuration.yaml:</p>
      <pre class="mono small" style="white-space:pre-wrap">sensor:
  - platform: rest
    name: Homestead alerts
    resource: ${esc(location.origin)}/api/v1/status
    headers:
      Authorization: !secret homestead_api_key
    value_template: "{{ value_json.alerts.active }}"</pre>
      <p>with <span class="mono">homestead_api_key: "Bearer hsk_…"</span> in secrets.yaml. The <a href="${API_DOCS}" target="_blank" rel="noopener">API guide</a> has more.</p>`)
    + UI.actions(UI.button("Done", "window.__apiToken='';closeModal();apiKeysPaint()", { kind: "pri" })));
};

window.apiKeyRevoke = async (id, name) => {
  if (!(await ask(`Revoke ${name}? Anything using it is refused from now on.`, { danger: true, ok: "Revoke" }))) return;
  try {
    await api("/api/auth/keys/revoke", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id }) });
    toast(`${name} revoked`, "ok");
    apiKeysPaint();
  } catch (e) { toast(e.message, "bad"); }
};
