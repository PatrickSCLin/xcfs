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
# line gets its line's patch if `git apply --check` accepts it; nothing is applied fuzzily.
KITS = {"6.4": {"patch": "api-digests-swift-6.4.2.patch", "resolved": "Package-swift-6.4.resolved"}}
KIT_FILES = ("build.sh", "selftest.py")
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
    if toolchain and os.access(wrapper, os.X_OK) and swift_release(toolchain.get("swiftVersion", "")) == info["release"]:
        if test and test.get("ok"):
            return wrapper, None, False
    if building():
        return None, "being built for %s in the background (log: %s)" % (tag, os.path.join(r, "build.log")), False
    failed = os.path.join(r, "build-failed")
    if os.path.exists(failed) and time.time() - os.path.getmtime(failed) < RETRY_AFTER:
        why = open(failed).read().strip() or "its build failed"
        return None, "%s (log: %s); retried after a day" % (why, os.path.join(r, "build.log")), False
    return None, "not built yet for %s" % tag, True


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
            4: "the driver failed its correctness self-test"}


def start_build():
    """Build the selected Xcode's driver in a detached process; returns the log path, or None."""
    info = selected()
    tag, kit = kit_for(info)
    if not tag:
        return None
    r = root(info)
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
    script = ('"$1" "$2" "$3" "$4" "$5" "$6"; rc=$?; '
              'case $rc in 0) ;; %s *) echo "its build failed (exit $rc)" > "$2/build-failed";; esac; '
              'rm -f "$2/building"' % reasons)
    with open(log, "w") as out:
        p = subprocess.Popen(["/bin/sh", "-c", script, "sh", os.path.join(kitdir, "build.sh"), r, tag,
                              kit["patch"], kit["resolved"], info["driver"]],
                             stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                             env=dict(os.environ, XCFS_CACHE=state.cache_dir()), start_new_session=True)
    with open(os.path.join(r, "building"), "w") as f:
        json.dump({"pid": p.pid, "started": time.time(), "tag": tag}, f)
    return log
