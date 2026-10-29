"""The cookie a data move's progress page is read with (#936).

Named once, here, because three places must agree on it: the route that sets
it (homestead_self_data_server), the move's own status server that reads it
while Homestead is away (homestead_self_data_worker), and the route table's
list of Homestead's cookies (homestead_routes). A leaf module with no
imports, so the worker and the route table can both take it.
"""

NAME = "homestead-data-move"
PATH = "/api/self/data/handoff/"
