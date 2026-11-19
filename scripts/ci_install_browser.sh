#!/bin/sh
# Playwright's Chromium for a CI job:
#   sh scripts/ci_install_browser.sh
# `playwright install --with-deps` ran apt-get update + install on every job, even with the
# browser cached, and a slow Ubuntu mirror hung it until the step's limit (18-19 Nov 2026).
# So: install the browser only (a cache hit makes that a no-op), see whether it starts, and
# touch apt only when it does not, with apt told to give up and retry instead of hanging.
# Time: the callers' step limit is 5 minutes (300 s). An apt attempt starts only if less than
# 200 s have passed, and takes at most 60 s (5 s more if it has to be killed) plus a start check
# of at most 20 s or a 5 s pause, so the worst case is 200 + 85 = 285 s. Errors are never hidden:
# a failure shows its reason.
set -eu

START=$(date +%s)
LIMIT_BEFORE_ATTEMPT=200
ATTEMPT_SECONDS=60

npm install --no-save --no-package-lock playwright@1.64.1 >/dev/null
npx playwright install chromium >/dev/null

# Launch headless Chromium and close it; its own error text goes to the log.
browser_starts() {
  timeout 20 node -e '
    const { chromium } = require("playwright");
    chromium.launch().then(browser => browser.close()).catch(error => { console.error(String(error.message || error).split("\n")[0]); process.exit(1); });
  '
}

if browser_starts; then
  echo "browser starts with the runner's own libraries; apt not used"
  exit 0
fi

echo "browser does not start; installing its system libraries" >&2
SUDO=""
if [ "$(id -u)" != 0 ] && command -v sudo >/dev/null 2>&1; then SUDO="sudo"; fi
# Retry quickly rather than wait on one stalled mirror connection.
printf '%s\n' 'Acquire::Retries "3";' 'Acquire::http::Timeout "20";' 'Acquire::https::Timeout "20";' \
  'DPkg::Lock::Timeout "10";' |
  $SUDO tee /etc/apt/apt.conf.d/80-ci-retries >/dev/null || echo "could not write the apt retry settings; going on without" >&2

attempt=1
while [ "$attempt" -le 3 ]; do
  if [ $(( $(date +%s) - START )) -ge "$LIMIT_BEFORE_ATTEMPT" ]; then
    echo "no time left for another apt attempt" >&2
    break
  fi
  status=0
  timeout -k 5 "$ATTEMPT_SECONDS" npx playwright install-deps chromium >/dev/null || status=$?
  if [ "$status" = 0 ] && browser_starts; then
    echo "installed system libraries (attempt $attempt)"
    exit 0
  fi
  echo "apt attempt $attempt gave no browser that starts (install-deps exit $status; 124 means it ran out of time)" >&2
  attempt=$((attempt + 1))
  sleep 5
done

echo "ERROR: Chromium cannot start and its system libraries could not be installed (is the Ubuntu package mirror reachable?)" >&2
exit 1
