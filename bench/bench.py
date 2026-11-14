#!/usr/bin/env python3
"""bench.py <profile.json> --source <local clone> [--agents N] [--work DIR] [--cache DIR] [--phases plain,xcfs]
           [--xcfs PATH] [--reuse-from RESULTS.json]   (sides not in --phases are taken from that earlier run)

Compare N parallel checkouts made the plain way (git worktree + the project's own setup and build) with the same N
checkouts made through xcfs (a pool of slots). The benchmark knows no project: the profile says how to create, build
and remove a plain worktree, what xcfs config to use, and which edits to time.

Measured for each side:
  - time to get a checkout ready to build, and the first build in it (what an agent waits for)
  - disk: the volume's free space before/after (real usage, clones and hard links counted once) and du per checkout
  - resident memory of processes working for each checkout (build servers, compilers) after its build
  - an edit loop in the first checkout: no-op build, then each profile edit, built one after another
  - removal: time, and what is left behind (caches outside the checkout, processes still running)

Results go to <work>/results-<name>-<time>.json and .md, with paths shortened to placeholders.
"""
import json, os, re, shutil, subprocess, sys, time

HERE = os.path.dirname(os.path.realpath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "lib"))


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


PROFILE_PATH = sys.argv[1]
PROFILE = json.load(open(PROFILE_PATH))
SOURCE = os.path.realpath(arg("--source"))
N = int(arg("--agents", PROFILE.get("agents", 3)))
WORK = os.path.realpath(arg("--work", os.path.join(os.path.dirname(SOURCE), "xcfs-bench-work")))
PHASES = arg("--phases", "plain,xcfs").split(",")
XCFS = os.path.realpath(arg("--xcfs", os.path.join(ROOT, "bin", "xcfs")))
STAMP = time.strftime("%Y%m%d-%H%M%S")
LOG = os.path.join(WORK, "logs")
os.makedirs(LOG, exist_ok=True)
ENV = dict(os.environ, XCFS_STATE=WORK + "/xcfs-state", XCFS_CACHE=WORK + "/xcfs-cache",
           XCFS_POOL_ROOT=WORK + "/pool", XDG_CONFIG_HOME=WORK + "/xcfs-config", XCFS_SESSION="bench")
for k in ("CLAUDE_CODE_SESSION_ID",):
    ENV.pop(k, None)
# A cache every checkout shares (a build tool's disk cache, say): the profile's commands find it in $XCFS_BENCH_CACHE.
ENV["XCFS_BENCH_CACHE"] = os.path.realpath(arg("--cache", os.path.join(WORK, "shared-cache")))
RESULTS = {"profile": PROFILE["name"], "agents": N, "started": STAMP, "machine": {}, "plain": {}, "xcfs": {},
           "round": {}}
VARIANTS = PROFILE["xcfs"].get("variants") or {"xcfs": {}}


def say(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def fmt(s, **kw):
    kw.setdefault("repo", SOURCE)
    kw.setdefault("work", WORK)
    kw.setdefault("bench", HERE)
    for k, v in kw.items():
        s = s.replace("{%s}" % k, str(v))
    return s


def free_kb():
    st = os.statvfs(WORK)
    return st.f_bavail * st.f_frsize // 1024


def du_kb(*paths):
    paths = [p for p in paths if p and os.path.exists(p)]
    if not paths:
        return 0
    out = subprocess.run(["du", "-sk", "-c", *paths], capture_output=True, text=True).stdout.strip().splitlines()
    return int(out[-1].split()[0]) if out else 0


def shell(cmd, cwd, log_name, timeout=None, env=None):
    """Run cmd, log its output; return (seconds, returncode, disk delta in KB)."""
    f0, t0 = free_kb(), time.time()
    with open(os.path.join(LOG, log_name + ".log"), "a") as log:
        log.write("$ %s\n" % cmd)
        log.flush()
        try:
            r = subprocess.run(cmd, shell=True, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, env=env or ENV,
                               timeout=timeout)
            code = r.returncode
        except subprocess.TimeoutExpired:
            code = None
    return round(time.time() - t0, 1), code, f0 - free_kb()


XENV = {}  # the environment of the xcfs variant being measured


def link_machine_toolchains(cache):
    """The experimental Swift driver (lib/xcfs/adapters/swiftdriver) is built once per machine into the user's xcfs cache;
    a variant with a cache of its own sees it through a link."""
    real = os.path.join(os.environ.get("XCFS_CACHE") or os.path.expanduser("~/Library/Caches/xcfs"), "swift-driver")
    link = os.path.join(cache, "swift-driver")
    if os.path.isdir(real) and not os.path.lexists(link):
        os.makedirs(cache, exist_ok=True)
        os.symlink(real, link)


def xcfs(*args, cwd=None, log_name="xcfs"):
    f0, t0 = free_kb(), time.time()
    r = subprocess.run([sys.executable, XCFS, *args, "--json"], capture_output=True, text=True, env=XENV or ENV,
                       cwd=cwd or WORK)
    with open(os.path.join(LOG, log_name + ".log"), "a") as log:
        log.write("$ xcfs %s\n%s\n%s\n" % (" ".join(args), r.stdout[-20000:], r.stderr[-5000:]))
    try:
        out = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        out = {"ok": False, "error": (r.stderr or r.stdout)[-500:]}
    return round(time.time() - t0, 1), r.returncode, f0 - free_kb(), out


def processes_for(path):
    """(pids, RSS in KB) of processes with path (on a boundary) in their command line or as their cwd."""
    pat = re.compile(re.escape(path) + r"(?=/|\s|$|\"|')")
    pids, rss = set(), 0
    for l in subprocess.run(["ps", "-axo", "pid=,rss=,command="], capture_output=True, text=True).stdout.splitlines():
        parts = l.strip().split(None, 2)
        if len(parts) == 3 and pat.search(parts[2]) and int(parts[0]) != os.getpid():
            pids.add(int(parts[0]))
            rss += int(parts[1])
    return sorted(pids), rss


def owned_caches(path):
    """Caches outside path that belong to it, as xcfs's adapters see them (DerivedData, Bazel output bases, …)."""
    from xcfs.adapter import builtin_adapters
    out = []
    for a in builtin_adapters().values():
        out += [p for p in a.owned_caches(path) if os.path.exists(p)]
    return out


def apply_edit(root, edit, n):
    path = os.path.join(root, edit["file"])
    text = open(path).read()
    if "append" in edit:
        text += fmt(edit["append"], n=n)
    else:
        if edit["find"] not in text:
            raise SystemExit("edit %r: text not found in %s" % (edit["name"], edit["file"]))
        text = text.replace(edit["find"], fmt(edit["replace"], n=n), 1)
    open(path, "w").write(text)


def revert_edits(root):
    for e in PROFILE.get("edits", []):
        subprocess.run(["git", "-C", root, "checkout", "--", e["file"]], capture_output=True)


def edit_loop(root, build, label, check=None):
    """No-op build, then each edit in turn (cumulative, as a session of work would be). Every run and every side
    writes different text: both sides may share a build cache, and an edit the other side already built would be a
    cache hit instead of a build."""
    rows = []
    base = int(time.time() * 1000) % 10 ** 9 * 10
    t, code, _ = build("no-op")
    rows.append({"step": "no-op build", "seconds": t, "ok": code == 0})
    say("  %s no-op: %ss" % (label, t))
    reps = int(PROFILE.get("editRepeats", 1))
    edits = [(e, "" if r == 0 else " (repeat %d)" % (r + 1)) for r in range(reps) for e in PROFILE.get("edits", [])]
    for i, (e, suffix) in enumerate(edits, 1):
        e = dict(e, name=e["name"] + suffix)
        if suffix and e is not None and i > 1 and edits[i - 2][1] != suffix:
            revert_edits(root)  # a repeat starts again from a clean tree
            build("revert")
        apply_edit(root, e, base + i)
        if check:  # the fast path an agent has for "does my change compile": only the edited file
            tc, ccode, _ = check(os.path.join(root, e["file"]))
            rows.append({"step": e["name"] + " — xcfs check of the file", "seconds": tc, "ok": ccode == 0})
            say("  %s check %s: %ss" % (label, e["name"], tc))
        t, code, _ = build(e["name"])
        rows.append({"step": e["name"], "seconds": t, "ok": code == 0})
        say("  %s %s: %ss%s" % (label, e["name"], t, "" if code == 0 else " (FAILED)"))
    revert_edits(root)
    return rows


def machine():
    def out(*c):
        return subprocess.run(c, capture_output=True, text=True).stdout.strip()
    m = {"model": out("sysctl", "-n", "hw.model"), "cpu": out("sysctl", "-n", "machdep.cpu.brand_string"),
         "cores": out("sysctl", "-n", "hw.ncpu"), "memory_gb": round(int(out("sysctl", "-n", "hw.memsize") or 0) / 2 ** 30),
         "macos": out("sw_vers", "-productVersion"), "xcode": " ".join(out("xcodebuild", "-version").split()),
         "source_commit": out("git", "-C", SOURCE, "rev-parse", "--short", "HEAD")}
    if PROFILE.get("versionCommand"):
        m["toolchain"] = subprocess.run(PROFILE["versionCommand"], shell=True, cwd=SOURCE, capture_output=True,
                                        text=True).stdout.strip().splitlines()[-1:] or [""]
        m["toolchain"] = m["toolchain"][0]
    return m


def xcfs_build_dir(root):
    try:
        return json.load(open(os.path.join((XENV or ENV)["XCFS_STATE"], "registry.json")))["builds"].get(root, {}).get("dir")
    except (OSError, ValueError):
        return None


def checkout_stats(root):
    """du of the checkout, and of its build output wherever it lives: caches outside the checkout that belong to it
    (global DerivedData, Bazel output bases) and, for xcfs, the slot's build directory."""
    caches = owned_caches(root)
    bdir = xcfs_build_dir(root)
    pids, rss = processes_for(root)
    return {"checkout_kb": du_kb(root), "caches_kb": du_kb(*(caches + ([bdir] if bdir else []))),
            "caches": len(caches) + (1 if bdir else 0), "processes": len(pids), "rss_kb": rss}


def stop_processes(root):
    pids, _ = processes_for(root)
    for p in pids:
        try:
            os.kill(p, 15)
        except OSError:
            pass
    time.sleep(2)


# ------------------------------------------------------------------ plain git worktrees
def run_plain():
    P = PROFILE["plain"]
    res = RESULTS["plain"] = {"checkouts": [], "edit_loop": [], "removal": []}
    dests = []
    for i in range(1, N + 1):
        dest = os.path.join(WORK, "%s-wt-plain-%d" % (os.path.basename(SOURCE), i))
        branch = "xcfs-bench/plain-%d" % i
        dests.append(dest)
        say("plain %d: create" % i)
        tc, dc, code = 0.0, 0, 0
        for c in P["create"]:
            t, code, d = shell(fmt(c, dest=dest, branch=branch), SOURCE, "plain-%d-create" % i, env=ENV)
            tc, dc = tc + t, dc + d
            if code != 0:
                raise SystemExit("plain create failed: %s (see logs)" % c)
        say("plain %d: build" % i)
        tb, code, db = shell(fmt(P["build"], dest=dest), dest, "plain-%d-build" % i)
        if code != 0:
            raise SystemExit("plain build %d failed (see %s)" % (i, LOG))
        row = dict({"agent": i, "create_seconds": round(tc, 1), "create_disk_kb": dc, "first_build_seconds": tb,
                    "build_disk_kb": db}, **checkout_stats(dest))
        res["checkouts"].append(row)
        say("plain %d: create %.1fs, first build %.1fs, disk +%.1f GB" % (i, tc, tb, (dc + db) / 2 ** 20))
    res["edit_loop"] = edit_loop(dests[0], lambda name: shell(fmt(P["build"], dest=dests[0]), dests[0],
                                                             "plain-edit-loop"), "plain")
    for i, dest in enumerate(dests, 1):
        f0 = free_kb()
        t, code, _ = shell(fmt(P["remove"], dest=dest, branch="xcfs-bench/plain-%d" % i), SOURCE, "plain-%d-remove" % i)
        if code != 0:
            say("plain %d: REMOVE FAILED (see logs)" % i)
        time.sleep(3)
        left = owned_caches(dest)
        pids, rss = processes_for(dest)
        res["removal"].append({"agent": i, "seconds": t, "reclaimed_kb": free_kb() - f0, "caches_left": len(left),
                               "caches_left_kb": du_kb(*left), "processes_left": len(pids), "rss_left_kb": rss})
        say("plain %d: removed in %.1fs; left behind %d cache(s) %.1f GB, %d process(es)" % (
            i, t, len(left), du_kb(*left) / 2 ** 20, len(pids)))
    # Put the machine back (not timed): what a careful person would have to do by hand.
    for i, dest in enumerate(dests, 1):
        stop_processes(dest)
        for c in owned_caches(dest):
            subprocess.run(["chmod", "-R", "u+w", c], capture_output=True)  # build tools leave read-only dirs
            shutil.rmtree(c, ignore_errors=True)
        subprocess.run(["git", "-C", SOURCE, "branch", "-D", "xcfs-bench/plain-%d" % i], capture_output=True)


# ------------------------------------------------------------------ xcfs pool
def run_xcfs(variant, overrides):
    """One xcfs configuration (the profile's config, with a variant's toolchain overrides), in its own state, cache,
    pool and config directories."""
    global XENV
    d = os.path.join(WORK, "xcfs-" + variant)
    link_machine_toolchains(d + "/cache")
    XENV = dict(ENV, XCFS_STATE=d + "/state", XCFS_CACHE=d + "/cache", XCFS_POOL_ROOT=d + "/pool",
                XDG_CONFIG_HOME=d + "/config")
    res = RESULTS["xcfs"][variant] = {"overrides": overrides, "warm": {}, "checkouts": [], "edit_loop": [],
                                      "release": [], "retire": {}}
    cfg = json.loads(json.dumps(PROFILE["xcfs"]["config"]))
    for t in cfg.get("toolchains", []):
        t.update(overrides)
    cfg_dir = os.path.join(d, "config", "xcfs")
    os.makedirs(cfg_dir, exist_ok=True)
    json.dump(cfg, open(os.path.join(cfg_dir, os.path.basename(SOURCE) + ".json"), "w"), indent=1)
    json.dump({"buildTimeout": 6 * 3600, "warmOnRelease": False}, open(os.path.join(cfg_dir, "settings.json"), "w"))
    if cfg.get("afterSeed") or cfg.get("prepare"):
        xcfs("trust", SOURCE)
    say("xcfs[%s]: pool warm --slots %d" % (variant, N))
    t, code, d, out = xcfs("pool", "warm", SOURCE, "--slots", str(N), log_name="xcfs-warm")
    if code != 0:
        raise SystemExit("xcfs pool warm failed: %s %s" % (out.get("error"), [x.get("error") for x in out.get("slots") or []]))
    res["warm"] = {"seconds": t, "disk_kb": d, "slots": out.get("slots")}
    say("xcfs[%s]: warmed %d slot(s) in %.1fs, disk +%.1f GB" % (variant, N, t, d / 2 ** 20))
    wts = []
    for i in range(1, N + 1):
        t1, code, d1, out = xcfs("checkout", SOURCE, "xcfs-bench/xcfs-%d" % i, log_name="xcfs-%d" % i)
        if code != 0:
            raise SystemExit("xcfs checkout failed: %s" % out.get("error"))
        wt = out["worktree"]
        wts.append(wt)
        t2, code, d2, b = xcfs("build", wt, log_name="xcfs-%d-build" % i)
        if code != 0:
            raise SystemExit("xcfs build failed in slot %d (see %s)" % (i, LOG))
        row = dict({"agent": i, "create_seconds": t1, "create_disk_kb": d1, "first_build_seconds": t2,
                    "build_disk_kb": d2, "warm_at_baseline": out.get("warm_at_baseline")}, **checkout_stats(wt))
        res["checkouts"].append(row)
        say("xcfs[%s] %d: checkout %.1fs, first build %.1fs" % (variant, i, t1, t2))
    res["edit_loop"] = edit_loop(wts[0], lambda name: xcfs("build", wts[0], log_name="xcfs-%s-edit-loop" % variant)[:3],
                                "xcfs[%s]" % variant,
                                check=(lambda f: xcfs("check", f, log_name="xcfs-%s-edit-loop" % variant)[:3])
                                if PROFILE["xcfs"].get("measureCheck") else None)
    for i, wt in enumerate(wts, 1):
        t, code, d, out = xcfs("release", wt, "--no-warm", "--prune-branch", log_name="xcfs-%d-release" % i)
        time.sleep(3)
        pids, rss = processes_for(wt)
        res["release"].append({"agent": i, "seconds": t, "ok": code == 0, "processes_left": len(pids),
                               "rss_left_kb": rss, "error": out.get("error")})
        say("xcfs[" + variant + "] %d: released in %.1fs; %d process(es) left%s" % (i, t, len(pids), "" if code == 0 else
                                                                   " -- RELEASE FAILED: %s" % out.get("error")))
    f0 = free_kb()
    t, code, _, out = xcfs("pool", "resize", SOURCE, "0", log_name="xcfs-retire")
    if out.get("retire_on_release"):
        say("xcfs: NOT retired (still leased): %s" % out["retire_on_release"])
    time.sleep(10)
    left = [c for wt in wts for c in owned_caches(wt)]
    procs = [processes_for(wt) for wt in wts]
    res["retire"] = {"seconds": t, "ok": code == 0, "reclaimed_kb_after_10s": free_kb() - f0, "caches_left": len(left),
                     "caches_left_kb": du_kb(*left), "processes_left": sum(len(p) for p, _ in procs),
                     "rss_left_kb": sum(r for _, r in procs)}
    say("xcfs[%s]: retired the pool in %.1fs; caches left %d" % (variant, t, len(left)))
    XENV = {}


# ------------------------------------------------------------------ one agent's round of work
def round_edits():
    edits = PROFILE.get("edits", [])
    k = int(PROFILE.get("roundEdits", 5))
    return [edits[i % len(edits)] for i in range(k)] if edits else []


def run_round_plain():
    """A whole task, the plain way: a worktree with the project's setup, a first build, then every edit verified by a
    build (xcodebuild has no faster way to say whether a change compiles), then removal."""
    P = PROFILE["plain"]
    dest = os.path.join(WORK, "%s-wt-round" % os.path.basename(SOURCE))
    branch = "xcfs-bench/round"
    steps, t0, f0 = [], time.time(), free_kb()
    for c in P["create"]:
        t, code, _ = shell(fmt(c, dest=dest, branch=branch), SOURCE, "round-plain")
        steps.append(("create", t))
    t, code, _ = shell(fmt(P["build"], dest=dest), dest, "round-plain")
    steps.append(("first build", t))
    base = int(time.time() * 1000) % 10 ** 9 * 10
    seen = set()
    for i, e in enumerate(round_edits(), 1):
        if e["name"] in seen:  # the same edit again (5 edits from a shorter list): start that file afresh
            subprocess.run(["git", "-C", dest, "checkout", "--", e["file"]], capture_output=True)
        seen.add(e["name"])
        apply_edit(dest, e, base + i)
        t, code, _ = shell(fmt(P["build"], dest=dest), dest, "round-plain")
        steps.append(("edit %d + build" % i, t))
    t, code, _ = shell(fmt(P["remove"], dest=dest, branch=branch), SOURCE, "round-plain")
    steps.append(("remove", t))
    total = round(time.time() - t0, 1)
    time.sleep(3)
    left = owned_caches(dest)
    RESULTS["round"]["plain"] = {"total_seconds": total, "steps": steps, "left_kb": du_kb(*left),
                                 "disk_kb_after": f0 - free_kb()}
    say("round[plain]: %.1fs total; left behind %s" % (total, gb(du_kb(*left))))
    for c in left:  # put the machine back (not timed)
        subprocess.run(["chmod", "-R", "u+w", c], capture_output=True)
        shutil.rmtree(c, ignore_errors=True)
    subprocess.run(["git", "-C", SOURCE, "branch", "-D", branch], capture_output=True)


def run_round_xcfs(variant, overrides):
    """A whole task through xcfs: a slot warmed beforehand (not timed), checkout, a first build, every edit verified
    with `xcfs check`, one full build at the end, release."""
    global XENV
    d = os.path.join(WORK, "round-xcfs-" + variant)
    link_machine_toolchains(d + "/cache")
    XENV = dict(ENV, XCFS_STATE=d + "/state", XCFS_CACHE=d + "/cache", XCFS_POOL_ROOT=d + "/pool",
                XDG_CONFIG_HOME=d + "/config")
    cfg = json.loads(json.dumps(PROFILE["xcfs"]["config"]))
    for t in cfg.get("toolchains", []):
        t.update(overrides)
    cfg_dir = os.path.join(d, "config", "xcfs")
    os.makedirs(cfg_dir, exist_ok=True)
    json.dump(cfg, open(os.path.join(cfg_dir, os.path.basename(SOURCE) + ".json"), "w"), indent=1)
    json.dump({"buildTimeout": 6 * 3600, "warmOnRelease": False}, open(os.path.join(cfg_dir, "settings.json"), "w"))
    t, code, _, out = xcfs("pool", "warm", SOURCE, "--slots", "1", log_name="round-xcfs-%s" % variant)
    if code != 0:
        raise SystemExit("round warm-up failed: %s" % out.get("error"))
    steps, t0, f0 = [], time.time(), free_kb()
    t, code, _, out = xcfs("checkout", SOURCE, "xcfs-bench/round-" + re.sub(r"[^A-Za-z0-9._-]+", "-", variant),
                           log_name="round-xcfs-%s" % variant)
    if code != 0:
        raise SystemExit("round checkout failed: %s" % out.get("error"))
    wt = out["worktree"]
    steps.append(("checkout", t))
    decisions = []   # what each build and check used: driver, Swift driver, skipped scripts, success

    def note(step, code, out):
        decisions.append({"step": step, "ok": code == 0 and out.get("ok", True) is not False,
                          **{k: out.get(k) for k in ("driver", "swift_driver", "skipped_scripts",
                                                     "skipped_scripts_reason") if out.get(k) is not None}})
    t, code, _, out = xcfs("build", wt, log_name="round-xcfs-%s" % variant)
    steps.append(("first build", t))
    note("first build", code, out)
    base = int(time.time() * 1000) % 10 ** 9 * 10
    seen = set()
    for i, e in enumerate(round_edits(), 1):
        if e["name"] in seen:
            subprocess.run(["git", "-C", wt, "checkout", "--", e["file"]], capture_output=True)
        seen.add(e["name"])
        apply_edit(wt, e, base + i)
        t, code, _, out = xcfs("check", os.path.join(wt, e["file"]), log_name="round-xcfs-%s" % variant)
        steps.append(("edit %d + check" % i, t))
        note("edit %d + check" % i, code, out)
    t, code, _, out = xcfs("build", wt, log_name="round-xcfs-%s" % variant)
    steps.append(("final build", t))
    note("final build", code, out)
    revert_edits(wt)  # the agent would commit; the benchmark must not leave edits behind
    t, code, _, out = xcfs("release", wt, "--no-warm", "--prune-branch", log_name="round-xcfs-%s" % variant)
    steps.append(("release", t))
    total = round(time.time() - t0, 1)
    RESULTS["round"]["xcfs (%s)" % variant] = {"total_seconds": total, "steps": steps, "left_kb": 0,
                                               "disk_kb_after": f0 - free_kb(), "decisions": decisions}
    say("round[xcfs %s]: %.1fs total" % (variant, total))
    xcfs("pool", "resize", SOURCE, "0", log_name="round-xcfs-%s" % variant)
    XENV = {}


# ------------------------------------------------------------------ report
def gb(kb):
    if kb is None:
        return "-"
    return "%.1f GB" % (kb / 2 ** 20) if abs(kb) >= 2 ** 20 else "%d MB" % round(kb / 1024)


def report():
    m = RESULTS["machine"]
    sides = ([("plain worktrees", RESULTS["plain"])] if RESULTS["plain"] else []) + \
        [("xcfs" if n == "xcfs" else "xcfs (%s)" % n, r) for n, r in RESULTS["xcfs"].items()]
    L = ["# Benchmark: %s, %d parallel checkouts" % (PROFILE["name"], N), "",
         "%s · %s, %s cores, %s GB · macOS %s · %s · source %s" % (
             m.get("model"), m.get("cpu"), m.get("cores"), m.get("memory_gb"), m.get("macos"), m.get("xcode"),
             m.get("source_commit")), ""]
    if m.get("toolchain"):
        L += ["Toolchain: %s" % m["toolchain"], ""]
    if PROFILE.get("description"):
        L += [PROFILE["description"], ""]

    def avg(rows, f):
        return sum(f(r) for r in rows) / len(rows) if rows else 0

    def col(f):
        return " | ".join(f(name, d) for name, d in sides)
    head = "| | " + " | ".join(n for n, _ in sides) + " |"
    sep = "|---|" + "---|" * len(sides)

    def wait(name, d):
        rows = d.get("checkouts", [])
        return "%.1f s" % avg(rows, lambda r: r["create_seconds"] + r["first_build_seconds"]) if rows else "-"

    def disk(name, d):
        rows = d.get("checkouts", [])
        kb = sum(r["create_disk_kb"] + r["build_disk_kb"] for r in rows) + (d.get("warm") or {}).get("disk_kb", 0)
        return gb(kb) + (" (incl. warm-up)" if d.get("warm") else "")

    def warm(name, d):
        return "%.1f s" % d["warm"]["seconds"] if d.get("warm") else "-"

    def mem(name, d):
        return gb(sum(r["rss_kb"] for r in d.get("checkouts", [])))

    def left(name, d):
        if "removal" in d:
            return "%s, %d process(es)" % (gb(sum(r["caches_left_kb"] for r in d["removal"])),
                                           sum(r["processes_left"] for r in d["removal"]))
        r = d.get("retire") or {}
        return "%s, %d process(es)" % (gb(r.get("caches_left_kb", 0)), r.get("processes_left", 0))
    L += ["## Summary", "", head, sep,
          "| an agent waits for a new checkout (ready + first build, mean) | %s |" % col(wait),
          "| background work before any agent asks | %s |" % col(warm),
          "| disk for %d built checkouts (volume free space used) | %s |" % (N, col(disk)),
          "| resident memory of their processes after building | %s |" % col(mem),
          "| left behind after removing them (plain: removal; xcfs: retiring the pool) | %s |" % col(left), ""]
    if RESULTS.get("round"):
        L += ["## One agent's round of work", "",
              "Plain: worktree + setup, first build, %d edits each verified by a build, removal. xcfs: checkout of a slot "
              "warmed beforehand, first build, the same edits each verified by `xcfs check`, one final build, release."
              % len(round_edits()), "", "| side | total | steps | left behind |", "|---|---|---|---|"]
        for side, r in RESULTS["round"].items():
            L.append("| %s | **%.1f s** | %s | %s |" % (side, r["total_seconds"], ", ".join(
                "%s %.1f s" % (n, t) for n, t in r["steps"]), gb(r["left_kb"])))
        L.append("")
    L += ["## Edit loop in the first checkout", "", head, sep]
    steps = []
    for _, d in sides:
        for r in d.get("edit_loop", []):
            if r["step"] not in steps:
                steps.append(r["step"])
    for st in steps:
        def cell(name, d, st=st):
            r = next((x for x in d.get("edit_loop", []) if x["step"] == st), None)
            return "-" if not r else "%.1f s%s" % (r["seconds"], "" if r["ok"] else " (failed)")
        L.append("| %s | %s |" % (st, col(cell)))
    L += ["", "## Per checkout", "",
          "| side | agent | ready to build | first build | disk (real) | checkout (du) | build output (du) | resident memory |",
          "|---|---|---|---|---|---|---|---|"]
    for name, d in sides:
        for r in d.get("checkouts", []):
            L.append("| %s | %d | %.1f s | %.1f s | %s | %s | %s | %s (%d proc) |" % (
                name, r["agent"], r["create_seconds"], r["first_build_seconds"], gb(r["create_disk_kb"] + r["build_disk_kb"]),
                gb(r["checkout_kb"]), gb(r["caches_kb"]), gb(r["rss_kb"]), r["processes"]))
    L += ["", "## Removal", "", "| side | agent | time | left behind | processes still running |", "|---|---|---|---|---|"]
    for r in RESULTS["plain"].get("removal", []):
        L.append("| plain | %d | %.1f s | %d cache(s), %s | %d (%s) |" % (
            r["agent"], r["seconds"], r["caches_left"], gb(r["caches_left_kb"]), r["processes_left"], gb(r["rss_left_kb"])))
    for name, d in sides[1 if RESULTS["plain"] else 0:]:
        for r in d.get("release", []):
            L.append("| %s release | %d | %.1f s | (slot kept for the next lease)%s | %d (%s) |" % (
                name, r["agent"], r["seconds"], "" if r.get("ok", True) else " FAILED", r["processes_left"],
                gb(r["rss_left_kb"])))
        r = d.get("retire") or {}
        if r:
            L.append("| %s pool resize 0 | all | %.1f s | %d cache(s), %s | %d (%s) |" % (
                name, r["seconds"], r["caches_left"], gb(r["caches_left_kb"]), r.get("processes_left", 0),
                gb(r.get("rss_left_kb", 0))))
    text = "\n".join(L) + "\n"
    for a, b in ((WORK, "<work>"), (SOURCE, "<source>"), (os.path.expanduser("~"), "~")):
        text = text.replace(a, b)
    return text


def main():
    if arg("--plain-from") or arg("--reuse-from"):
        # Reuse the sides of an earlier run of the same profile that this run does not measure again.
        prev = json.load(open(arg("--plain-from") or arg("--reuse-from")))
        if "plain" not in PHASES:
            RESULTS["plain"] = prev["plain"]
        for name, r in (prev.get("xcfs") or {}).items():
            if "xcfs" not in PHASES and "xcfs:" + name not in PHASES:
                RESULTS["xcfs"][name] = r
        RESULTS["reused_from"] = prev.get("started")
    # Profile environment: {repo}, {work} and {bench} (this directory, for helpers) are filled in.
    ENV.update({k: fmt(os.path.expanduser(str(v))) for k, v in PROFILE.get("env", {}).items()})
    RESULTS["machine"] = machine()
    say("benchmark %s: %d agents, work %s" % (PROFILE["name"], N, WORK))
    try:
        if "plain" in PHASES:
            run_plain()
        for name, overrides in VARIANTS.items():
            if "xcfs" in PHASES or "xcfs:" + name in PHASES:
                run_xcfs(name, overrides)
        if "round" in PHASES:
            run_round_plain()
            for name, overrides in VARIANTS.items():
                run_round_xcfs(name, overrides)
    finally:
        base = os.path.join(WORK, "results-%s-%s" % (re.sub(r"\W+", "-", PROFILE["name"]).lower(), STAMP))
        data = json.dumps(RESULTS, indent=1)
        for a, b in ((WORK, "<work>"), (SOURCE, "<source>"), (os.path.expanduser("~"), "~")):
            data = data.replace(a, b)
        open(base + ".json", "w").write(data)
        open(base + ".md", "w").write(report())
        say("results: %s.md" % base)


if __name__ == "__main__":
    main()
