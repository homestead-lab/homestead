/* Homestead core: the API client: api(), mustRead() and the aborts of a left page's reads. A classic script, loaded right after core.js (#911). */
/* ---------------- fetch ---------------- */
/* Every navigation bumps this. A view builds its page across several awaits,
   so a slow one could still be waiting when you move on - and then paint its
   answer over the page you actually asked for, leaving the URL, the title and
   the body disagreeing. A read whose answer arrives after you have navigated
   away is for a page nobody is looking at, so it is abandoned rather than
   returned. Writes are never abandoned: their result has to be reported.
   An abandoned read never settles, so nothing of the page left runs on. Each
   gets a promise of its own that nothing else holds: the frames awaiting it,
   and the page data they keep, are then freed with it. One shared promise
   would hold every abandoned page for the rest of the visit. */
window.NAV_TOKEN = 0;
const abandoned = () => new Promise(() => { });

/* Dropping a late answer is not enough: the request itself keeps one of the
   browser's six connections to Homestead busy until the cluster answers, and
   the next page's reads queue behind it. So each page visit has an
   AbortController, and leaving the page cancels the reads it started (#705). */
let pageReads = { token: -1, controller: null };
function pageReadSignal() {
  if (typeof AbortController === "undefined") return null;
  if (pageReads.token !== window.NAV_TOKEN) {
    pageReads.controller?.abort();
    pageReads = { token: window.NAV_TOKEN, controller: new AbortController() };
  }
  return pageReads.controller.signal;
}
/* Called when the page changes: cancels what the page being left is reading. */
function abortPageReads() {
  if (pageReads.token !== window.NAV_TOKEN) { pageReads.controller?.abort(); pageReads = { token: -1, controller: null }; }
}
window.abortPageReads = abortPageReads;

async function api(path, opts) {
  // `keep` marks a read whose answer matters after the page changes.
  const readOnly = (!opts || !opts.method || opts.method === "GET") && !opts?.keep;
  const startedAt = window.NAV_TOKEN;
  // A page's own read is cancelled when the page is left; a caller's own
  // signal, a `keep` read and a write are left alone.
  const page = readOnly && !opts?.signal ? pageReadSignal() : null;
  // `timeout` (ms) gives up on a request nothing will answer: a host that went
  // down mid-request leaves its connection open, and the browser waits on it
  // long after Homestead is back.
  if (opts?.timeout && !opts.signal && typeof AbortSignal !== "undefined" && AbortSignal.timeout) {
    const timeout = AbortSignal.timeout(opts.timeout);
    opts = { ...opts, signal: page && AbortSignal.any ? AbortSignal.any([timeout, page]) : timeout };
  } else if (page) opts = { ...opts, signal: page };
  // Linked clusters: which one this goes to (fleet.js).
  if (window.fleetRoute) ({ path, opts } = window.fleetRoute(path, opts || {}));
  const diagnostic = window.HomesteadRecorder?.request(path, opts);
  if (diagnostic) opts = { ...opts, headers: { ...opts?.headers, ...diagnostic.headers } };
  let r;
  try { r = await fetch(path, opts); }
  catch (error) {
    const left = readOnly && startedAt !== window.NAV_TOKEN;
    diagnostic?.done(0, undefined, left && error?.name === "AbortError");
    // A connection that broke after you left the page is for a page nobody is
    // looking at too: dropped like a late answer, so its error is not shown on
    // the page you went to.
    if (left) return abandoned();
    throw error;
  }
  const missing = r.headers.get("x-homestead-fleet-missing");
  if (missing && window.fleetNoteMissing) window.fleetNoteMissing(missing);
  const ct = r.headers.get("content-type") || "";
  let b;
  try { b = ct.includes("json") ? await r.json() : await r.text(); }
  catch (error) {
    const left = readOnly && startedAt !== window.NAV_TOKEN;
    diagnostic?.done(r.status, undefined, left && error?.name === "AbortError");
    // Cancelled while its answer was still arriving, because you left the page.
    if (left) return abandoned();
    throw error;
  }
  diagnostic?.done(r.status, b?.operation?.id);
  if (readOnly && startedAt !== window.NAV_TOKEN) return abandoned();
  if (!r.ok) throw Object.assign(new Error((b && b.error) || r.statusText), { status: r.status, body: b });
  if (b && b.operation && window.noteOperation) window.noteOperation(b.operation);
  return b;
}

/* A read the caller cannot do without. Its failure says what could not be
   read, so the page shows that rather than a form built on a guess (#620). */
function mustRead(path, what, opts) {
  return api(path, opts).catch(e => { throw Object.assign(new Error(`Homestead could not read ${what}. ${e.message}`), { status: e.status }); });
}

// Node only (tests): the same names, for require(). The browser has no `module`.
if (typeof module === "object" && module.exports) module.exports = {
  abandoned, pageReads, pageReadSignal, abortPageReads, api, mustRead
};
