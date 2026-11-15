"""The experimental Swift driver (swiftdriver/): is it usable for the selected Xcode, and building it when it is not.

The patch is made for a swift-driver release line (6.4.x). For the selected Xcode, xcfs reads `swiftc --version`, takes
the swift-driver release tag of that Swift, picks the patch of its release line and builds the driver in a detached
process (clone, `git apply --check` then apply, `swift build`, a correctness self-test; ~1-2 min). Each Xcode gets its
own install, keyed by its Swift build and its location, because the driver links that Xcode's toolchain. Until the
driver is built, and whenever anything does not fit (another release line, a patch that does not apply, a failed
build or self-test), builds use Xcode's own driver; the reason is in each build's JSON and in `xcfs doctor`.
"""
import hashlib, json, os, pkgutil, re, subprocess, time

from .. import state
from ..util import read_json, run

# Patches by swift-driver release line, with the dependency pins each was built and measured with. A release of the
# line gets its line's patch if `git apply --check` accepts it; nothing is applied fuzzily. The releases the patch was
# made and measured on are pinned to their commits: a tag moved elsewhere is not built.
KITS = {"6.4": {"patch": "api-digests-swift-6.4.2.patch", "resolved": "Package-swift-6.4.resolved",
                "commits": dict.fromkeys(("swift-6.4.0-RELEASE", "swift-6.4.1-RELEASE", "swift-6.4.2-RELEASE"),
                                         "174567a5681a9a949bcd52f821deb8fa65105434")}}
KIT_FILES = ("build.sh", "selftest.py", "frontend-gate.py")
# SWIFT_EXEC: the patched driver, its frontends started through the gate (frontend-gate.py, installed as
# frontend-bin/swift-frontend) that bounds how many run at once across every build using this install. The driver
# finds the toolchain's other tools next to the frontend (swift-symbolgraph-extract, for one), so frontend-bin links
# every other tool of the toolchain's bin directory.
WRAPPER = """#!/bin/sh
# SWIFT_EXEC for Xcode (written by xcfs): the patched swift-driver, run against the frontend of the Xcode it was
# built for, through frontend-bin/swift-frontend (a gate that bounds how many frontends run at once). wrapper 2
D=$(cd "$(dirname "$0")" && pwd)
export XCFS_REAL_SWIFT_FRONTEND="%(frontend)s"
export SWIFT_DRIVER_SWIFT_FRONTEND_EXEC="$(cd "$D/.." && pwd)/frontend-bin/swift-frontend"
export SWIFT_DRIVER_API_DIGEST_DIR="${SWIFT_DRIVER_API_DIGEST_DIR:-$(cd "$D/.." && pwd -P)/store}"
exec "$D/swift-driver" --driver-mode=swiftc "$@"
"""
SELFTEST_VERSION = 2       # selftest.py's VERSION: an installed driver tested by an older self-test is tested again
RETRY_AFTER = 24 * 3600    # a failed build is not retried sooner
VERSION_LINE = re.compile(r"Apple Swift version (?P<swift>\d+\.\d+(?:\.\d+)?) \((?P<build>swiftlang-[^ )]+)[^)]*\)")
DRIVER_LINE = re.compile(r"swift-driver version: (?P<driver>[0-9][^ \n]*)")

_selected = None


def swift_release(text):
    m = re.search(r"Apple Swift version \S+ \([^)]*\)", text or "")
    return m.group(0) if m else ""


def parse_version(text):
    """{"release", "swift", "driver", "build"} from `swiftc --version` (the driver's line goes to stderr, the
    compiler's to stdout, so the two may come in either order), or None."""
    m = VERSION_LINE.search(text or "")
    if not m:
        return None
    d = DRIVER_LINE.search(text)
    swift = m.group("swift") if m.group("swift").count(".") == 2 else m.group("swift") + ".0"
    return {"release": swift_release(text), "swift": swift, "driver": d.group("driver") if d else None,
            "build": m.group("build")}


def selected():
    """The selected Xcode's Swift (it follows DEVELOPER_DIR and xcode-select), remembered per Xcode."""
    global _selected
    if _selected is None:
        from .xcode import XcodeAdapter, developer_dir
        dev = developer_dir()
        key = "2|%s|%s" % (dev, XcodeAdapter().version())   # 2: the format of what is remembered
        path = os.path.join(state.cache_dir(), "swift-driver", "selected-swift.json")
        known = read_json(path, {}) or {}
        info = known.get(key) if not os.environ.get("TOOLCHAINS") else None
        if not isinstance(info, dict):
            r = run("xcrun", "swiftc", "--version", check=False)
            info = parse_version(r.stdout + r.stderr) if r.returncode == 0 else None
            info = dict(info or {}, developer_dir=dev)
            if info.get("swift") and not os.environ.get("TOOLCHAINS"):
                known[key] = info
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as f:
                    json.dump(known, f, indent=1)
        _selected = info
    return _selected


def selected_swift():
    return (selected() or {}).get("release", "")


def kit_for(info):
    """(tag, kit) for the selected Swift, or (None, why not)."""
    if not info or not info.get("swift"):
        return None, "cannot read the selected Xcode's Swift version"
    line = ".".join(info["swift"].split(".")[:2])
    kit = KITS.get(line)
    if not kit:
        return None, "no patch for Swift %s (patches exist for %s)" % (line, ", ".join(sorted(KITS)))
    if not info.get("driver"):
        return None, "the selected Xcode's swiftc reports no swift-driver version"
    return "swift-%s-RELEASE" % info["swift"], kit


def root(info=None):
    """The install for the selected Xcode: <cache>/swift-driver/<swift>-<swift build>-<hash of the Xcode's path>."""
    info = info or selected() or {}
    where = hashlib.sha256((info.get("developer_dir") or "").encode()).hexdigest()[:6]
    name = "%s-%s-%s" % (info.get("swift", "unknown"), info.get("build", "unknown"), where)
    return os.path.join(state.cache_dir(), "swift-driver", re.sub(r"[^A-Za-z0-9._-]", "_", name))


def status():
    """(SWIFT_EXEC wrapper or None, why not, whether building it could help)."""
    info = selected()
    tag, kit = kit_for(info)
    if not tag:
        return None, kit, False
    r = root(info)
    wrapper, toolchain = os.path.join(r, "bin", "swiftc"), read_json(os.path.join(r, "toolchain.json"), None)
    test = read_json(os.path.join(r, "selftest.json"), None)
    built = bool(toolchain and os.access(wrapper, os.X_OK)
                 and swift_release(toolchain.get("swiftVersion", "")) == info["release"])
    if built and test and test.get("ok") and test.get("version") == SELFTEST_VERSION:
        refresh_wrapper(r, toolchain)
        return wrapper, None, False
    if building():
        return None, "being built for %s in the background (log: %s)" % (tag, os.path.join(r, "build.log")), False
    failed = os.path.join(r, "build-failed")
    if os.path.exists(failed) and time.time() - os.path.getmtime(failed) < RETRY_AFTER:
        why = open(failed).read().strip() or "its build failed"
        return None, "%s (log: %s); retried after a day" % (why, os.path.join(r, "build.log")), False
    if built and test and test.get("ok"):
        return None, "built for %s; its newer self-test has not run yet" % tag, True
    return None, "not built yet for %s" % tag, True


def refresh_wrapper(r, toolchain):
    """Bring an install's wrapper and frontend gate to this version of xcfs (an install made by an older one has
    neither the gate nor its wrapper); rewritten only when they differ. Anything that fails leaves the old ones."""
    if not toolchain.get("frontend") or not os.path.isdir(os.path.dirname(toolchain["frontend"])):
        return
    try:
        _refresh_wrapper(r, toolchain)
    except OSError as e:
        from ..util import warn
        warn("could not update the experimental driver's wrapper in %s: %s" % (r, e))


def _refresh_wrapper(r, toolchain):
    fbin = os.path.join(r, "frontend-bin")
    os.makedirs(fbin, exist_ok=True)
    tools = os.path.dirname(toolchain["frontend"])
    for n in os.listdir(tools):
        link, target = os.path.join(fbin, n), os.path.join(tools, n)
        if n == "swift-frontend" or (os.path.islink(link) and os.readlink(link) == target):
            continue
        if os.path.lexists(link):
            os.remove(link)
        os.symlink(target, link)
    files = {os.path.join(r, "bin", "swiftc"): (WRAPPER % {"frontend": toolchain["frontend"]}).encode(),
             os.path.join(fbin, "swift-frontend"): pkgutil.get_data(__package__, "swiftdriver/frontend-gate.py")}
    for path, data in files.items():
        try:
            with open(path, "rb") as f:
                if f.read() == data:
                    continue
        except OSError:
            pass
        tmp = "%s.%d.tmp" % (path, os.getpid())
        with open(tmp, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o755)
        os.replace(tmp, path)


def install_id():
    """Changes whenever the installed driver or its features file does; passed as a build setting so the build
    service plans anew (it keeps build descriptions while the settings are the same)."""
    parts = []
    for rel in ("bin/swift-driver", "share/swift/features.json", "toolchain.json"):
        try:
            st = os.stat(os.path.join(root(), rel))
            parts.append("%s:%d:%d" % (rel, st.st_size, int(st.st_mtime)))
        except OSError:
            parts.append(rel + ":-")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:12]


def building():
    marker = read_json(os.path.join(root(), "building"), None)
    if not marker:
        return False
    try:
        os.kill(int(marker.get("pid", 0)), 0)
        return True
    except (OSError, ValueError):
        return False


FAILURES = {2: "the build script was given bad arguments", 3: "the patch does not apply to this release",
            4: "the driver failed its correctness self-test",
            5: "the release tag does not point at the commit the patch was made for"}


def start_build():
    """Build the selected Xcode's driver in a detached process (or, when it is built and only its self-test is out of
    date, run the self-test again); returns the log path, or None."""
    info = selected()
    tag, kit = kit_for(info)
    if not tag:
        return None
    r = root(info)
    toolchain = read_json(os.path.join(r, "toolchain.json"), None)
    retest = bool(toolchain and swift_release(toolchain.get("swiftVersion", "")) == info["release"]
                  and os.access(os.path.join(r, "bin", "swiftc"), os.X_OK))
    os.makedirs(r, exist_ok=True)
    if building():
        return None
    kitdir = os.path.join(r, "kit")
    os.makedirs(kitdir, exist_ok=True)
    for name in KIT_FILES + (kit["patch"], kit["resolved"]):   # from the package, which may be a zip application
        with open(os.path.join(kitdir, name), "wb") as f:
            f.write(pkgutil.get_data(__package__, "swiftdriver/" + name))
    os.chmod(os.path.join(kitdir, "build.sh"), 0o755)
    for stale in ("build-failed", "selftest.json"):
        try:
            os.remove(os.path.join(r, stale))
        except OSError:
            pass
    log = os.path.join(r, "build.log")
    reasons = " ".join("%d) echo %s > \"$2/build-failed\";;" % (k, json.dumps(v)) for k, v in FAILURES.items())
    if retest:   # selftest.py exits 1 on a failure; reported like build.sh's 4
        run_it = ('python3 "%s" "$2/bin/swiftc" "$2/selftest.json" || (exit 4)'
                  % os.path.join(kitdir, "selftest.py"))
    else:
        run_it = '"$1" "$2" "$3" "$4" "$5" "$6" "$7"'
    script = ('%s; rc=$?; '
              'case $rc in 0) ;; %s *) echo "its build failed (exit $rc)" > "$2/build-failed";; esac; '
              'rm -f "$2/building"' % (run_it, reasons))
    with open(log, "w") as out:
        p = subprocess.Popen(["/bin/sh", "-c", script, "sh", os.path.join(kitdir, "build.sh"), r, tag,
                              kit["patch"], kit["resolved"], info["driver"], kit.get("commits", {}).get(tag, "")],
                             stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                             env=dict(os.environ, XCFS_CACHE=state.cache_dir()), start_new_session=True)
    with open(os.path.join(r, "building"), "w") as f:
        json.dump({"pid": p.pid, "started": time.time(), "tag": tag}, f)
    return log
