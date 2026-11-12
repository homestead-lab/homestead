"""An Edit save with the stored values its caller confirmed deleting.

edit_workload deletes from the app's Secret only the keys its caller
confirmed (prepared["secret_removal"]: the route sets it from the review's
acknowledgement, homestead_workloads.secret_removal); a caller that confirms
none deletes none. Tests of the save itself confirm what they expect to go."""
import homestead_workload_edit as WEDIT


def save(cfg, confirmed, hold=False):
    """edit_workload(cfg) as the Edit route makes it once deleting the
    keys in confirmed was acknowledged."""
    prepared = WEDIT.prepare_edit(cfg)
    prepared["secret_removal"] = sorted(confirmed)
    return WEDIT.edit_workload(cfg, hold=hold, prepared=prepared)
