"""What a request binds on its handler's thread ends with the request.

The request role (homestead_host_access) and the person a change is signed as
(homestead_changes.REQUEST.user) are thread locals the handler binds for each
request. They were cleared only when the next request began, so between
requests they still named the last caller: work later on that thread, or a
test that drives the handler in its own thread, read them."""
import functools

import homestead_changes as CHANGES
import homestead_host_access as HOSTACCESS


def unbind():
    """Back to Homestead's own scope on this thread: no caller's role (None,
    which host access and the Kubernetes guard read as Homestead itself,
    unchecked, as _begin() always set it) and no one to sign changes as.
    This ends a request's binding; it does not tighten anything."""
    CHANGES.REQUEST.user = None
    HOSTACCESS.set_role(None)


def unbinds_after(decorator):
    """decorator(method), unbinding when the request ends, also when it raises."""
    def wrap(method):
        inner = decorator(method)

        @functools.wraps(method)
        def handled(handler):
            try:
                return inner(handler)
            finally:
                unbind()
        return handled
    return wrap
