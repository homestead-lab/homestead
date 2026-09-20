/* Sign-in gate, account controls, and the 401 handler that wraps every call */

let ME = null, ROLE = null;
const RANK = { viewer: 0, operator: 1, admin: 2 };
window.can = need => RANK[ROLE] >= RANK[need];

/* Every mutating call carries this header. The cookie is SameSite=Strict, so a
   cross-site form cannot ride along; the header a cross-site form cannot set. */
const _fetch = window.fetch.bind(window);
window.fetch = (url, opts = {}) => {
  if (typeof url === "string" && url.startsWith("/api/") &&
      opts.method && opts.method !== "GET") {
    opts.headers = Object.assign({ "X-HarvUI-Auth": "1" }, opts.headers || {});
  }
  return _fetch(url, opts);
};

async function authState() {
  try { return await (await _fetch("/api/auth/state")).json(); }
  catch (e) { return { setup: false, user: null }; }
}

function gate(html) {
  $("#gatebox").innerHTML = html;
  $("#gate").classList.remove("hidden");
}
function ungate() { $("#gate").classList.add("hidden"); }

function loginForm(err, setup) {
  gate(`
    <img class="mark" src="/assets/homestead-mark.svg?v=2.7.13" alt="">
    <h2>${setup ? "Set up Homestead" : "Homestead"}</h2>
    <p class="sub">${setup ? "Create the first administrator account" : "Sign in to continue"}</p>
    ${err ? `<div class="gateerr">${esc(err)}</div>` : ""}
    <div class="f"><label>Username</label>
      <input type="text" id="lg_user" autocomplete="username" autocapitalize="none" spellcheck="false"></div>
    <div class="f"><label>Password</label>
      <input type="password" id="lg_pass" autocomplete="${setup ? "new-password" : "current-password"}"></div>
    ${setup ? `<div class="f"><label>Confirm password</label>
      <input type="password" id="lg_pass2" autocomplete="new-password"></div>` : ""}
    <button class="btn pri wide" id="lg_go">${setup ? "Create account" : "Sign in"}</button>
    ${setup ? `<div class="gatehint">Minimum 10 characters. Stored as PBKDF2-SHA256 with a
      per-user salt in a Kubernetes Secret — never in plain text.</div>`
      : `<div class="gatehint">Homestead can deploy, move and delete workloads.<br>Sessions last 12 hours.</div>`}`);
  const go = () => setup ? doSetup() : doLogin();
  $("#lg_go").onclick = go;
  ["lg_user", "lg_pass", "lg_pass2"].forEach(id => {
    const el = $("#" + id);
    if (el) el.addEventListener("keydown", e => { if (e.key === "Enter") go(); });
  });
  setTimeout(() => $("#lg_user").focus(), 60);
}

async function doLogin() {
  const username = $("#lg_user").value.trim(), password = $("#lg_pass").value;
  if (!username || !password) return loginForm("Enter a username and password");
  $("#lg_go").textContent = "Signing in…"; $("#lg_go").disabled = true;
  try {
    const r = await _fetch("/api/auth/login", { method: "POST",
      headers: { "Content-Type": "application/json", "X-HarvUI-Auth": "1" },
      body: JSON.stringify({ username, password }) });
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
      headers: { "Content-Type": "application/json", "X-HarvUI-Auth": "1" },
      body: JSON.stringify({ username, password }) });
    const b = await r.json();
    if (!r.ok) return loginForm(b.error || "Setup failed", true);
    ME = b.user; ROLE = "admin"; ungate(); afterAuth();
  } catch (e) { loginForm(e.message, true); }
}

window.doLogout = async () => {
  try { await fetch("/api/auth/logout", { method: "POST" }); } catch (e) { }
  ME = null;
  clearInterval(window.__loopTimer);
  clearInterval(window.__imageUpdateLoop);
  clearTimeout(window.__operationTimer);
  loginForm();
};

window.pwChange = () => modal("Change password", `
  <div class="f"><label>Current password</label><input type="password" id="pw_old" autocomplete="current-password"></div>
  <div class="f"><label>New password</label><input type="password" id="pw_new" autocomplete="new-password"></div>
  <div class="f"><label>Confirm new password</label><input type="password" id="pw_new2" autocomplete="new-password"></div>
  <div class="row" style="margin-top:16px">
    <button class="btn pri" onclick="doPwChange()">Change password</button>
    <button class="btn" onclick="closeModal()">Cancel</button></div>
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

window.manageUsers = async () => {
  modal("Users", `<div class="empty"><span class="spin2"></span>loading</div>`);
  try {
    const us = await api("/api/auth/users");
    $("#mbody").innerHTML = `
      <div class="card flat pad0" style="margin-bottom:16px"><div class="tblwrap"><table class="tbl">
        <thead><tr><th>User</th><th>Role</th><th>Last sign-in</th><th></th></tr></thead><tbody>
        ${us.map(u => `<tr><td><div class="row" style="gap:9px">
            <div class="av">${esc(u.name.slice(0, 2).toUpperCase())}</div><b>${esc(u.name)}</b>
            ${u.name === ME ? '<span class="tag ok">you</span>' : ""}</div></td>
          <td><select onchange="setRole('${esc(u.name)}',this.value)" ${u.name === ME ? "disabled" : ""}
              style="padding:5px 9px;font-size:12px;width:auto">
            ${["viewer", "operator", "admin"].map(r =>
              `<option value="${r}" ${u.role === r ? "selected" : ""}>${r}</option>`).join("")}
          </select></td>
          <td class="dim small mono">${esc(u.last_login || "never")}</td>
          <td>${u.name === ME || us.length === 1 ? '<span class="dim xs">—</span>'
            : `<button class="btn sm danger" onclick="delUser('${esc(u.name)}')">Remove</button>`}</td>
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
  if (!confirm(`Remove user "${name}"?`)) return;
  try {
    await api("/api/auth/users/delete", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: name }) });
    toast("removed", "ok"); manageUsers();
  } catch (e) { toast(e.message, "bad"); }
};

/* any 401 anywhere drops straight back to the sign-in gate */
const _api = window.api;
window.api = async (path, opts) => {
  try { return await _api(path, opts); }
  catch (e) {
    if (/not signed in/i.test(e.message)) { ME = null; loginForm("Session expired — sign in again"); }
    else if (/cannot do this/i.test(e.message)) toast(e.message, "bad");
    throw e;
  }
};

function roleClass(r) {
  return "pill " + (r === "admin" ? "ok" : r === "operator" ? "low" : "neutral");
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
(async () => {
  const st = await authState();
  if (st.setup) return loginForm(null, true);
  if (!st.user) return loginForm();
  ME = st.user; ROLE = st.role || "admin"; ungate(); afterAuth();
})();

async function afterAuth() {
  paintWho();
  await loadHealthSettings();
  const route = HarvRouter.resolve(window.location.pathname);
  go(route.view, { history: false, fromLocation: true });
  startLoop();
  if (window.startOperationChecks) window.startOperationChecks();
  if (window.startUpdateChecks) window.startUpdateChecks();
}

window.setRole = async (name, role) => {
  try {
    await api("/api/auth/role", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: name, role }) });
    toast(name + " is now " + role, "ok"); manageUsers();
  } catch (e) { toast(e.message, "bad"); manageUsers(); }
};
