"""The wiki page "What Homestead changes" names every kind of object Homestead
makes (#744), so the page cannot fall behind the code.

A kind counts as made by Homestead when:
- the install manifests, the Helm chart or the k3s/RKE2 bootstrap declare it
  (a top-level `kind:` line), or
- server code builds an object of it: a dict literal with `apiVersion`,
  `kind` and `metadata`, the shape of a body sent to the API server. A kind
  written as an expression (`"A" if x else "B"`) counts for each name in it.

A kind passes when its name appears in backticks anywhere on the page.

What the scan cannot see is listed by hand in HAND_LISTED, and the page must
name those too: kinds held in a variable, kinds inside YAML text in Python,
and kinds Homestead only patches or deletes (a write to an existing object
carries no kind, and its path is often built at run time). Kinds that come
from what a person gives Homestead - the Resources page, a Helm chart, an
upstream release manifest - are not Homestead's code and are not checked.

The page's advice on removing Homestead must never delete by the
`homestead.io/managed` label: apps deployed through Homestead carry it too.
"""
import ast
import re
import unittest
from pathlib import Path

import source_scan as SS
from fakes.kube import no_cluster

ROOT = Path(__file__).resolve().parents[1]
WIKI = ROOT / "docs" / "wiki"
PAGE = WIKI / "What-Homestead-changes.md"
INSTALL = ROOT / "scripts" / "install.sh"
LINK = re.compile(r"\]\(What-Homestead-changes(#[^)]*)?\)")

# Bodies that have the shape of an object but are only part of a request.
NOT_OBJECTS = {"DeleteOptions": "the options of a delete request"}

# Kinds Homestead writes that the scan above cannot see, and where.
HAND_LISTED = {
    "ServiceAccount": "kind in a variable: homestead_self_data_kube.access_resources",
    "Role": "kind in a variable: homestead_self_data_kube.access_resources",
    "RoleBinding": "kind in a variable: homestead_self_data_kube.access_resources",
    "ClusterRoleBinding": "kind in a variable: homestead_self_data_kube.access_resources",
    "PersistentVolume": "patched and deleted only: homestead_reclass, homestead_onboard",
    "VolumeAttachment": "patched only (finalizers): homestead_onboard; Longhorn's: homestead_lhrebuild",
    "Machine": "deleted only (Cluster API): homestead_onboard",
    "Replica": "deleted only (Longhorn): homestead_disks",
    "Volume": "patched and deleted only (Longhorn): homestead_volumes, homestead_node_parity, homestead_fs_freeze",
    "Setting": "patched only (Longhorn): homestead_lhrebuild, homestead_node_parity, homestead_fs_freeze",
    "Node": "patched and deleted only: taints, labels, cordon; Longhorn nodes' disks",
    "StatefulSet": "scaled only: homestead_power_hold",
}

INSTALL_FILES = ("deploy/*.yaml", "charts/homestead/templates/*.yaml", "scripts/bootstrap-k3s.sh")
LINKED_FROM = ("_Sidebar.md", "Installing-Homestead.md", "Installing-on-an-existing-cluster.md",
               "Installing-on-k3s.md", "Installing-on-RKE2.md", "Installing-on-Harvester.md")
KIND_NAME = re.compile(r"[A-Z][A-Za-z0-9]+")


def setUpModule():
    # No cluster here: a read on the way to what a test checks fails at once, as on CI,
    # instead of looking the API server's name up (tests/fakes/kube.py no_cluster).
    unittest.enterModuleContext(no_cluster())


def install_kinds(root=ROOT):
    """{kind: [where]} from the top-level `kind:` lines of the install files."""
    found = {}
    for pattern in INSTALL_FILES:
        for path in sorted(root.glob(pattern)):
            for number, line in enumerate(SS.text(path).splitlines(), 1):
                match = re.match(r"kind: ([A-Za-z0-9]+)\s*$", line)
                if match:
                    found.setdefault(match.group(1), []).append(f"{path.relative_to(root)}:{number}")
    return found


def install_names(root=ROOT):
    """{name: [where]}: the literal top-level metadata.name of each object the
    plain manifest and the Helm chart install. A templated name is left out."""
    found = {}
    for pattern in INSTALL_FILES[:2]:
        for path in sorted(root.glob(pattern)):
            lines = SS.text(path).splitlines()
            for number, line in enumerate(lines, 1):
                if line.rstrip() != "metadata:":
                    continue
                for below in lines[number:]:
                    match = re.match(r"  name: ([A-Za-z0-9.-]+)\s*$", below)
                    if match:
                        found.setdefault(match.group(1), []).append(f"{path.relative_to(root)}:{number}")
                    if not below.startswith("  ") or match:
                        break
    return found


def code_kinds(root=ROOT):
    """{kind: [where]} from the object bodies server code builds."""
    found = {}
    for path in sorted((root / "server").rglob("*.py")):
        for node in ast.walk(SS.tree(path)):
            if not isinstance(node, ast.Dict):
                continue
            keys = {key.value: value for key, value in zip(node.keys, node.values)
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)}
            if not {"apiVersion", "kind", "metadata"} <= set(keys):
                continue
            for part in ast.walk(keys["kind"]):
                if (isinstance(part, ast.Constant) and isinstance(part.value, str)
                        and KIND_NAME.fullmatch(part.value)):
                    found.setdefault(part.value, []).append(f"{path.relative_to(root)}:{node.lineno}")
    return found


def DOC_PAGES():
    return sorted([*ROOT.glob("docs/**/*.md"), ROOT / "README.md"])


# Saying to delete, remove, prune, purge, clean up or uninstall, or that it can go.
DELETE = re.compile(r"\b(delete|remove|rm|prune|purge|clean[ -]?up|uninstall|get rid of|(can|may) go)\b", re.I)
# Homestead's labels that also mark apps, their parts and their data, plain or
# URL-encoded: managed, and the app and task labels (on apps' parts too).
LABEL = re.compile(r"homestead\.io(/|%2F)(managed|app|task)\b|managed(=|%3D)true|managed-by(=|%3D)homestead", re.I)
# A warning: the negation comes right before the verb ("do not delete").
WARNING = re.compile(r"\b(not|never|don't)\s+(\w+\s+){0,2}?" + DELETE.pattern[2:], re.I)


def deletes_by_managed_label(text):
    """The places in text that say to delete by one of Homestead's labels.

    Code: every fenced block under one heading counts as one command, so a
    command split with a backslash, a list made in one block and deleted in
    the next, or deleted on the next line is still seen. Prose counts per
    paragraph, and each table row on its own: in one that names a label, a
    sentence that tells the reader to delete ("Delete the ones ...") or names
    the label beside a delete is advice, unless the negation sits right
    before the verb ("do not delete", "never remove"). Case does not matter."""
    units, prose, block, section = [], [], None, []

    def end_prose():
        if prose:
            units.append(("prose", " ".join(prose)))
            prose.clear()

    def end_section():
        if section:
            units.append(("code", " ".join(section)))
            section.clear()

    for line in text.splitlines() + [""]:
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            if block is None:
                end_prose()
                block = []
            else:
                section.append(" ".join(part.rstrip("\\ ") for part in block))
                block = None
        elif block is not None:
            block.append(line)
        elif stripped.startswith("#"):
            end_prose()
            end_section()
        elif stripped.startswith("|"):
            end_prose()
            units.append(("prose", stripped))
        elif stripped:
            prose.append(line.lstrip("> "))
        else:
            end_prose()
    end_prose()
    end_section()
    found = []
    for kind, unit in units:
        if not (DELETE.search(unit) and LABEL.search(unit)):
            continue
        if kind == "prose" and not any(
                not WARNING.search(sentence)
                and (re.match(r"\W*" + DELETE.pattern, sentence, re.I) or LABEL.search(sentence))
                for sentence in re.split(r"(?<=[.!?])\s+", unit) if DELETE.search(sentence)):
            continue
        found.append(unit.strip())
    return found


def uninstall_objects(script):
    """{name: tier} from the list install.sh --uninstall removes."""
    block = re.search(r"^UNINSTALL_OBJECTS='\n(.*?)^'", script, re.M | re.S)
    return {line.split()[3]: line.split()[0] for line in block.group(1).splitlines() if line.strip()}


def page_section(page, start, end):
    return page[page.index(start):page.index(end)]


def uninstall_list_problems(script, page):
    """Where the script's list and the page's "Safe to delete" list part ways:
    the script removing something that is not there, or that is under "do not
    delete", or the page calling something safe that the script leaves."""
    safe = page_section(page, "### Safe to delete", "### Still used")
    keep = page[page.index("### Still used"):]
    objects = uninstall_objects(script)
    problems = []
    for name in objects:
        if f"`{name}`" not in safe:
            problems.append(f"the script removes {name}, which is not under Safe to delete")
        if f"`{name}`" in keep:
            problems.append(f"the script removes {name}, which is under do not delete")
    for name in set(re.findall(r"`(homestead(?:-[a-z0-9-]+)?)`", safe)) - set(objects):
        if name not in UNINSTALL_NOT_OBJECTS:
            problems.append(f"the page calls {name} safe, and the script does not remove it")
    return problems


# On the page's safe list, removed by the script another way than by name:
# Longhorn attachment tickets are one key of an object that stays.
UNINSTALL_NOT_OBJECTS = {"homestead-rebuild", "homestead-revert"}


# The ways server code names a ConfigMap or Secret of its own: by the naming
# helper, in an API path, or in a constant that a path uses.
OBJECT_NAME = re.compile(r'NAMES\.object_name\(["\']([a-z0-9-]+)["\']')
PATH_NAME = re.compile(r"/(?:configmaps|secrets)/(homestead-[a-z0-9-]+)")
BRAND_PATH = re.compile(r"/(?:configmaps|secrets)/\{NAMES\.BRAND\}-([a-z0-9-]+)")
CONSTANT = re.compile(r'^([A-Z_]+) = "(homestead-[a-z0-9-]+)"', re.M)
# Not what a person finds on the cluster under that name.
NOT_RECORDS = {"homestead-isos": "a StorageClass, on the page", "homestead-smb": "a share server, on the page",
               "homestead-nfs": "a share server, on the page"}


def code_object_names(root=ROOT):
    """The ConfigMaps and Secrets server code names as its own, {name: file}."""
    found = {}
    for path in sorted((root / "server").rglob("*.py")):
        text = SS.text(path)
        names = {f"homestead-{name}" for name in OBJECT_NAME.findall(text) + BRAND_PATH.findall(text)}
        names |= set(PATH_NAME.findall(text))
        for constant, name in CONSTANT.findall(text):
            if re.search(r"/(?:configmaps|secrets)/\{(?:\w+\.)?" + constant + r"\}", text):
                names.add(name)
        for name in names:
            found.setdefault(name, path.name)
    return found


def made_kinds(root=ROOT):
    found = {}
    for source in (install_kinds(root), code_kinds(root)):
        for kind, where in source.items():
            found.setdefault(kind, []).extend(where)
    for kind in NOT_OBJECTS:
        found.pop(kind, None)
    return found


def unlisted(root=ROOT):
    """The kinds Homestead makes that the page does not name in backticks."""
    page = SS.text(root / PAGE.relative_to(ROOT))
    return {kind: where for kind, where in made_kinds(root).items() if f"`{kind}`" not in page}


class WhatHomesteadChanges(unittest.TestCase):
    def test_the_scan_finds_what_homestead_is_known_to_make(self):
        # A scan that quietly found nothing would pass every page.
        found = made_kinds()
        for kind in ("Deployment", "ClusterRole", "DaemonSet", "RecurringJob", "PriorityClass", "Lease"):
            self.assertIn(kind, found)
        self.assertNotIn("DeleteOptions", found)

    def test_every_kind_homestead_makes_is_on_the_page(self):
        self.assertTrue(PAGE.exists(), f"{PAGE.relative_to(ROOT)} is missing")
        missing = unlisted()
        self.assertEqual(missing, {}, "Name these kinds on docs/wiki/What-Homestead-changes.md "
                         "(in backticks, with when Homestead makes them):\n" + "\n".join(
                             f"  {kind}: {', '.join(where[:3])}" for kind, where in sorted(missing.items())))

    def test_every_object_the_install_makes_is_named_on_the_page(self):
        found = install_names()
        for name in ("homestead", "homestead-data", "homestead-console", "homestead-nodeprobe"):
            self.assertIn(name, found, "the name scan found nothing it should")
        page = SS.text(PAGE)
        missing = {name: where for name, where in found.items() if f"`{name}`" not in page}
        self.assertEqual(missing, {}, "Name these installed objects on the page, in backticks:\n" + "\n".join(
            f"  {name}: {', '.join(where[:3])}" for name, where in sorted(missing.items())))

    def test_the_sidebar_and_install_pages_link_to_it(self):
        for name in LINKED_FROM:
            with self.subTest(page=name):
                self.assertTrue(LINK.search(SS.text(WIKI / name)),
                                f"{name} has no link to What-Homestead-changes")

    def test_the_kinds_the_scan_cannot_see_are_on_the_page(self):
        page = SS.text(PAGE)
        missing = sorted(kind for kind in HAND_LISTED if f"`{kind}`" not in page)
        self.assertEqual(missing, [], "Name these hand-listed kinds on the page: " + ", ".join(missing))

    def test_the_marks_a_restore_into_an_app_leaves_are_on_the_page(self):
        # Restore into an app (#743) marks the claims it moves an app between,
        # and swaps back or deletes on its own (#935): every key it writes,
        # and the default time a previous volume is kept, must be on the page.
        source = (ROOT / "server" / "homestead_restore_swap.py").read_text(encoding="utf-8")
        keys = sorted(set(re.findall(r'NAMES\.key\("([a-z-]+)"\)', source)))
        for key in ("previous-volume", "previous-of", "replaced-by", "restored-into", "keep-previous"):
            self.assertIn(key, keys, "the scan found nothing it should")
        page = PAGE.read_text(encoding="utf-8")
        missing = [key for key in keys if f"homestead.io/{key}" not in page and f"`{key}`" not in page]
        self.assertEqual(missing, [], "Name these restore-swap marks on the page: " + ", ".join(missing))
        days = re.search(r"^DEFAULT_KEEP_DAYS = (\d+)", source, re.M).group(1)
        flat = " ".join(page.split())
        self.assertRegex(flat, re.compile(rf"previous volumes?[^|]*\b{days} days", re.I),
                         "the page must say how long a previous volume is kept")
        self.assertRegex(flat, re.compile(r"restore into an app[^|]*swapped back", re.I),
                         "the page must say a restore into an app swaps back on its own")

    def test_the_label_check_catches_every_form_of_the_advice(self):
        # Each of these would delete apps; each must be caught.
        for text in ("Most carry `homestead.io/managed=true`. Delete the ones you no longer want.",
                     "```bash\nkubectl delete all -A -l homestead.io/managed=true\n```",
                     "```bash\nkubectl get all -A \\\n  -l homestead.io/managed=true -o name \\\n"
                     "  | xargs kubectl delete\n```",
                     "```\nkubectl get all -A -l homestead.io/managed=true -o name > list\n"
                     "xargs -a list kubectl delete\n```",
                     "Remove everything labelled homestead.io/managed=true when you are done.",
                     "```\nKUBECTL DELETE ALL -L HOMESTEAD.IO/MANAGED=TRUE\n```",
                     # A "not" elsewhere in the sentence is not a warning.
                     "If you do not plan to reinstall, delete everything labelled `homestead.io/managed=true`.",
                     "Delete what carries `homestead.io/managed=true` that you do not need.",
                     # A table row is advice on its own, whatever other rows say.
                     "| Object | Action |\n|---|---|\n| `homestead-auth` | do not delete |\n"
                     "| everything labelled `homestead.io/managed=true` | delete it |",
                     # Other verbs.
                     "```\nkubectl apply --prune -l homestead.io/managed=true -f empty/\n```",
                     "Purge whatever has `homestead.io/managed=true`.",
                     "Clean up everything labelled `homestead.io/managed=true` afterwards.",
                     # A listing in one block and the delete in the next.
                     "```\nkubectl get all -A -l homestead.io/managed=true -o name > list\n```\n\n"
                     "Then:\n\n```\nxargs -a list kubectl delete\n```",
                     # The other label Homestead puts on app Secrets and restored volumes.
                     "```\nkubectl delete secrets,pvc -A -l app.kubernetes.io/managed-by=homestead\n```",
                     "Remove everything with `app.kubernetes.io/managed-by=homestead`.",
                     "```\ncurl -X DELETE \"$API/api/v1/secrets?labelSelector=homestead.io%2Fmanaged%3Dtrue\"\n```",
                     # The labels that name an app or a task, on apps and their parts too.
                     "```\nkubectl delete all -A -l homestead.io/app=web\n```",
                     "```\nkubectl delete pods,jobs -A -l homestead.io/task\n```",
                     "```\ncurl -X DELETE \"$API/api/v1/pods?labelSelector=homestead.io%2Fapp%3Dweb\"\n```",
                     "Remove whatever carries `homestead.io/task=copy`.",
                     # Saying it may go, without a delete verb.
                     "Everything labelled `homestead.io/managed=true` can go.",
                     "Objects with `homestead.io/app` may go once the app is gone."):
            with self.subTest(text=text[:50]):
                self.assertTrue(deletes_by_managed_label(text), text)
        for text in ("> **Do not delete by the `homestead.io/managed=true` label.** Apps carry it too.",
                     "Never remove by `homestead.io/app` or `homestead.io/task` either.",
                     "> Do not delete by any Homestead label (`homestead.io/managed=true`). Only the objects "
                     "named under \"Safe to delete\" below can go.",
                     "Never delete by that label.",
                     "```bash\nkubectl -n lab delete deployment/homestead\n```",
                     "Do not delete by any Homestead label (`homestead.io/*` or `app.kubernetes.io/managed-by=homestead`).",
                     "| `app.kubernetes.io/managed-by=homestead` on app Secrets | never remove by this label |"):
            with self.subTest(text=text[:50]):
                self.assertFalse(deletes_by_managed_label(text), text)

    def test_no_page_says_to_delete_by_the_managed_label(self):
        # Apps deployed through Homestead carry the label as well, so deleting
        # by it would delete them (homestead_deploy_manifests: the app's
        # Deployment and Service).
        apps = SS.text(SS.SERVER / "homestead_deploy_manifests.py")
        self.assertIn('NAMES.key("managed")', apps, "apps no longer carry the label: revisit this test")
        for path in DOC_PAGES():
            with self.subTest(page=str(path.relative_to(ROOT))):
                self.assertEqual(deletes_by_managed_label(SS.text(path)), [])
        page = " ".join(SS.text(PAGE).split())
        self.assertIn("apps you deployed through Homestead carry it too", page,
                      "the page must warn that the managed label also marks the reader's apps")

    def test_the_uninstall_list_matches_the_page(self):
        script, page = SS.text(INSTALL), SS.text(PAGE)
        self.assertGreater(len(uninstall_objects(script)), 20, "the list scan found nothing it should")
        self.assertEqual(uninstall_list_problems(script, page), [])
        safe = page_section(page, "### Safe to delete", "### Still used")
        for thing in ("homestead.io/standby", "homestead-rebuild", "homestead-revert", "99-homestead-hold"):
            self.assertIn(thing, script)
            self.assertIn(thing, safe)

    def test_the_guard_fails_when_the_script_and_the_page_part_ways(self):
        script, page = SS.text(INSTALL), SS.text(PAGE)
        self.assertEqual(uninstall_list_problems(script, page), [])
        # The script starts removing something the page says never to delete.
        for name in ("homestead-backup-target", "homestead-objectstore", "homestead-shares"):
            worse = script.replace("self own service homestead\n", f"self own service homestead\nsafe apps secret {name}\n")
            self.assertIn(f"the script removes {name}, which is under do not delete",
                          uninstall_list_problems(worse, page))
        # The page stops calling something safe.
        worse = page.replace("`homestead-smart-key`", "`a-secret`")
        self.assertTrue(any("homestead-smart-key" in problem for problem in uninstall_list_problems(script, worse)))
        # The script drops something the page still calls safe.
        worse = script.replace("safe apps secret homestead-smart-key\n", "")
        self.assertIn("the page calls homestead-smart-key safe, and the script does not remove it",
                      uninstall_list_problems(worse, page))

    def test_every_record_only_the_code_names_is_on_the_page(self):
        found = code_object_names()
        for name in ("homestead-settings", "homestead-clusters", "homestead-iso-library", "homestead-shell-sessions",
                     "homestead-cluster-shutdown"):
            self.assertIn(name, found, "the name scan found nothing it should")
        page = SS.text(PAGE)
        missing = {name: where for name, where in found.items()
                   if f"`{name}" not in page and name not in NOT_RECORDS}
        self.assertEqual(missing, {}, "Decide these and name them on the page: stays, or on the script's list")

    def test_the_by_hand_steps_name_everything_the_script_removes(self):
        page = SS.text(PAGE)
        safe = page_section(page, "### Safe to delete", "### Still used")
        block = " ".join(re.findall(r"```bash\n(.*?)```", safe, re.S))
        missing = sorted(name for name in uninstall_objects(SS.text(INSTALL))
                         if not re.search(r"[/ ]" + re.escape(name) + r"(\s|$)", block))
        self.assertEqual(missing, [], "Give these by-hand steps on the page")

    def test_the_by_hand_steps_warn_before_their_first_delete(self):
        safe = page_section(SS.text(PAGE), "### Safe to delete", "### Still used")
        before = " ".join(safe[:safe.index("```bash")].split())
        self.assertIn("Skip anything named `homestead` that is not Homestead's", before)

    def test_the_page_says_what_the_script_does_and_leaves(self):
        section = " ".join(SS.text(PAGE)[SS.text(PAGE).index("## What removing Homestead leaves behind"):].split())
        for words in ("install.sh --uninstall", "--dry-run", "--everything", "asks first", "by exact name",
                      "`homestead.io/*` annotations", "puts the node probe back", "`HS_NODEPROBE=no`", "`HOMESTEAD_NODEPROBE_AUTO_INSTALL=false`", "doesn't record its install", "`nodeprobe.enabled`", "held-for-power",
                      "`homestead-host-change-", "A later run cannot recognise", "choose `--everything` on the first run",
                      "wait at least 5 minutes", "Flux, Argo CD or a k3s `HelmChart`",
                      "skip anything named `homestead` that is not Homestead's"):
            self.assertIn(words, section)
        self.assertNotIn("nothing removes it yet", section)


if __name__ == "__main__":
    unittest.main()
