/* "What's in this update", in the image-update review.
   Where to read about the new version (release notes, code changes, project
   site: links from the image's own labels), and what changes between the
   running image and the new one in ports, volumes, env and the user it runs
   as (POST /api/image-updates/notes). Env comes by name only. Read only:
   the review never waits for it, and a failure leaves it usable. */
(() => {
  const codes = list => list.map(x => `<code>${esc(x)}</code>`).join(" ");
  const MARKS = {add: "+", del: "−", chg: "~"};
  const line = (tone, label, html) => `<li class="un-line"><span class="un-mark ${tone}" aria-hidden="true">${MARKS[tone]}</span><span class="un-what">${esc(label)}</span><span class="un-detail">${html}</span></li>`;

  // Ports, volumes, env and user, as lines a person reads top to bottom.
  const changeLines = c => {
    const out = [], env = c.env || {};
    if (c.ports.added.length) out.push(line("add", "New port", codes(c.ports.added)));
    if (c.ports.removed.length) out.push(line("del", "Port no longer exposed", codes(c.ports.removed)));
    if (c.volumes.added.length) out.push(line("add", "New volume", `${codes(c.volumes.added)} <span class="dim">not kept unless you add storage for it</span>`));
    if (c.volumes.removed.length) out.push(line("del", "Volume no longer declared", codes(c.volumes.removed)));
    if (env.added?.length) out.push(line("add", "New setting", codes(env.added)));
    if (env.removed?.length) out.push(line("del", "Setting removed", codes(env.removed)));
    if (env.changed?.length) out.push(line("chg", "Default changed", `${codes(env.changed)} <span class="dim">value changed</span>`));
    if (c.user) out.push(line("chg", "Runs as", `<code>${esc(c.user.was)}</code> → <code>${esc(c.user.now)}</code> <span class="dim">${c.user.overridden_by != null
      ? `your app runs it as <code>${esc(c.user.overridden_by)}</code>, so this does not apply`
      : "files it writes may need new owners"}</span>`));
    return out;
  };

  const links = n => {
    const l = n.links || {};
    const a = (href, text) => `<a class="un-link" href="${safeHref(href)}" target="_blank" rel="noopener noreferrer">${esc(text)} ${icon("ext")}</a>`;
    return [l.release && a(l.release, n.to_version ? `Release notes for ${n.to_version}` : "Release notes"),
      l.compare && a(l.compare, "Code changes"), l.project && a(l.project, "Project site")].filter(Boolean).join("");
  };

  const imageBlock = (n, many) => {
    const name = many ? `<div class="un-container">${esc(n.container)}</div>` : "";
    if (n.error || !n.changes) return `<div class="un-image">${name}<p class="un-quiet">${esc(asSentence(n.error || "No details for this image"))}</p></div>`;
    const c = n.changes, lines = changeLines(c), head = links(n), own = c.env?.set_by_app || [];
    const build = c.build_env_changed ? `<p class="un-quiet">${c.build_env_changed} build detail${c.build_env_changed === 1 ? "" : "s"} changed too (versions and checksums the image was built from).</p>` : "";
    return `<div class="un-image">${name}
      ${head ? `<div class="un-links">${head}</div>` : `<p class="un-quiet">This image does not say where its source or release notes are.</p>`}
      ${lines.length ? `<ul class="un-lines">${lines.join("")}</ul>`
        : `<p class="un-same">${icon("check")} Same ports, volumes and settings as the version running now.</p>`}
      ${own.length ? `<p class="un-quiet">Your app sets ${codes(own)} itself, so its own values still apply.</p>` : ""}${build}</div>`;
  };

  const render = results => {
    const blocks = results.map(({config, notes, error}) => {
      const head = results.length > 1 ? `<div class="un-app">${esc(config.name)}</div>` : "";
      if (error) return `${head}<p class="dim">${esc(asSentence(error))}</p>`;
      const images = notes?.images || [];
      if (!images.length) return `${head}<p class="dim">${esc(asSentence(notes?.error || "No details for this update"))}</p>`;
      return head + images.map(n => imageBlock(n, images.length > 1)).join("");
    }).join("");
    return `<section class="un-notes" aria-label="What's in this update"><h3 class="un-title">What's in this update</h3>${blocks}</section>`;
  };

  /* Fills `el` for the apps being reviewed: one request per app, side by
     side. A failure says so in a sentence and leaves the review usable. */
  window.updateNotesInto = async (el, configs) => {
    if (!el) return;
    el.innerHTML = `<p class="un-loading dim"><span class="spin2"></span> Reading what changed…</p>`;
    const results = await Promise.all(configs.map(async config => {
      try {
        return {config, notes: await api("/api/image-updates/notes", {method: "POST", timeout: 20000,
          headers: {"Content-Type": "application/json", ...(config.cluster ? {"X-Homestead-Cluster": config.cluster} : {})},
          body: JSON.stringify({ns: config.ns, name: config.name})})};
      } catch (error) { return {config, error: error.message || "Could not read what changed"}; }
    }));
    if (el.isConnected) el.innerHTML = render(results);
  };
  window.UpdateNotes = {render};
})();
