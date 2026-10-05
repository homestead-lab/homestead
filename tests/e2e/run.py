"""Run a release test suite on real VMs.

    python tests/e2e/run.py --distro k3s --suite power --version 2.8.312

Needs Linux with KVM, qemu-system-x86, qemu-utils, cloud-image-utils,
kubectl and sudo (for the VM bridge). In CI, .github/workflows/release-e2e.yml
runs every suite on both distributions in parallel. --keep leaves the VMs
running to look at afterwards; --scenario runs only those scenarios of the
suite (comma-separated). --prepare-base makes the cached base image - Ubuntu
updated, with the packages the installer adds - and exits.
"""
import argparse
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import diagnostics, install, log  # noqa: E402
from harness.api import Homestead  # noqa: E402
from harness.context import Context  # noqa: E402
from harness.kube import Kube  # noqa: E402
from harness.vms import Lab  # noqa: E402
from suites import SUITES  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--distro", choices=("k3s", "rke2"), default="k3s")
    parser.add_argument("--suite", choices=sorted(SUITES))
    parser.add_argument("--version", help="a published Homestead release, e.g. 2.8.312 or 2.8.312-dev.1")
    parser.add_argument("--scenario", help="run only these scenarios of the suite, comma-separated")
    parser.add_argument("--prepare-base", action="store_true", help="make the cached base image and exit")
    parser.add_argument("--artifacts", default=os.environ.get("E2E_ARTIFACTS") or tempfile.mkdtemp(prefix="homestead-e2e-"))
    parser.add_argument("--memory", type=int, default=int(os.environ.get("E2E_MEMORY", "4096")))
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    if args.prepare_base:
        log.to(args.artifacts)
        Lab(Path(args.artifacts) / "lab", 1).prepare_base()
        return
    if not (args.suite and args.version):
        parser.error("--suite and --version are needed")
    only = {s.strip() for s in (args.scenario or "").split(",") if s.strip()}
    unknown = only - {name for name, _ in SUITES[args.suite]["scenarios"]}
    if unknown:
        parser.error(f"{args.suite} has no scenario {', '.join(sorted(unknown))}")

    suite = SUITES[args.suite]
    log.to(args.artifacts)
    log.info(f"Suite {args.suite} on {suite['nodes']} {args.distro} host(s), Homestead {args.version}; artifacts in {args.artifacts}")
    memory = (args.distro == "rke2" and suite.get("rke2_memory")) or suite.get("memory") or         (args.memory if suite["nodes"] > 1 else max(args.memory, 6144))
    lab = Lab(Path(args.artifacts) / "lab", suite["nodes"], memory=memory, cpus=suite.get("cpus", 2),
              data_disk=suite.get("data_disk", ""), hugepages=suite.get("hugepages", 0))
    failures, ctx = [], None
    try:
        log.group("Hosts")
        lab.up()
        log.end_group()
        log.group(f"Install {args.distro}, Longhorn and Homestead {args.version}")
        if suite.get("separate"):
            contexts = []
            for config, vip, nodes in install.build_separate(lab, args.distro, args.version, args.artifacts):
                kube = Kube(config)
                kube.nodes_ready(1)
                kube.deployment_ready("lab", "homestead", timeout=1800)
                contexts.append(Context(lab, kube, None, args.distro, args.version, args.artifacts, vip=vip, nodes=nodes))
            ctx, ctx.others = contexts[0], contexts[1:]
        else:
            kube = Kube(install.build(lab, args.distro, args.version, args.artifacts,
                                           agents=suite.get("agents", 0), extra=suite.get("installer")))
            kube.nodes_ready(len(lab.nodes))
            kube.deployment_ready("lab", "homestead", timeout=1800)
            ctx = Context(lab, kube, None, args.distro, args.version, args.artifacts)
        log.end_group()
        for c in [ctx] + ctx.others:
            c.api = Homestead(c.homestead_urls())
            c.api.sign_in()
        for name, scenario in suite["scenarios"]:
            if only and name not in only:
                continue
            log.group(f"Scenario: {name}")
            started = time.time()
            try:
                scenario(ctx)
                log.info(f"PASSED {name} in {int(time.time() - started)}s")
            except Exception as error:
                log.error(f"{name}: {error}")
                log.info(traceback.format_exc())
                failures.append(name)
                diagnostics.collect(ctx, name.replace(" ", "-"))
            finally:
                log.end_group()
    except Exception as error:
        log.error(f"setting up: {error}")
        log.info(traceback.format_exc())
        failures.append("setup")
        if not ctx and (Path(args.artifacts) / "kubeconfig").exists():
            # Homestead never came up: the cluster still says why.
            try:
                ctx = Context(lab, Kube(str(Path(args.artifacts) / "kubeconfig")), None, args.distro, args.version, args.artifacts)
            except Exception:
                ctx = None
        if ctx:
            diagnostics.collect(ctx, "setup")
    finally:
        if not args.keep:
            lab.down()
    log.info(("FAILED: " + ", ".join(failures)) if failures else f"Suite {args.suite} passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
