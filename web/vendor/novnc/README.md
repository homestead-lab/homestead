# noVNC (vendored)

Version 1.7.0, the `core` and `vendor` directories of the `@novnc/novnc` npm
package, unmodified. They are ES modules; `web/js/vm-console.js` imports
`core/rfb.js` only when a VM console is opened, never on page load.

The VM console speaks RFB to KubeVirt's `vnc` subresource through Homestead's
WebSocket proxy (`server/homestead_vmconsole.py`), so the browser never holds
the service-account token.

Upstream: https://github.com/novnc/noVNC - MPL-2.0, see LICENSE.txt and AUTHORS.
pako (in `vendor/pako`) is MIT, see its LICENSE.
