import hashlib, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_icons as ICONS
import homestead_move_state as STATE
import homestead_move_phases as PHASE
import homestead_names as NAMES
import server

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
REFERENCE = f"/api/icons/{hashlib.sha256(PNG).hexdigest()}.png"


class Client:
    def __init__(self, data=None, error=None):
        self.data, self.error, self.asked = data, error, []

    def icon_bytes(self, cluster, reference):
        self.asked.append((cluster, reference))
        if self.error:
            raise self.error
        return self.data


class CarryIconTests(unittest.TestCase):
    """A moved workload's logo named the source's icon cache, which the move
    did not bring, so its card had no logo here."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = STATE.DATA_DIR, STATE.CLIENT, ICONS.persist
        STATE.DATA_DIR = self.tmp.name

    def tearDown(self):
        STATE.DATA_DIR, STATE.CLIENT, ICONS.persist = self.saved
        self.tmp.cleanup()

    def test_the_logo_comes_from_the_source_under_the_same_name(self):
        STATE.CLIENT = Client(PNG)
        annotations = {NAMES.key("icon"): REFERENCE}

        found = PHASE.carry_icon("oldcluster", annotations)

        self.assertEqual(REFERENCE, found)
        self.assertTrue(ICONS.exists(REFERENCE, self.tmp.name))
        self.assertEqual([("oldcluster", REFERENCE)], STATE.CLIENT.asked)

    def test_the_original_url_is_the_fallback(self):
        STATE.CLIENT = Client(error=ConnectionError("gone"))
        ICONS.persist = lambda source, data_dir: ICONS.store(PNG, data_dir)
        annotations = {NAMES.key("icon"): "/api/icons/" + "a" * 64 + ".png",
                       NAMES.key("icon-source"): "https://example.com/logo.png"}

        found = PHASE.carry_icon("oldcluster", annotations)

        self.assertEqual(REFERENCE, found)
        self.assertEqual(REFERENCE, annotations[NAMES.key("icon")])

    def test_a_logo_that_cannot_be_had_does_not_fail_the_move(self):
        STATE.CLIENT = Client(error=ConnectionError("gone"))
        annotations = {NAMES.key("icon"): REFERENCE}

        self.assertEqual("", PHASE.carry_icon("oldcluster", annotations))
        self.assertEqual(REFERENCE, annotations[NAMES.key("icon")])

    def test_a_logo_already_cached_here_is_left_alone(self):
        ICONS.store(PNG, self.tmp.name)
        STATE.CLIENT = Client(PNG)

        PHASE.carry_icon("oldcluster", {NAMES.key("icon"): REFERENCE})

        self.assertEqual([], STATE.CLIENT.asked)


class HealIconTests(unittest.TestCase):
    """Workloads moved before moves carried logos get theirs back."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (server.NAMES_HELPERS.DATA_DIR, STATE.DATA_DIR, STATE.CLIENT)
        server.NAMES_HELPERS.DATA_DIR = STATE.DATA_DIR = self.tmp.name
        server.NAMES_HELPERS._ICON_HEALED.clear()
        server.NAMES_HELPERS._ICON_TRIED.clear()

    def tearDown(self):
        server.NAMES_HELPERS.DATA_DIR, STATE.DATA_DIR, STATE.CLIENT = self.saved
        server.NAMES_HELPERS._ICON_HEALED.clear()
        server.NAMES_HELPERS._ICON_TRIED.clear()
        self.tmp.cleanup()

    def test_a_moved_workload_fetches_its_logo_from_where_it_came_from(self):
        STATE.CLIENT = Client(PNG)
        annotations = {NAMES.key("icon"): REFERENCE, NAMES.key("moved-from"): "oldcluster/frigate"}

        # As if the background attempt had just been started, so this test
        # runs the heal itself rather than racing a thread.
        server.NAMES_HELPERS._ICON_TRIED[REFERENCE] = server.time.time()
        self.assertEqual("", server.NAMES_HELPERS.display_icon(annotations))
        server.NAMES_HELPERS._heal_icon(REFERENCE, annotations)

        healed = server.NAMES_HELPERS.display_icon(annotations)
        self.assertRegex(healed, r"^/api/icons/[0-9a-f]{64}\.png$")
        self.assertEqual(PNG, Path(ICONS.resolve(healed, self.tmp.name)[0]).read_bytes())
        self.assertEqual([("oldcluster", REFERENCE)], STATE.CLIENT.asked)

    def test_a_missing_logo_is_tried_once_then_left_a_while(self):
        started = []
        saved = server.threading.Thread
        server.threading.Thread = lambda **kw: type("T", (), {"start": lambda self: started.append(kw)})()
        try:
            annotations = {NAMES.key("icon"): REFERENCE}
            server.NAMES_HELPERS.display_icon(annotations)
            server.NAMES_HELPERS.display_icon(annotations)
        finally:
            server.threading.Thread = saved

        self.assertEqual(1, len(started))


if __name__ == "__main__":
    unittest.main()
