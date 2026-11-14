#!/usr/bin/env python3
"""oracle.py <profile.json> --source <clone> [--work DIR] [--edits N] [--keep]

Is an incremental build the same as a clean one, on a real project, with xcfs's defaults (build session, the
experimental Swift driver)? One worktree takes the profile's "oracle" edits one after another and is built
incrementally after each; for every step a fresh worktree gets the same edits so far and is built from nothing. Every
object file the clean build produced is compared with the incremental build's: its code (`otool -t`) and its symbol
names (`nm -j`). Debug information is not compared — it records each worktree's own paths. A build that fails on one
side must fail on the other.

An edit is {"name", "file", "find"/"replace" or "append", "breaks": true if the project no longer builds after it};
"{n}" in the text is replaced with a number. A breaking edit is undone (and built) before the next one. With --keep,
the run stops at the first step with differences and keeps both worktrees.

Prints a line per step and writes <work>/oracle-<name>-<time>.json; results are never overwritten.
"""
import concurrent.futures, hashlib, json, os, re, subprocess, sys, time

HERE = os.path.dirname(os.path.realpath(__file__))
ROOT = os.path.dirname(HERE)


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


PROFILE = json.load(open(sys.argv[1]))
SOURCE = os.path.realpath(arg("--source"))
WORK = os.path.realpath(arg("--work", os.path.join(os.path.dirname(SOURCE), "xcfs-oracle-work")))
XCFS = os.path.join(ROOT, "bin", "xcfs")
STAMP = time.strftime("%Y%m%d-%H%M%S")
os.makedirs(WORK, exist_ok=True)
_driver = os.path.join(os.environ.get("XCFS_CACHE") or os.path.expanduser("~/Library/Caches/xcfs"), "swift-driver")
if os.path.isdir(_driver) and not os.path.lexists(WORK + "/cache/swift-driver"):
    os.makedirs(WORK + "/cache", exist_ok=True)
    os.symlink(_driver, WORK + "/cache/swift-driver")
ENV = dict(os.environ, XCFS_STATE=WORK + "/state", XCFS_CACHE=WORK + "/cache", XCFS_POOL_ROOT=WORK + "/pool",
           XDG_CONFIG_HOME=WORK + "/config")
ENV.pop("CLAUDE_CODE_SESSION_ID", None)
ENV.setdefault("XCFS_DRIVER", "session")   # the configuration under test; the default picks it after its own check


def say(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def xcfs(*args):
    t0 = time.time()
    r = subprocess.run([sys.executable, XCFS, *args, "--json"], capture_output=True, text=True, env=ENV, cwd=WORK)
    try:
        out = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        out = {"ok": False, "error": (r.stderr or r.stdout)[-500:]}
    return round(time.time() - t0, 1), out


def apply_edit(root, e, n, undo=False):
    p = os.path.join(root, e["file"])
    text = open(p).read()
    if "append" in e:
        added = e["append"].replace("{n}", str(n))
        text = text[:-len(added)] if undo else text + added
    else:
        a, b = e["find"], e["replace"].replace("{n}", str(n))
        a, b = (b, a) if undo else (a, b)
        assert a in text, (e["name"], a)
        text = text.replace(a, b, 1)
    open(p, "w").write(text)


# The checkouts' own names (and so their build directories'), wherever they appear in a symbol: the labels of local
# string literals contain the string, and some strings are source paths.
CHECKOUT_NAME = re.compile(r"oracle-[ic]-%s-\d\d(-[0-9a-f]{8})?" % STAMP)


def fingerprint(obj):
    text = subprocess.run(["otool", "-t", obj], capture_output=True, text=True).stdout.split("\n", 1)[-1]
    names = CHECKOUT_NAME.sub("<checkout>", subprocess.run(["nm", "-j", obj], capture_output=True, text=True).stdout).split()
    return hashlib.sha256((text + "\0" + "\n".join(sorted(names))).encode()).hexdigest()


# Xcode names an object <file>-<hash of the source's path>.o when a target has two sources of one name: the hash
# differs between checkouts, so objects are matched by name without it (several of one name: as a sorted list).
PATH_HASH = re.compile(r"-[0-9a-f]{32}(?=\.o$)")


def objects(dd):
    base = os.path.join(dd, "Build", "Intermediates.noindex")
    paths = [os.path.join(dp, f) for dp, _, fs in os.walk(base) for f in fs if f.endswith(".o")]
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        prints = list(pool.map(fingerprint, paths))
    out = {}
    for p, h in zip(paths, prints):
        out.setdefault(PATH_HASH.sub("-#", os.path.relpath(p, base)), []).append(h)
    return {k: sorted(v) for k, v in out.items()}


SOURCE_EXT = (".swift", ".m", ".mm", ".c", ".cpp")


def recompile(checkout, dd, rels):
    """Touch the sources of these objects (in the checkout and in the packages its build checked out) and build."""
    stems = {PATH_HASH.sub("", os.path.basename(r))[:-2] for r in rels if "/Binary/" not in r}
    for top in (checkout, os.path.dirname(dd)):
        for dp, dns, fns in os.walk(top):
            dns[:] = [d for d in dns if not d.startswith(".") and d not in ("Build", "Index.noindex")]
            for f in fns:
                stem, ext = os.path.splitext(f)
                if ext in SOURCE_EXT and stem in stems:
                    os.utime(os.path.join(dp, f))
    return xcfs("build", checkout)


def main():
    cfg = json.loads(json.dumps(PROFILE["xcfs"]["config"]))
    d = os.path.join(WORK, "config", "xcfs")
    os.makedirs(d, exist_ok=True)
    json.dump(cfg, open(os.path.join(d, os.path.basename(SOURCE) + ".json"), "w"), indent=1)
    edits = PROFILE["oracle"][:int(arg("--edits", len(PROFILE["oracle"])))]
    # Both checkouts' paths (and so their build directories') have the same length: paths are compiled in (#file,
    # #filePath, assertions), and a string of another length moves every string after it, and the code that refers
    # to them, so objects built at paths of different lengths differ although nothing is wrong.
    t, out = xcfs("new", SOURCE, "oracle-i-%s-00" % STAMP)
    inc = out["worktree"]
    t, out = xcfs("build", inc)
    if not out.get("ok"):
        raise SystemExit("the first build failed: %s" % (out.get("error") or out.get("diagnostics", [])[:2]))
    say("incremental worktree %s built in %.0fs (driver %s, swift driver %s)" % (
        inc, t, out.get("driver"), out.get("swift_driver")))
    steps, applied, base_n = [], [], int(time.time()) % 100000
    try:
        for i, e in enumerate(edits, 1):
            n = base_n + i
            apply_edit(inc, e, n)
            applied.append((e, n))
            ti, oi = xcfs("build", inc)
            inc_objs = objects(oi["derived_data"]) if oi.get("ok") else {}
            tc, oc = xcfs("new", SOURCE, "oracle-c-%s-%02d" % (STAMP, i))
            clean = oc["worktree"]
            assert len(clean) == len(inc), (clean, inc)
            for ce, cn in applied:
                apply_edit(clean, ce, cn)
            tc, oc = xcfs("build", clean)
            clean_objs = objects(oc["derived_data"]) if oc.get("ok") else {}
            if oc.get("ok") and oi.get("ok"):
                assert len(oc["derived_data"]) == len(oi["derived_data"]), (oc["derived_data"], oi["derived_data"])
            diffs = []
            if bool(oi.get("ok")) != bool(oc.get("ok")):
                diffs.append("incremental build %s, clean build %s" % (
                    "succeeded" if oi.get("ok") else "failed", "succeeded" if oc.get("ok") else "failed"))
            differing = []
            for rel, h in sorted(clean_objs.items()):
                if rel not in inc_objs:
                    diffs.append("missing from the incremental build: " + rel)
                elif inc_objs[rel] != h:
                    differing.append(rel)
            # A file compiled in a clean build can come out differently from the same file compiled alone (the
            # driver batches files; what a batch emits into each object depends on its company). The clean checkout
            # has nothing stale, so its sources are compiled again there, one build, and compared once more.
            explained = []
            if differing:
                recompile(clean, oc["derived_data"], differing)
                again = objects(oc["derived_data"])
                explained = [r for r in differing if again.get(r) == inc_objs[r]]
                diffs += ["differs: " + r for r in differing if r not in explained]
            step = {"edit": e["name"], "incremental_ok": oi.get("ok"), "incremental_seconds": ti,
                    "clean_ok": oc.get("ok"), "clean_seconds": tc, "objects_compared": len(clean_objs),
                    "driver": [oi.get("driver"), oc.get("driver")], "swift_driver": oi.get("swift_driver"),
                    "differences": diffs, "same_when_compiled_alone_in_the_clean_checkout": explained}
            steps.append(step)
            say("%-60s inc %s %.0fs | clean %s %.0fs | %d objects, %d differences %s%s" % (
                e["name"][:60], oi.get("ok"), ti, oc.get("ok"), tc, len(clean_objs), len(diffs), diffs[:3],
                " (%d more matched once compiled alone)" % len(explained) if explained else ""))
            if diffs and "--keep" in sys.argv:
                say("  kept for a look: %s (incremental: %s)" % (clean, inc))
                inc = None
                return 1
            subprocess.run(["git", "-C", clean, "checkout", "--", "."], capture_output=True)
            xcfs("rm", clean, "--apply", "--prune-branch")
            if e.get("breaks"):
                apply_edit(inc, e, n, undo=True)
                applied.pop()
                xcfs("build", inc)
    finally:
        if inc:
            subprocess.run(["git", "-C", inc, "checkout", "--", "."], capture_output=True)
            xcfs("rm", inc, "--apply", "--prune-branch")
        path = os.path.join(WORK, "oracle-%s-%s.json" % (re.sub(r"\W+", "-", PROFILE["name"]).lower(), STAMP))
        data = json.dumps({"profile": PROFILE["name"], "steps": steps}, indent=1)
        for a, b in ((WORK, "<work>"), (SOURCE, "<source>"), (os.path.expanduser("~"), "~")):
            data = data.replace(a, b)
        open(path, "w").write(data)
        bad = sum(1 for s in steps if s["differences"])
        say("%d steps, %d with differences; results: %s" % (len(steps), bad, path))
    return 1 if any(s["differences"] for s in steps) else 0


if __name__ == "__main__":
    sys.exit(main())
