# Monaco Editor (vendored subset)

Version 0.52.2, taken from the npm package's `min/vs` build. Only the parts
Homestead uses are kept, because the full distribution is 14 MB:

- `vs/loader.js`, `vs/editor/editor.main.{js,css}` — the editor itself
- `vs/base/worker/workerMain.js` — the worker host the language services run in
- `vs/base/browser/ui/codicons/codicon/codicon.ttf` — the icon font its CSS asks for
- `vs/language/json/*` — JSON parsing and validation
- `vs/basic-languages/*` — highlighting for the formats found in appdata

Everything else (other language services, translations, the diff and standalone
extras) is deliberately absent. The editor is loaded only when a file is opened,
never on page load, and `web/js/views-storage.js` falls back to a plain textarea
if it fails to load at all.

Upstream: https://github.com/microsoft/monaco-editor — MIT, see LICENSE.
