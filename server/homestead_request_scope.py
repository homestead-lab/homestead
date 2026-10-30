"""What a request binds on its handler's thread ends with the request.

The request role (homestead_host_access) and the person a change is signed as
(homestead_changes.REQUEST.user) are thread locals the handler binds for each
request. They were cleared only when the next request began, so between
requests they still named the last caller: work later on that thread, or a
test that drives the handler in its own thread, read them."""
import functools

import homestead_changes as CHANGES
import homestead_host_access as HOSTACCESS
import homestead_read_deadline as DEADLINE

# GET connections that stay open as long as someone uses them: no deadline.
LONG_CONNECTIONS = ("/api/console", "/api/node/shell", "/api/vm/console")


def unbind():
    """Back to Homestead's own scope on this thread: no caller's role (None,
    which host access and the Kubernetes guard read as Homestead itself,
    unchecked, as _begin() always set it) and no one to sign changes as.
    This ends a request's binding; it does not tighten anything."""
    CHANGES.REQUEST.user = None
    HOSTACCESS.set_role(None)
    DEADLINE.end()


def begin(handler):
    """A request starts: nothing of the last one bound, and a GET's
    Kubernetes reads given one deadline together (homestead_read_deadline)."""
    unbind()
    path = (getattr(handler, "path", "") or "").split("?", 1)[0]
    if getattr(handler, "command", None) == "GET" and path not in LONG_CONNECTIONS:
        DEADLINE.begin()


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
