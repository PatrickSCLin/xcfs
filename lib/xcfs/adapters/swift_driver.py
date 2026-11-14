"""The experimental Swift driver (swiftdriver/): is it usable for the selected Xcode, and building it when it is not.

The driver is patched for one Swift release. It lives in <xcfs cache>/swift-driver/<release>/ (bin/swiftc is the
wrapper Xcode runs as SWIFT_EXEC; toolchain.json records the `swiftc --version` it was built for). When it is missing
and the selected Xcode has that release, xcfs builds it in a detached process (clone, patch, `swift build`, ~1-2 min);
until it is ready, and whenever anything does not match, builds use Xcode's own driver.
"""
import json, os, pkgutil, re, subprocess, time

from .. import state
from ..util import read_json, run

SWIFT = "6.4.2"            # the release the patch is made for
RETRY_AFTER = 24 * 3600    # a failed build is not retried sooner
KIT = ("build.sh", "api-digests-swift-%s.patch" % SWIFT, "Package.resolved")

_swift = None


def swift_release(text):
    m = re.search(r"Apple Swift version \S+ \([^)]*\)", text or "")
    return m.group(0) if m else ""


def selected_swift():
    """The selected Xcode's Swift, as `swiftc --version` names it: "Apple Swift version 6.4.2 (swiftlang-...)"."""
    global _swift
    if _swift is None:
        r = run("xcrun", "swiftc", "--version", check=False)
        _swift = swift_release(r.stdout + r.stderr) if r.returncode == 0 else ""
    return _swift


def root():
    return os.path.join(state.cache_dir(), "swift-driver", SWIFT)


def status():
    """(SWIFT_EXEC wrapper or None, why not, whether building it could help)."""
    have = selected_swift()
    if "Apple Swift version %s " % SWIFT not in have + " ":
        return None, "made for Swift %s; the selected Xcode has %s" % (SWIFT, have or "no Swift"), False
    r = root()
    wrapper, info = os.path.join(r, "bin", "swiftc"), read_json(os.path.join(r, "toolchain.json"), None)
    if info and os.access(wrapper, os.X_OK):
        if swift_release(info.get("swiftVersion", "")) == have:
            return wrapper, None, False
        return None, "built for %s, the selected Xcode has %s" % (swift_release(info.get("swiftVersion", "")), have), True
    if building():
        return None, "being built in the background (log: %s)" % os.path.join(r, "build.log"), False
    failed = os.path.join(r, "build-failed")
    if os.path.exists(failed) and time.time() - os.path.getmtime(failed) < RETRY_AFTER:
        return None, "its last build failed (log: %s); retried after a day" % os.path.join(r, "build.log"), False
    return None, "not built yet", True


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
    import hashlib
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


def start_build(force=False):
    """Build the driver in a detached process; returns the log path, or None if a build is already running."""
    r = root()
    os.makedirs(r, exist_ok=True)
    if building():
        return None
    kit = os.path.join(r, "kit")
    os.makedirs(kit, exist_ok=True)
    for name in KIT:  # from the package, which may be a zip application
        data = pkgutil.get_data(__package__, "swiftdriver/" + name)
        with open(os.path.join(kit, name), "wb") as f:
            f.write(data)
    os.chmod(os.path.join(kit, "build.sh"), 0o755)
    log = os.path.join(r, "build.log")
    script = ('"$1" %s && rm -f "$2/build-failed" || touch "$2/build-failed"; rm -f "$2/building"'
              % ("--force" if force else ""))
    with open(log, "w") as out:
        p = subprocess.Popen(["/bin/sh", "-c", script, "sh", os.path.join(kit, "build.sh"), r],
                             stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                             env=dict(os.environ, XCFS_CACHE=state.cache_dir()), start_new_session=True)
    with open(os.path.join(r, "building"), "w") as f:
        json.dump({"pid": p.pid, "started": time.time()}, f)
    return log
