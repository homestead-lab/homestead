#!/bin/sh
# The browser checks CI runs against the demo server, in groups that take
# about the same time, so each group gets a runner of its own:
#   sh scripts/ci_ui_checks.sh <group>
# Every check in the list belongs to exactly one group; a new check joins
# the group that finishes soonest (the CI job's timings show which).
set -eu
# The demo server the CI job starts; the checks that do not read it ignore it.
export HOMESTEAD_URL=http://127.0.0.1:4173

case "${1:-}" in
  dialogs-0|dialogs-1|dialogs-2)
    AUDIT_SHARD=${1#dialogs-} AUDIT_SHARDS=3 node scripts/audit_dialogs.mjs
    ;;
  dialog-flows)
    DIALOG_STANDARD_SCREENSHOTS=release-assets/dialogs/section-rail node tests/integration/dialog-section-rail.mjs
    EDIT_SCREENSHOTS=release-assets/dialogs/container-edit node tests/integration/container-edit-steps.mjs
    CONTAINER_SET_SCREENSHOTS=release-assets/dialogs/container-set node tests/integration/container-set.mjs
    PASSTHROUGH_SCREENSHOTS=release-assets/dialogs/passthrough node tests/integration/passthrough-devices.mjs
    VM_ISOLATION_SCREENSHOTS=release-assets/dialogs/vm-isolation node tests/integration/vm-network-isolation.mjs
    FIREWALL_SCREENSHOTS=release-assets/dialogs/firewall node tests/integration/workload-firewall.mjs
    ;;
  pages)
    node scripts/audit_pages.mjs
    PAGE_STANDARD_SCREENSHOTS=release-assets/pages/components node tests/integration/page-components.mjs
    DASHBOARD_EDITOR_SCREENSHOTS=release-assets/pages/dashboard-editor node tests/integration/dashboard-editor.mjs
    node tests/integration/dashboard-account.mjs
    DEPLOY_FORM_SCREENSHOTS=release-assets/pages/container-deploy node tests/integration/container-deploy-form.mjs
    WIDGET_OPTIONS_SCREENSHOTS=release-assets/pages/widget-options node tests/integration/dashboard-widget-options.mjs
    CUSTOM_WIDGET_SCREENSHOTS=release-assets/pages/custom-widget node tests/integration/dashboard-custom.mjs
    ;;
  pages-dashboard)
    INSIGHTS_OUTPUT=release-assets/pages/insights node tests/integration/health-insights.mjs
    NODE_HEALTH_SCREENSHOTS=release-assets/pages/node-health node tests/integration/dashboard-node-health.mjs
    RESOURCE_WIDGET_SCREENSHOTS=release-assets/pages/resources node tests/integration/dashboard-resources.mjs
    ARCHITECTURE_SCREENSHOTS=release-assets/pages/architecture node tests/integration/combined-architecture.mjs
    NOTIFICATION_SCREENSHOTS=release-assets/pages/notifications node tests/integration/mobile-notifications.mjs
    ALERT_SCREENSHOTS=release-assets/pages/alerts node tests/integration/notification-alerts.mjs
    node scripts/check_dashboard_storage.mjs
    node scripts/check_collection_layouts.mjs
    ;;
  pages-setup)
    node scripts/check_setup_guide.mjs
    STORAGE_SCREENSHOTS=release-assets/pages/setup-storage node tests/integration/setup-storage-classes.mjs
    node scripts/check_demo_scenarios.mjs
    node scripts/check_launch_import_logs.mjs
    node scripts/check_mobile_pwa.mjs
    ;;
  pages-live)
    node scripts/check_dashboard_stream.mjs
    VOLUME_SCREENSHOTS=release-assets/pages/planned-volumes node tests/integration/planned-container-volumes.mjs
    node tests/integration/volume-files-cleanup.mjs
    node scripts/check_update_channels.mjs
    node scripts/check_troubleshooting.mjs
    ;;
  *)
    echo "Usage: sh scripts/ci_ui_checks.sh dialogs-0|dialogs-1|dialogs-2|dialog-flows|pages|pages-dashboard|pages-setup|pages-live" >&2
    exit 2
    ;;
esac
